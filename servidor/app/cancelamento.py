"""Regras de cancelamento.

Passageira: grátis até CARENCIA_SEG depois que a corrida foi aceita. Depois disso paga a taxa de
deslocamento (TAXA_BASE + POR_KM do caminho que quem dirige já fez, até TAXA_MAX). Se quem dirige já
chegou ao embarque (desistência), soma a espera cobrável. Com sinal fraco em qualquer lado, a taxa
não é cobrada na hora: vai para análise da central e as duas pessoas recebem um alerta.
A taxa vai 100% para quem dirige.

Quem dirige: cada cancelamento entra no histórico das últimas 100 corridas aceitas. A cada 10
cancelamentos nessa janela a nota cai 2%, até 10% (50 ou mais cancelamentos).
"""
from __future__ import annotations

from . import regras

CARENCIA_SEG = 3 * 60
TAXA_BASE = 5.00
TAXA_POR_KM = regras.POR_KM
TAXA_MAX = 20.00
JANELA = 100
PASSO, PENAL_POR_PASSO, PENAL_MAX = 10, 0.02, 0.10


def taxa_deslocamento(km_percorridos: float, espera_seg: float, no_embarque: bool) -> dict:
    desloc = min(TAXA_MAX, TAXA_BASE + max(0.0, km_percorridos) * TAXA_POR_KM)
    espera = (max(0.0, espera_seg - regras.ESPERA_GRATIS_SEG) / 60 * regras.ESPERA_POR_MIN) if no_embarque else 0.0
    return {"deslocamento": round(desloc, 2), "espera": round(espera, 2), "total": round(desloc + espera, 2)}


def registrar(m: dict, cancelou: bool) -> None:
    h = m.setdefault("historico_aceites", [])
    h.append("c" if cancelou else "ok")
    del h[:-JANELA]
    aplicar_penalidade(m)


def penalidade(m: dict) -> float:
    n = m.get("historico_aceites", []).count("c")
    return min(PENAL_MAX, (n // PASSO) * PENAL_POR_PASSO)


def aplicar_penalidade(m: dict) -> None:
    """A nota exibida é a média das avaliações menos a penalidade por cancelamentos."""
    m.setdefault("nota_avaliacoes", m.get("nota", 5.0))
    p = penalidade(m)
    m["penalidade_cancelamento"] = p
    m["nota"] = round(m["nota_avaliacoes"] * (1 - p), 2)


def resumo(m: dict) -> dict:
    h = m.get("historico_aceites", [])
    n = h.count("c")
    prox = (n // PASSO + 1) * PASSO
    return {"corridas_na_janela": len(h), "cancelamentos": n, "penalidade_pct": round(penalidade(m) * 100),
            "nota": m.get("nota"), "nota_avaliacoes": m.get("nota_avaliacoes", m.get("nota")),
            "proxima_penalidade_em": None if penalidade(m) >= PENAL_MAX else prox - n}
