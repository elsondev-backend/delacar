"""Pagamentos com Stripe Connect.

Modelo: cada motorista tem uma conta conectada (Express). A passageira salva um cartão;
ao fim da corrida o servidor cobra o cartão e a Stripe divide o valor na hora:
85% vão para a conta da motorista (transfer_data.destination) e 15% ficam com a
Motora (application_fee_amount). As tarifas da Stripe saem da parte da plataforma.

Variáveis de ambiente:
    STRIPE_SECRET_KEY        sk_test_... (testes) ou sk_live_... (produção)
    STRIPE_PUBLISHABLE_KEY   pk_test_... / pk_live_...  (usada pelo app para salvar o cartão)
    STRIPE_WEBHOOK_SECRET    whsec_...  (confere a assinatura dos avisos da Stripe)

Sem STRIPE_SECRET_KEY o módulo roda em modo simulado: nada é cobrado e as respostas
imitam a Stripe, para o app e os testes funcionarem sem conta.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import regras

log = logging.getLogger("motora.pagamentos")
API = "https://api.stripe.com/v1"


class ErroPagamento(Exception):
    pass


def ativo() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def chave_publica() -> str | None:
    return os.environ.get("STRIPE_PUBLISHABLE_KEY")


def centavos(valor: float) -> int:
    return int(round(valor * 100))


def taxa_plataforma(valor_centavos: int) -> int:
    return int(round(valor_centavos * regras.TAXA_PLATAFORMA))


# ---------- cliente HTTP mínimo da Stripe ----------
def _achatar(dados: dict, prefixo: str = "") -> list[tuple[str, str]]:
    """Converte {'a': {'b': 1}, 'c': [x]} no formato da Stripe: a[b]=1, c[0]=x."""
    out = []
    for k, v in dados.items():
        chave = f"{prefixo}[{k}]" if prefixo else str(k)
        if isinstance(v, dict):
            out += _achatar(v, chave)
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                out += _achatar(item, f"{chave}[{i}]") if isinstance(item, dict) else [(f"{chave}[{i}]", str(item))]
        elif isinstance(v, bool):
            out.append((chave, "true" if v else "false"))
        elif v is not None:
            out.append((chave, str(v)))
    return out


def _stripe(metodo: str, caminho: str, dados: dict | None = None, idem: str | None = None) -> dict:
    corpo = urllib.parse.urlencode(_achatar(dados or {})).encode() if metodo == "POST" else None
    req = urllib.request.Request(API + caminho, data=corpo, method=metodo)
    chave = os.environ["STRIPE_SECRET_KEY"]
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{chave}:".encode()).decode())
    if idem:
        req.add_header("Idempotency-Key", idem)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read())["error"].get("message", "")
        except Exception:
            msg = str(e)
        raise ErroPagamento(msg) from e


def _sim(prefixo: str) -> str:
    return f"{prefixo}_sim_{uuid.uuid4().hex[:14]}"


# ---------- motorista: conta de recebimento ----------
def criar_conta_motorista(m: dict) -> str:
    if not ativo():
        return _sim("acct")
    conta = _stripe("POST", "/accounts", {
        "type": "express", "country": "BR", "email": m["email"], "business_type": "individual",
        "capabilities": {"card_payments": {"requested": True}, "transfers": {"requested": True}},
        "business_profile": {"mcc": "4121", "product_description": "Motorista parceiro(a) Motora"},
        "metadata": {"motorista_id": m["id"]},
    }, idem=f"conta-{m['id']}")
    return conta["id"]


def link_cadastro(conta_id: str, url_base: str) -> str:
    """Link da página da Stripe onde a motorista informa documentos e conta bancária."""
    if not ativo():
        return url_base.rstrip("/") + "/?recebimentos=simulado"
    link = _stripe("POST", "/account_links", {
        "account": conta_id, "type": "account_onboarding",
        "refresh_url": url_base.rstrip("/") + "/?recebimentos=refazer",
        "return_url": url_base.rstrip("/") + "/?recebimentos=concluido",
    })
    return link["url"]


def status_conta(conta_id: str) -> dict:
    if not ativo():
        return {"ativo": True, "pendencias": []}
    c = _stripe("GET", f"/accounts/{conta_id}")
    return {"ativo": bool(c.get("charges_enabled") and c.get("payouts_enabled")),
            "pendencias": (c.get("requirements") or {}).get("currently_due", [])}


# ---------- passageira: cartão salvo ----------
def criar_cliente(p: dict) -> str:
    if not ativo():
        return _sim("cus")
    c = _stripe("POST", "/customers", {"name": p["nome"], "email": p["email"], "phone": p["telefone"],
                                       "metadata": {"passageira_id": p["id"]}}, idem=f"cliente-{p['id']}")
    return c["id"]


def iniciar_cartao(cliente_id: str) -> dict:
    """Cria um SetupIntent; o app usa o client_secret com o Stripe.js para salvar o cartão."""
    if not ativo():
        return {"simulado": True}
    si = _stripe("POST", "/setup_intents", {"customer": cliente_id, "usage": "off_session",
                                            "payment_method_types": ["card"]})
    return {"client_secret": si["client_secret"], "setup_intent": si["id"], "publishable_key": chave_publica()}


def confirmar_cartao(setup_intent_id: str | None) -> dict:
    if not ativo():
        return {"metodo": _sim("pm"), "marca": "visa", "final": "4242", "simulado": True}
    si = _stripe("GET", f"/setup_intents/{setup_intent_id}")
    if si.get("status") != "succeeded" or not si.get("payment_method"):
        raise ErroPagamento("O cartão ainda não foi confirmado.")
    pm = _stripe("GET", f"/payment_methods/{si['payment_method']}")
    card = pm.get("card") or {}
    return {"metodo": pm["id"], "marca": card.get("brand"), "final": card.get("last4")}


# ---------- cobrança com divisão automática ----------
def cobrar(*, valor: float, cliente_id: str, metodo_id: str, conta_motorista: str,
           corrida_id: str, motivo: str, valor_cheio: float | None = None, sem_taxa: bool = False,
           extra_motorista: float = 0.0) -> dict:
    """Cobra o cartão da passageira e envia 85% para a conta da motorista.

    `valor_cheio` é o preço sem o desconto de fidelidade: quem dirige recebe 85% dele,
    e o desconto sai da parte da Motora.
    """
    total = centavos(valor)
    # sem_taxa: reembolso integral a quem dirige (ex.: taxa de limpeza comprovada)
    # extra_motorista: parte que vai inteira para quem dirige, fora da divisão (taxa de deslocamento)
    extra = centavos(extra_motorista)
    repasse = total if sem_taxa else extra + int(round((centavos(valor_cheio if valor_cheio is not None else valor) - extra)
                                                       * (1 - regras.TAXA_PLATAFORMA)))
    taxa = max(0, total - repasse)
    registro = {"motivo": motivo, "valor": round(valor, 2), "taxa_motora": taxa / 100,
                "repasse_motorista": (total - taxa) / 100, "quando": time.time()}
    if total < 50:  # a Stripe não cobra menos de R$ 0,50
        return {**registro, "status": "isento", "id": None}
    if not ativo():
        return {**registro, "status": "pago", "id": _sim("pi"), "simulado": True}
    pi = _stripe("POST", "/payment_intents", {
        "amount": total, "currency": "brl", "customer": cliente_id, "payment_method": metodo_id,
        "off_session": True, "confirm": True, "application_fee_amount": taxa,
        "transfer_data": {"destination": conta_motorista},
        "description": f"Motora · {motivo} · corrida {corrida_id[:8]}",
        "metadata": {"corrida_id": corrida_id, "motivo": motivo},
    }, idem=f"{corrida_id}-{motivo}")
    status = "pago" if pi["status"] == "succeeded" else pi["status"]
    return {**registro, "status": status, "id": pi["id"]}


# ---------- avisos (webhooks) ----------
def verificar_webhook(payload: bytes, assinatura: str, tolerancia: int = 300) -> dict:
    segredo = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not segredo:
        raise ErroPagamento("STRIPE_WEBHOOK_SECRET não configurado.")
    partes = dict(p.split("=", 1) for p in assinatura.split(",") if "=" in p)
    t = partes.get("t", "")
    esperado = hmac.new(segredo.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    assinaturas = [v for k, v in (p.split("=", 1) for p in assinatura.split(",") if "=" in p) if k == "v1"]
    if not any(hmac.compare_digest(esperado, s) for s in assinaturas):
        raise ErroPagamento("Assinatura inválida.")
    if not t.isdigit() or abs(time.time() - int(t)) > tolerancia:
        raise ErroPagamento("Aviso antigo demais.")
    return json.loads(payload)
