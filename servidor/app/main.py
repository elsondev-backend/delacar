"""Servidor do Motora (FastAPI).

Rodar:  uvicorn app.main:app --reload
Docs da API: http://localhost:8000/docs
"""
from __future__ import annotations

import os
import random
import re
import secrets
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import notificacoes, regras
from .db import Banco

STATIC = Path(__file__).resolve().parent.parent / "static"
FUSO = ZoneInfo(os.environ.get("MOTORA_FUSO", "America/Sao_Paulo"))
# Modo demo: aceita a hora enviada pelo app e acelera o relógio da espera (1 s = 20 s),
# para testar tudo em poucos minutos. Em produção use MOTORA_MODO_DEMO=0.
DEMO = os.environ.get("MOTORA_MODO_DEMO", "1") == "1"
FATOR_TEMPO = float(os.environ.get("MOTORA_FATOR_TEMPO", "20" if DEMO else "1"))

app = FastAPI(title="Motora API", version="1.0",
              description="Corridas para passageiras mulheres, com frota elétrica e híbrida.")
banco = Banco()


def agora() -> float:
    return time.time()


def hora_local() -> int:
    return datetime.now(FUSO).hour


# ---------- modelos ----------
class XY(BaseModel):
    x: float = Field(ge=0, le=regras.MAPA_KM)
    y: float = Field(ge=0, le=regras.MAPA_KM)


class Pessoa(BaseModel):
    nome: str
    cpf: str
    nascimento: date
    telefone: str
    email: str
    contato_nome: str
    contato_telefone: str
    aceite_monitoramento: bool
    foto: str | None = Field(default=None, description="data URL da selfie (até ~2 MB)")


class Passageira(Pessoa):
    declaracao_mulher: bool


class Motorista(Pessoa):
    sexo: str = Field(pattern="^[FM]$")
    declaracao_veracidade: bool
    cnh: str
    placa: str
    modelo: str
    cor: str
    ano: int


class PedidoCorrida(BaseModel):
    passageira_id: str
    origem: XY
    destino: XY
    prefere_motorista_mulher: bool = False
    hora: int | None = Field(default=None, ge=0, le=23, description="só no modo demo")


class Estimativa(BaseModel):
    origem: XY
    destino: XY
    hora: int | None = Field(default=None, ge=0, le=23)


class Aceite(BaseModel):
    motorista_id: str
    posicao: XY | None = None


class Inicio(BaseModel):
    pin: str


class Posicao(BaseModel):
    x: float
    y: float


class Parada(BaseModel):
    ativa: bool


class Proposta(BaseModel):
    rota: list[XY] = Field(min_length=2, description="da posição atual até o destino")


class RespostaProposta(BaseModel):
    aceita: bool
    viu_engarrafamento: bool = False


class Sos(BaseModel):
    por: str = Field(pattern="^(passageira|motorista)$")


class RegiaoAjuste(BaseModel):
    chamadas: int = Field(ge=0)
    motoristas: int = Field(ge=0)


# ---------- validação de cadastro ----------
def _digitos(s: str) -> str:
    return re.sub(r"\D", "", s)


def _validar_pessoa(p: Pessoa) -> None:
    erros = []
    if len(p.nome.split()) < 2:
        erros.append("Informe o nome completo.")
    if len(_digitos(p.cpf)) != 11:
        erros.append("O CPF precisa ter 11 dígitos.")
    hoje = date.today()
    idade = hoje.year - p.nascimento.year - ((hoje.month, hoje.day) < (p.nascimento.month, p.nascimento.day))
    if idade < 18:
        erros.append("É preciso ter 18 anos ou mais.")
    if len(_digitos(p.telefone)) < 10:
        erros.append("Informe um celular com DDD.")
    if not re.match(r"^\S+@\S+\.\S+$", p.email):
        erros.append("Informe um e-mail válido.")
    if not p.contato_nome or len(_digitos(p.contato_telefone)) < 10:
        erros.append("Informe o contato de emergência com telefone.")
    if not p.aceite_monitoramento:
        erros.append("O monitoramento de percurso é obrigatório para usar o app.")
    if p.foto and len(p.foto) > 2_800_000:
        erros.append("A foto é grande demais (máximo 2 MB).")
    if erros:
        raise HTTPException(422, {"erros": erros})


def _publico(u: dict) -> dict:
    return {k: u[k] for k in ("id", "nome", "foto", "sexo", "modelo", "cor", "placa", "nota") if k in u}


# ---------- rotas da API ----------
@app.get("/api/saude")
def saude():
    return {"ok": True, "demo": DEMO}


@app.get("/api/config")
def config():
    return {
        "demo": DEMO, "fator_tempo": FATOR_TEMPO, "limite_km": regras.LIMITE_KM,
        "eletricos": regras.ELETRICOS, "hibridos": regras.HIBRIDOS,
        "tarifa": {"bandeirada": regras.BANDEIRADA, "por_km": regras.POR_KM, "por_min": regras.POR_MIN,
                   "espera_gratis_seg": regras.ESPERA_GRATIS_SEG, "espera_por_min": regras.ESPERA_POR_MIN,
                   "noturno": regras.NOTURNO, "minima": regras.TARIFA_MINIMA, "taxa": regras.TAXA_PLATAFORMA},
    }


@app.post("/api/passageiras", status_code=201)
def cadastrar_passageira(p: Passageira):
    _validar_pessoa(p)
    if not p.declaracao_mulher:
        raise HTTPException(422, {"erros": ["O app atende apenas passageiras mulheres. Confirme a declaração."]})
    uid = uuid.uuid4().hex
    banco.salvar_usuario(uid, "passageira", {**p.model_dump(mode="json"), "id": uid, "nota": 5.0})
    banco.evento(None, "info", f"Passageira {p.nome} cadastrada.")
    return {"id": uid}


@app.post("/api/motoristas", status_code=201)
def cadastrar_motorista(m: Motorista):
    _validar_pessoa(m)
    erros = []
    if not m.declaracao_veracidade:
        erros.append("Confirme a declaração para continuar.")
    if len(_digitos(m.cnh)) != 11:
        erros.append("A CNH precisa ter 11 dígitos.")
    if not re.match(r"^[A-Z]{3}-?\d[A-Z0-9]\d{2}$", m.placa, re.I):
        erros.append("Placa inválida. Use o formato ABC-1D23.")
    if not regras.veiculo_permitido(m.modelo):
        erros.append("Só aceitamos veículos elétricos ou híbridos da lista.")
    if m.ano < 2014:
        erros.append("O veículo precisa ser 2014 ou mais novo.")
    if erros:
        raise HTTPException(422, {"erros": erros})
    uid = uuid.uuid4().hex
    banco.salvar_usuario(uid, "motorista", {**m.model_dump(mode="json"), "id": uid, "nota": 5.0,
                                            "placa": m.placa.upper()})
    banco.evento(None, "info", f"Motorista {m.nome} cadastrad{'o' if m.sexo == 'M' else 'a'}.")
    return {"id": uid}


def _hora(h: int | None) -> int:
    return h if (DEMO and h is not None) else hora_local()


def _estimar(origem: dict, destino: dict, hora: int) -> dict:
    km = regras.dist(origem, destino)
    minutos = km / regras.VELOCIDADE_MEDIA * 60
    regiao = regras.regiao_de(origem)
    r = banco.regioes()[regiao]
    fator = regras.fator_dinamico(r["chamadas"], r["motoristas"])
    noite = regras.noturno(hora)
    t = regras.calcular(km, minutos, 0, noite, fator)
    return {"km": km, "minutos": minutos, "regiao": regiao, "fator": fator, "noturno": noite, "tarifa": t.dict()}


@app.post("/api/estimativa")
def estimativa(e: Estimativa):
    est = _estimar(e.origem.model_dump(), e.destino.model_dump(), _hora(e.hora))
    est["dentro_do_limite"] = 0 < est["km"] <= regras.LIMITE_KM
    return est


@app.get("/api/regioes")
def listar_regioes():
    return {n: {**d, "fator": regras.fator_dinamico(d["chamadas"], d["motoristas"])}
            for n, d in banco.regioes().items()}


@app.put("/api/regioes/{nome}")
def ajustar_regiao(nome: str, a: RegiaoAjuste):
    if nome not in banco.regioes():
        raise HTTPException(404, "Região não encontrada.")
    banco.salvar_regiao(nome, a.model_dump())
    return listar_regioes()[nome]


def _corrida(cid: str) -> dict:
    c = banco.corrida(cid)
    if not c:
        raise HTTPException(404, "Corrida não encontrada.")
    return c


def _status(c: dict, *permitidos: str) -> None:
    if c["status"] not in permitidos:
        raise HTTPException(409, f"Ação indisponível com a corrida em '{c['status']}'.")


def _ajusta_chamadas(regiao: str, delta: int) -> None:
    r = banco.regioes()[regiao]
    r["chamadas"] = max(0, r["chamadas"] + delta)
    banco.salvar_regiao(regiao, r)


def _visao(c: dict) -> dict:
    """Corrida como o app vê (inclui dados públicos das duas pessoas)."""
    out = {k: v for k, v in c.items() if k not in ("token",)}
    p = banco.usuario(c["passageira_id"]) or {}
    out["passageira"] = _publico(p)
    if c.get("motorista_id"):
        out["motorista"] = _publico(banco.usuario(c["motorista_id"]) or {})
    out["espera_seg"] = _espera(c)
    return out


def _espera(c: dict) -> float:
    s = c.get("espera_seg", 0.0)
    if c["status"] == "no_embarque" and c.get("chegada_em"):
        s += (agora() - c["chegada_em"]) * FATOR_TEMPO
    if c.get("parada_desde"):
        s += (agora() - c["parada_desde"]) * FATOR_TEMPO
    return s


@app.post("/api/corridas", status_code=201)
def pedir_corrida(pedido: PedidoCorrida):
    if not banco.usuario(pedido.passageira_id, "passageira"):
        raise HTTPException(404, "Passageira não encontrada.")
    o, d = pedido.origem.model_dump(), pedido.destino.model_dump()
    est = _estimar(o, d, _hora(pedido.hora))
    if est["km"] == 0:
        raise HTTPException(422, "Escolha um destino diferente do embarque.")
    if est["km"] > regras.LIMITE_KM:
        raise HTTPException(422, f"Destino a {est['km']:.1f} km. O limite por corrida é {regras.LIMITE_KM} km.")
    c = {
        "id": uuid.uuid4().hex, "token": None, "status": "buscando", "criada_em": agora(),
        "passageira_id": pedido.passageira_id, "motorista_id": None,
        "prefere_motorista_mulher": pedido.prefere_motorista_mulher,
        "origem": o, "destino": d, "rota": regras.rota_padrao(o, d),
        "km_base": est["km"], "km_extra": 0.0, "minutos": est["minutos"], "regiao": est["regiao"],
        "fator": est["fator"], "noturno": est["noturno"], "estimativa": est["tarifa"],
        "pin": f"{random.randint(0, 9999):04d}", "espera_seg": 0.0, "parada_desde": None,
        "posicao": None, "desvio_km": 0.0, "alerta_desvio": False, "trafego": False,
        "proposta": None, "adicional": 0.0, "compartilhada_com": None, "sos": None, "tarifa_final": None,
    }
    banco.salvar_corrida(c)
    _ajusta_chamadas(c["regiao"], +1)
    banco.evento(c["id"], "info", f"Corrida pedida em {c['regiao']}: {est['km']:.1f} km.")
    return _visao(c)


@app.get("/api/corridas/disponiveis")
def disponiveis(motorista_id: str):
    m = banco.usuario(motorista_id, "motorista")
    if not m:
        raise HTTPException(404, "Motorista não encontrado(a).")
    lista = [c for c in banco.corridas_buscando()
             if not (c["prefere_motorista_mulher"] and m["sexo"] != "F")]
    for c in lista:  # a motorista não vê o código de embarque
        c.pop("pin", None)
    return [_visao(c) | {"pin": None} for c in lista]


@app.get("/api/corridas/{cid}")
def ver_corrida(cid: str):
    return _visao(_corrida(cid))


@app.post("/api/corridas/{cid}/aceitar")
def aceitar(cid: str, a: Aceite):
    c = _corrida(cid)
    _status(c, "buscando")
    m = banco.usuario(a.motorista_id, "motorista")
    if not m:
        raise HTTPException(404, "Motorista não encontrado(a).")
    if c["prefere_motorista_mulher"] and m["sexo"] != "F":
        raise HTTPException(403, "A passageira pediu motorista mulher.")
    c.update(status="a_caminho", motorista_id=a.motorista_id, aceita_em=agora(),
             posicao=a.posicao.model_dump() if a.posicao else None)
    banco.salvar_corrida(c)
    banco.evento(cid, "info", f"{m['nome']} aceitou a corrida.")
    return _visao(c)


@app.post("/api/corridas/{cid}/chegada")
def chegada(cid: str):
    c = _corrida(cid)
    _status(c, "a_caminho")
    c.update(status="no_embarque", chegada_em=agora(), posicao=c["origem"])
    banco.salvar_corrida(c)
    banco.evento(cid, "info", "Motorista chegou ao embarque. Espera iniciada.")
    return _visao(c)


def _link(request: Request, token: str) -> str:
    base = os.environ.get("MOTORA_URL_PUBLICA") or str(request.base_url)
    return base.rstrip("/") + f"/acompanhar/{token}"


def _compartilhar(c: dict, request: Request, motivo: str) -> str:
    """Gera o link de acompanhamento e avisa o contato de emergência da passageira."""
    if not c.get("token"):
        c["token"] = secrets.token_urlsafe(16)
    link = _link(request, c["token"])
    p = banco.usuario(c["passageira_id"]) or {}
    m = banco.usuario(c["motorista_id"]) or {} if c.get("motorista_id") else {}
    if motivo == "inicio":
        texto = (f"Motora: {p.get('nome', 'Sua contato')} iniciou uma corrida com {m.get('nome', '')} "
                 f"({m.get('modelo', '')} {m.get('cor', '')}, placa {m.get('placa', '')}). "
                 f"Acompanhe a rota ao vivo: {link}")
        enviado = notificacoes.enviar(p.get("contato_telefone", ""), texto)
        c["compartilhada_com"] = {"nome": p.get("contato_nome"), "telefone": p.get("contato_telefone"),
                                  "link": link, "enviado": enviado, "quando": agora()}
        banco.evento(c["id"], "info", f"Rota compartilhada automaticamente com {p.get('contato_nome')}.")
    return link


@app.post("/api/corridas/{cid}/iniciar")
def iniciar(cid: str, i: Inicio, request: Request):
    c = _corrida(cid)
    _status(c, "no_embarque")
    if i.pin != c["pin"]:
        raise HTTPException(403, "Código não confere. Confirme com a passageira.")
    c["espera_seg"] = _espera(c)
    c.update(status="em_viagem", iniciada_em=agora())
    _compartilhar(c, request, "inicio")
    banco.salvar_corrida(c)
    banco.evento(cid, "info", "Embarque confirmado com código. Corrida iniciada.")
    return _visao(c)


@app.post("/api/corridas/{cid}/posicao")
def posicao(cid: str, p: Posicao):
    c = _corrida(cid)
    _status(c, "a_caminho", "no_embarque", "em_viagem")
    c["posicao"] = p.model_dump()
    if c["status"] == "em_viagem":
        c["desvio_km"] = regras.distancia_da_rota(c["posicao"], c["rota"])
        fora = c["desvio_km"] > regras.DESVIO_ALERTA_KM
        if fora and not c["alerta_desvio"]:
            c["alerta_desvio"] = True
            banco.evento(cid, "desvio", f"Desvio de rota detectado ({c['desvio_km']:.1f} km fora do trajeto).")
        elif not fora and c["alerta_desvio"] and c["desvio_km"] < 0.1:
            c["alerta_desvio"] = False
            banco.evento(cid, "info", "Veículo de volta ao trajeto previsto.")
    banco.salvar_corrida(c)
    return {"desvio_km": c["desvio_km"], "alerta_desvio": c["alerta_desvio"]}


@app.post("/api/corridas/{cid}/parada")
def parada(cid: str, p: Parada):
    c = _corrida(cid)
    _status(c, "em_viagem")
    if p.ativa and not c["parada_desde"]:
        c["parada_desde"] = agora()
        banco.evento(cid, "info", "Parada iniciada.")
    elif not p.ativa and c["parada_desde"]:
        c["espera_seg"] += (agora() - c["parada_desde"]) * FATOR_TEMPO
        c["parada_desde"] = None
        banco.evento(cid, "info", "Viagem retomada.")
    banco.salvar_corrida(c)
    return _visao(c)


@app.post("/api/corridas/{cid}/transito")
def transito(cid: str):
    c = _corrida(cid)
    _status(c, "em_viagem")
    c["trafego"] = True
    banco.salvar_corrida(c)
    banco.evento(cid, "desvio", "Trânsito intenso informado no trajeto.")
    return _visao(c)


@app.post("/api/corridas/{cid}/rota-alternativa")
def propor_rota(cid: str, prop: Proposta):
    c = _corrida(cid)
    _status(c, "em_viagem")
    rota = [p.model_dump() for p in prop.rota]
    if rota[-1] != c["destino"]:
        raise HTTPException(422, "A rota alternativa precisa terminar no destino.")
    atual = c["posicao"] or rota[0]
    restante = regras.dist(atual, c["destino"])
    km_extra = max(0.0, regras.comprimento(rota) - restante)
    if c["km_base"] + c["km_extra"] + km_extra > regras.LIMITE_KM * 1.5:
        raise HTTPException(422, "Rota alternativa longa demais.")
    c["proposta"] = {"rota": rota, "km_extra": km_extra,
                     "valor": regras.valor_rota_alternativa(km_extra, c["fator"], c["noturno"]), "criada_em": agora()}
    banco.salvar_corrida(c)
    banco.evento(cid, "info", f"Motorista propôs rota alternativa: +{km_extra:.1f} km, "
                              f"R$ {c['proposta']['valor']:.2f} adicional.")
    return _visao(c)


@app.post("/api/corridas/{cid}/rota-alternativa/resposta")
def responder_rota(cid: str, r: RespostaProposta):
    c = _corrida(cid)
    _status(c, "em_viagem")
    prop = c.get("proposta")
    if not prop:
        raise HTTPException(409, "Não há proposta de rota pendente.")
    if r.aceita:
        if not r.viu_engarrafamento:
            raise HTTPException(422, "Confirme que viu o engarrafamento antes de aceitar.")
        # cobrança antecipada (integre aqui o meio de pagamento: Pix, cartão etc.)
        c["adicional"] = round(c["adicional"] + prop["valor"], 2)
        c["km_extra"] += prop["km_extra"]
        c["rota"] = prop["rota"]
        c["trafego"] = False
        banco.evento(cid, "info", f"Passageira confirmou o engarrafamento e pagou R$ {prop['valor']:.2f} "
                                  "antecipadamente. Nova rota monitorada.")
    else:
        c["recusou_alternativa"] = True
        banco.evento(cid, "info", "Passageira recusou a rota alternativa.")
    c["proposta"] = None
    banco.salvar_corrida(c)
    return _visao(c)


@app.post("/api/corridas/{cid}/finalizar")
def finalizar(cid: str):
    c = _corrida(cid)
    _status(c, "em_viagem")
    if c["parada_desde"]:
        c["espera_seg"] += (agora() - c["parada_desde"]) * FATOR_TEMPO
        c["parada_desde"] = None
    t = regras.calcular(c["km_base"], c["minutos"], c["espera_seg"], c["noturno"], c["fator"], c["adicional"])
    c.update(status="concluida", concluida_em=agora(), tarifa_final=t.dict(), posicao=c["destino"],
             ganho_motorista=round(t.total * (1 - regras.TAXA_PLATAFORMA), 2))
    banco.salvar_corrida(c)
    _ajusta_chamadas(c["regiao"], -1)
    banco.evento(cid, "info", f"Corrida concluída: R$ {t.total:.2f}.")
    if c.get("compartilhada_com"):
        p = banco.usuario(c["passageira_id"]) or {}
        notificacoes.enviar(p.get("contato_telefone", ""), f"Motora: {p.get('nome')} chegou ao destino.")
    return _visao(c)


@app.post("/api/corridas/{cid}/cancelar")
def cancelar(cid: str):
    c = _corrida(cid)
    _status(c, "buscando", "a_caminho", "no_embarque")
    c["status"] = "cancelada"
    banco.salvar_corrida(c)
    _ajusta_chamadas(c["regiao"], -1)
    banco.evento(cid, "info", "Corrida cancelada.")
    return _visao(c)


@app.post("/api/corridas/{cid}/sos")
def sos(cid: str, s: Sos, request: Request):
    c = _corrida(cid)
    link = _compartilhar(c, request, "sos")
    quem = banco.usuario(c["passageira_id"] if s.por == "passageira" else c["motorista_id"]) or {}
    pos = c.get("posicao") or c["origem"]
    c["sos"] = {"por": s.por, "quando": agora(), "posicao": pos}
    notificacoes.enviar(quem.get("contato_telefone", ""),
                        f"Motora SOS: {quem.get('nome')} pediu socorro durante uma corrida. "
                        f"Localização ao vivo: {link}. Em perigo, ligue 190.")
    banco.salvar_corrida(c)
    banco.evento(cid, "sos", f"SOS acionado por {quem.get('nome')} ({s.por}) em {regras.regiao_de(pos)}.")
    return {"ok": True, "link": link}


@app.post("/api/corridas/{cid}/sos/encerrar")
def sos_fim(cid: str):
    c = _corrida(cid)
    c["sos"] = None
    banco.salvar_corrida(c)
    banco.evento(cid, "info", "Alerta de socorro encerrado.")
    return {"ok": True}


@app.get("/api/eventos")
def eventos():
    return banco.eventos()


# ---------- acompanhamento pelo contato de emergência ----------
@app.get("/api/acompanhar/{token}")
def acompanhar_dados(token: str):
    c = banco.corrida_por_token(token)
    if not c:
        raise HTTPException(404, "Link inválido ou expirado.")
    if c["status"] == "concluida" and agora() - c.get("concluida_em", 0) > 3600:
        raise HTTPException(410, "Esta corrida terminou há mais de 1 hora.")
    p = banco.usuario(c["passageira_id"]) or {}
    m = banco.usuario(c["motorista_id"]) or {} if c.get("motorista_id") else {}
    restante = regras.dist(c.get("posicao") or c["origem"], c["destino"])
    return {
        "status": c["status"], "passageira": p.get("nome", "").split(" ")[0],
        "motorista": {"nome": m.get("nome"), "foto": m.get("foto"), "modelo": m.get("modelo"),
                      "cor": m.get("cor"), "placa": m.get("placa")},
        "origem": c["origem"], "destino": c["destino"], "rota": c["rota"], "posicao": c.get("posicao"),
        "alerta_desvio": c["alerta_desvio"], "parada": bool(c.get("parada_desde")), "sos": bool(c.get("sos")),
        "chegada_min": round(restante / regras.VELOCIDADE_MEDIA * 60), "mapa_km": regras.MAPA_KM,
    }


@app.get("/acompanhar/{token}", include_in_schema=False)
def acompanhar_pagina(token: str):
    return FileResponse(STATIC / "acompanhar.html")


@app.get("/", include_in_schema=False)
def inicio():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
