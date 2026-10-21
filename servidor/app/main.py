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
import urllib.parse
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import avaliacoes, notificacoes, pagamentos, regras
from . import cancelamento, despacho, documentos, jornada, reclamacoes, resumo, vida
from . import transito as trafego
from . import postos as postos_mod
from .db import Banco
from .zonas import CELULA_KM, Zonas
from .postos import Postos

STATIC = Path(__file__).resolve().parent.parent / "static"
FUSO = ZoneInfo(os.environ.get("MOTORA_FUSO", "America/Sao_Paulo"))
# Modo demo: aceita a hora enviada pelo app e acelera o relógio da espera (1 s = 20 s),
# para testar tudo em poucos minutos. Em produção use MOTORA_MODO_DEMO=0.
DEMO = os.environ.get("MOTORA_MODO_DEMO", "1") == "1"
FATOR_TEMPO = float(os.environ.get("MOTORA_FATOR_TEMPO", "20" if DEMO else "1"))

app = FastAPI(title="Motora API", version="1.0",
              description="Corridas para passageiras mulheres, com frota elétrica e híbrida.")
banco = Banco()
zonas = Zonas(banco)
denuncias = avaliacoes.Denuncias(banco)
recl = reclamacoes.Reclamacoes(banco)
ocorr = trafego.Ocorrencias(banco)
ANTECEDENCIA_MIN_SEG = 30 * 60      # agendamento: pelo menos 30 min antes
ANTECEDENCIA_MAX_DIAS = 7
MARGEM_SAIDA_SEG = 10 * 60          # quem dirige deve chegar 10 min antes
PRIORIDADE_FAVORITOS_SEG = 60       # a chamada fica só com as favoritas por 1 min
postos = Postos(banco)


def agora() -> float:
    return time.time()


def hora_local() -> int:
    return datetime.now(FUSO).hour


def _hora_txt(t: float) -> str:
    return datetime.fromtimestamp(t, FUSO).strftime("%d/%m %H:%M")


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
    cnh_validade: date
    crlv_validade: date
    bluetooth_tipo: str = Field(default="multimidia", pattern="^(multimidia|adaptador|nenhum)$",
                                description="item de segurança: Bluetooth do próprio veículo, adaptador instalado ou nenhum")
    bluetooth_nome: str | None = Field(default=None, max_length=60, description="nome que o Bluetooth do veículo mostra")
    placa: str
    modelo: str
    cor: str
    ano: int


class PedidoCorrida(BaseModel):
    passageira_id: str
    origem: XY
    destino: XY
    prefere_motorista_mulher: bool = False
    volumes: int = Field(default=0, ge=0, le=regras.BAGAGEM_MAX_VOLUMES,
                         description="sacolas ou caixas no porta-malas (bagagem de mão não conta)")
    hora: int | None = Field(default=None, ge=0, le=23, description="só no modo demo")
    favoritos_primeiro: bool = False
    agendada_para: float | None = Field(default=None, description="horário do embarque (epoch); vazio = agora")
    motorista_preferido: str | None = Field(default=None, description="agendamento com uma motorista favorita")


class Estimativa(BaseModel):
    origem: XY
    destino: XY
    hora: int | None = Field(default=None, ge=0, le=23)
    fiel: bool = False
    volumes: int = Field(default=0, ge=0, le=regras.BAGAGEM_MAX_VOLUMES)


class Fidelidade(BaseModel):
    ativo: bool


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


class Sinal(BaseModel):
    por: str = Field(pattern="^(passageira|motorista)$")
    qualidade: str = Field(pattern="^(bom|fraco|sem)$")
    rtt_ms: int | None = None
    x: float | None = Field(default=None, description="onde o aparelho está; se faltar, usa a última posição da corrida")
    y: float | None = None


class ZonaManual(BaseModel):
    x: float = Field(ge=0, le=regras.MAPA_KM)
    y: float = Field(ge=0, le=regras.MAPA_KM)
    raio_km: float = Field(default=5, gt=0, le=50)
    nota: str | None = Field(default=None, max_length=200)


class Avaliacao(BaseModel):
    por: str = Field(pattern="^(passageira|motorista)$")
    notas: dict[str, int] = Field(description="critério -> 1 a 5 estrelas")
    ocorrencias: list[str] = Field(default_factory=list)
    comentario: str | None = Field(default=None, max_length=1000)
    bloquear: bool = False


class Decisao(BaseModel):
    decisao: str = Field(pattern="^(manter_suspensao|reativar|arquivar)$")
    nota_central: str | None = Field(default=None, max_length=500)


MOTIVOS_LIMPEZA = {"roupa_molhada": "Entrou com roupa molhada", "sujeira": "Sujou o veículo",
                   "bebida": "Bebida derramada", "vomito": "Vômito", "outro": "Outro"}
MOTIVOS_APOIO = {"recusa_sair": "Pessoa se recusa a sair do veículo", "discussao": "Discussão ou conflito",
                 "comportamento": "Comportamento inadequado", "veiculo": "Problema no veículo",
                 "pneu": "Reparo no pneu (pneu furado ou vazio)", "mecanico": "Socorro mecânico (pane ou guincho)",
                 "bateria": "Socorro de bateria (carga ou troca)",
                 "objeto": "Objeto esquecido", "rota": "Dúvida sobre a rota ou o valor", "outro": "Outro"}
TAXA_LIMPEZA_MAX = 250.0


class TaxaLimpeza(BaseModel):
    valor: float = Field(gt=0, le=TAXA_LIMPEZA_MAX)
    motivo: str
    descricao: str | None = Field(default=None, max_length=500)
    fotos: list[str] = Field(min_length=1, max_length=4, description="data URLs das fotos do dano/sujeira")
    nota_fiscal: str = Field(description="data URL da nota fiscal do serviço de limpeza")
    numero_nota: str = Field(min_length=1, max_length=60)


class Contestacao(BaseModel):
    motivo: str = Field(min_length=3, max_length=500)


class DecisaoTaxa(BaseModel):
    aprovar: bool
    nota_central: str | None = Field(default=None, max_length=500)


class Apoio(BaseModel):
    por: str = Field(pattern="^(passageira|motorista)$")
    motivo: str
    detalhe: str | None = Field(default=None, max_length=500)


class Sos(BaseModel):
    por: str = Field(pattern="^(passageira|motorista)$")
    modo: str = Field(default="normal", pattern="^(normal|assalto)$",
                      description="assalto: alerta silencioso, a tela de quem pediu não mostra nada")


class ConfirmaCartao(BaseModel):
    setup_intent_id: str | None = None


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
        "pagamentos": {"stripe": pagamentos.ativo(), "publishable_key": pagamentos.chave_publica()},
        "eletricos": regras.ELETRICOS, "hibridos": regras.HIBRIDOS,
        "tarifa": {"bandeirada": regras.BANDEIRADA, "por_km": regras.POR_KM, "por_min": regras.POR_MIN,
                   "espera_gratis_seg": regras.ESPERA_GRATIS_SEG, "parada_gratis_seg": regras.PARADA_GRATIS_SEG,
                   "bagagem_faixas": regras.BAGAGEM_FAIXAS, "dinamica_faixas": regras.FAIXAS_DINAMICA,
                   "dinamica_teto": regras.DINAMICA_TETO, "espera_por_min": regras.ESPERA_POR_MIN,
                   "noturno": regras.NOTURNO, "minima": regras.TARIFA_MINIMA, "taxa": regras.TAXA_PLATAFORMA,
                   "desconto_fiel": regras.DESCONTO_FIEL},
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
    if m.bluetooth_tipo != "nenhum" and not (m.bluetooth_nome or "").strip():
        erros.append("Informe o nome do Bluetooth do veículo (como aparece no celular).")
    hoje = datetime.now(FUSO).date()
    if m.cnh_validade < hoje:
        erros.append("A CNH está vencida. Renove para se cadastrar.")
    if m.crlv_validade < hoje:
        erros.append("O CRLV do veículo está vencido. Faça o licenciamento para se cadastrar.")
    if erros:
        raise HTTPException(422, {"erros": erros})
    uid = uuid.uuid4().hex
    dados = {**m.model_dump(mode="json"), "id": uid, "nota": 5.0, "placa": m.placa.upper()}
    vida.comprovar(dados, None, agora())   # a verificação facial do cadastro vale como a primeira prova de vida
    banco.salvar_usuario(uid, "motorista", dados)
    banco.evento(None, "info", f"Motorista {m.nome} cadastrad{'o' if m.sexo == 'M' else 'a'}.")
    return {"id": uid}


def _hora(h: int | None) -> int:
    return h if (DEMO and h is not None) else hora_local()


def _estimar(origem: dict, destino: dict, hora: int, fiel: bool = False, volumes: int = 0) -> dict:
    mr = trafego.melhor_rota(origem, destino, ocorr.ativas())
    km, minutos = mr["km"], mr["minutos"]
    regiao = regras.regiao_de(origem)
    r = banco.regioes()[regiao]
    fator = regras.fator_dinamico(r["chamadas"], r["motoristas"])
    noite = regras.noturno(hora)
    t = regras.calcular(km, minutos, 0, noite, fator, fiel=fiel, volumes=volumes)
    return {"km": km, "volumes": volumes, "bagagem_pct": regras.percentual_bagagem(volumes), "minutos": minutos, "regiao": regiao, "fator": fator, "noturno": noite, "tarifa": t.dict(),
            "alerta_sinal": zonas.alerta_para(mr["rota"]) if km else None,
            "rota": mr["rota"], "atraso_transito_min": mr["atraso_min"], "observacoes_rota": mr["observacoes"]}


@app.post("/api/estimativa")
def estimativa(e: Estimativa):
    est = _estimar(e.origem.model_dump(), e.destino.model_dump(), _hora(e.hora), e.fiel, e.volumes)
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
    out["parada_atual_seg"] = (agora() - c["parada_desde"]) * FATOR_TEMPO if c.get("parada_desde") else 0.0
    out["sinal"] = _sinal(c)
    return out


def _espera(c: dict) -> float:
    """Espera no embarque (2 min grátis)."""
    s = c.get("espera_seg", 0.0)
    if c["status"] == "no_embarque" and c.get("chegada_em"):
        s += (agora() - c["chegada_em"]) * FATOR_TEMPO
    return s


def _fechar_parada(c: dict) -> None:
    """Encerra a parada em curso; cada parada tem 1 min grátis."""
    if c.get("parada_desde"):
        dur = (agora() - c["parada_desde"]) * FATOR_TEMPO
        c["paradas_seg"] = c.get("paradas_seg", 0.0) + dur
        c["paradas_cobraveis_seg"] = c.get("paradas_cobraveis_seg", 0.0) + regras.parada_cobravel(dur)
        c["parada_desde"] = None


@app.post("/api/corridas", status_code=201)
def pedir_corrida(pedido: PedidoCorrida):
    pas = banco.usuario(pedido.passageira_id, "passageira")
    if not pas:
        raise HTTPException(404, "Passageira não encontrada.")
    if not pas.get("cartao"):
        raise HTTPException(402, "Adicione um cartão para pedir corridas.")
    if pas.get("status") == "suspensa":
        raise HTTPException(403, "Sua conta está suspensa enquanto a central analisa uma denúncia.")
    o, d = pedido.origem.model_dump(), pedido.destino.model_dump()
    est = _estimar(o, d, _hora(pedido.hora), bool(pas.get("fiel")), pedido.volumes)
    if est["km"] == 0:
        raise HTTPException(422, "Escolha um destino diferente do embarque.")
    if est["km"] > regras.LIMITE_KM:
        raise HTTPException(422, f"Destino a {est['km']:.1f} km. O limite por corrida é {regras.LIMITE_KM} km.")
    favs = pas.get("favoritos", [])
    if pedido.agendada_para is not None:
        falta = pedido.agendada_para - time.time()
        if falta < ANTECEDENCIA_MIN_SEG - 60:
            raise HTTPException(422, "Agende com pelo menos 30 minutos de antecedência.")
        if falta > ANTECEDENCIA_MAX_DIAS * 86400:
            raise HTTPException(422, "Agende com até 7 dias de antecedência.")
        if pedido.motorista_preferido and pedido.motorista_preferido not in favs:
            raise HTTPException(422, "Só dá para escolher uma pessoa motorista que está nos seus favoritos.")
    c = {
        "id": uuid.uuid4().hex, "token": None, "status": "buscando", "criada_em": agora(),
        "passageira_id": pedido.passageira_id, "motorista_id": None,
        "prefere_motorista_mulher": pedido.prefere_motorista_mulher,
        "origem": o, "destino": d, "rota": est["rota"], "observacoes_rota": est["observacoes_rota"],
        "favoritos_primeiro": bool(pedido.favoritos_primeiro and favs),
        "reservada_favoritos_ate": agora() + PRIORIDADE_FAVORITOS_SEG / FATOR_TEMPO if pedido.favoritos_primeiro and favs else None,
        "km_base": est["km"], "km_extra": 0.0, "minutos": est["minutos"], "regiao": est["regiao"],
        "fator": est["fator"], "noturno": est["noturno"], "estimativa": est["tarifa"],
        "pin": f"{random.randint(0, 9999):04d}", "espera_seg": 0.0, "parada_desde": None,
        "paradas_seg": 0.0, "paradas_cobraveis_seg": 0.0, "volumes": pedido.volumes,
        "posicao": None, "desvio_km": 0.0, "alerta_desvio": False, "trafego": False,
        "proposta": None, "adicional": 0.0, "pagamentos": [], "fiel": bool(pas.get("fiel")), "alerta_sinal": est["alerta_sinal"], "compartilhada_com": None, "sos": None, "tarifa_final": None,
    }
    if pedido.agendada_para is not None:
        c.update(status="agendada", agendada_para=pedido.agendada_para, motorista_preferido=pedido.motorista_preferido)
        banco.salvar_corrida(c)
        banco.evento(c["id"], "info", f"Corrida agendada em {c['regiao']} para {_hora_txt(pedido.agendada_para)}: {est['km']:.1f} km.")
        return _visao(c)
    banco.salvar_corrida(c)
    _ajusta_chamadas(c["regiao"], +1)
    banco.evento(c["id"], "info", f"Corrida pedida em {c['regiao']}: {est['km']:.1f} km."
                 + (" " + "; ".join(est["observacoes_rota"]) + "." if est["observacoes_rota"] else ""))
    if est["alerta_sinal"]["tem_alerta"]:
        banco.evento(c["id"], "desvio", "Corrida passa por zona de sinal fraco: " + est["alerta_sinal"]["mensagem"])
    return _visao(c)


PING_VALIDO_SEG = 60     # quem não consulta chamadas há mais de 1 min não recebe ofertas


def _ocupada(mid: str) -> bool:
    return any(c.get("motorista_id") == mid and c["status"] in ("a_caminho", "no_embarque", "em_viagem")
               for c in banco.corridas())


def _elegivel(c: dict, m: dict, exigir_posicao: bool = True) -> bool:
    if exigir_posicao and (not m.get("posicao_atual") or agora() - m.get("ultimo_ping", 0) > PING_VALIDO_SEG):
        return False
    return (m.get("status") != "suspensa" and not jornada.estado(m, agora())["em_descanso"] and _docs(m)["pode_dirigir"]
            and m.get("recebimento_ok") and not _reservada(c, m)
            and not (c["prefere_motorista_mulher"] and m["sexo"] != "F")
            and m["id"] not in c.get("recusada_por", [])
            and (not m.get("posicao_atual") or despacho.na_area(m, c["origem"]))
            and avaliacoes.pode_parear(banco.usuario(c["passageira_id"]) or {"id": c["passageira_id"]}, m))


def _despachar(c: dict) -> dict | None:
    """Oferta atual da chamada: 20 s com cada pessoa motorista, até 3 vezes para cada uma."""
    cands = [m for m in banco.usuarios("motorista") if _elegivel(c, m) and not _ocupada(m["id"])]
    lista, ignorou_6km = despacho.pool(c["origem"], cands)
    of, vezes = c.get("oferta"), c.setdefault("ofertas_por", {})
    ids = {m["id"] for m in lista}
    if of and agora() - of["desde"] < despacho.OFERTA_SEG and of["motorista_id"] in ids:
        return of
    m, concentrada = despacho.proxima(of, vezes, lista)
    if not m:
        c["oferta"] = None
    else:
        vezes[m["id"]] = vezes.get(m["id"], 0) + 1
        km = round(despacho.reta(m["posicao_atual"], c["origem"]), 1)
        c["oferta"] = {"motorista_id": m["id"], "desde": agora(), "vez": vezes[m["id"]], "concentrada": concentrada,
                       "distancia_km": km, "regra_6km_ignorada": km > despacho.EMBARQUE_MAX_KM and ignorou_6km,
                       "motoristas_disponiveis": len(lista)}
        if concentrada and not (of or {}).get("concentrada"):
            banco.evento(c["id"], "info", f"Poucas pessoas motoristas disponíveis: chamada concentrada em {m['nome']} até chegar outra.")
        if c["oferta"]["regra_6km_ignorada"] and not (of or {}).get("regra_6km_ignorada"):
            banco.evento(c["id"], "info", f"Menos de {despacho.POUCOS_MOTORISTAS} pessoas motoristas a até 6 km: "
                                          f"chamada enviada a {m['nome']}, a {km:.1f} km do embarque.")
    banco.salvar_corrida(c)
    return c["oferta"]


@app.get("/api/corridas/disponiveis")
def disponiveis(motorista_id: str, x: float | None = None, y: float | None = None):
    """Chamadas oferecidas a esta pessoa agora. Com a posição (x, y), vale o despacho: 20 s por oferta,
    até 3 vezes para cada pessoa, embarque a até 6 km e área de atuação. Sem posição, lista todas as compatíveis."""
    m = banco.usuario(motorista_id, "motorista")
    if not m:
        raise HTTPException(404, "Motorista não encontrado(a).")
    if m.get("status") == "suspensa" or jornada.estado(m, agora())["em_descanso"] or not _docs(m)["pode_dirigir"]:
        return []
    if x is not None and y is not None:
        m.update(posicao_atual={"x": x, "y": y}, ultimo_ping=agora())
        banco.salvar_usuario(m["id"], "motorista", m)
    out = []
    for c in banco.corridas_buscando():
        if m.get("posicao_atual"):
            of = _despachar(c)
            if not of or of["motorista_id"] != motorista_id:
                continue
            extra = {"oferta": {**of, "expira_em_seg": round(despacho.OFERTA_SEG - (agora() - of["desde"]), 1),
                                "vezes_max": None if of["concentrada"] else despacho.OFERTAS_MAX}}
        elif _elegivel(c, m, exigir_posicao=False):
            extra = {}
        else:
            continue
        out.append(_visao(c) | {"pin": None} | extra)   # a motorista não vê o código de embarque
    return out


class Recusa(BaseModel):
    motorista_id: str


@app.post("/api/corridas/{cid}/recusar")
def recusar(cid: str, r: Recusa):
    """Recusar (ou deixar passar os 20 s) passa a chamada para a próxima pessoa."""
    c = _corrida(cid)
    _status(c, "buscando")
    of = c.get("oferta")
    if of and of["motorista_id"] == r.motorista_id:
        of["desde"] = 0
        banco.salvar_corrida(c)
    nova = _despachar(c)
    return {"oferta_para_voce": bool(nova and nova["motorista_id"] == r.motorista_id), "vez": (nova or {}).get("vez")}


# ---------- área de atuação ----------
class Area(BaseModel):
    bairro: str | None = None
    x: float | None = Field(default=None, ge=0, le=regras.MAPA_KM)
    y: float | None = Field(default=None, ge=0, le=regras.MAPA_KM)
    nome: str | None = Field(default=None, max_length=120, description="endereço ou referência")


@app.get("/api/regras/resumo")
def resumo_regras():
    return resumo.RESUMO


@app.get("/api/bairros")
def bairros():
    return {"raio_km": despacho.AREA_RAIO_KM, "bairros": [{"nome": n, "x": x, "y": y} for n, (x, y) in despacho.BAIRROS.items()]}


@app.put("/api/motoristas/{mid}/area")
def definir_area(mid: str, a: Area):
    m = banco.usuario(mid, "motorista")
    if not m:
        raise HTTPException(404, "Motorista não encontrado(a).")
    if a.bairro:
        if a.bairro not in despacho.BAIRROS:
            raise HTTPException(422, "Bairro não encontrado. Escolha um da lista ou marque o endereço no mapa.")
        x, y = despacho.BAIRROS[a.bairro]
        nome = a.bairro
    elif a.x is not None and a.y is not None:
        x, y, nome = a.x, a.y, a.nome or "Endereço marcado no mapa"
    else:
        raise HTTPException(422, "Informe o bairro ou o endereço.")
    m["area"] = {"nome": nome, "x": x, "y": y, "raio_km": despacho.AREA_RAIO_KM}
    banco.salvar_usuario(mid, "motorista", m)
    banco.evento(None, "info", f"{m['nome']} passou a atuar a até {despacho.AREA_RAIO_KM} km de {nome}.")
    return m["area"]


@app.delete("/api/motoristas/{mid}/area")
def remover_area(mid: str):
    m = banco.usuario(mid, "motorista")
    if not m:
        raise HTTPException(404, "Motorista não encontrado(a).")
    m.pop("area", None)
    banco.salvar_usuario(mid, "motorista", m)
    return {"area": None}


@app.get("/api/corridas/{cid}")
def ver_corrida(cid: str):
    return _visao(_corrida(cid))


@app.post("/api/corridas/{cid}/aceitar")
def aceitar(cid: str, a: Aceite):
    c = _corrida(cid)
    _status(c, "buscando")
    m = _pode_aceitar(c, a.motorista_id)
    of = c.get("oferta")
    if of and of["motorista_id"] != a.motorista_id and agora() - of["desde"] < despacho.OFERTA_SEG:
        raise HTTPException(409, "Esta chamada está sendo oferecida a outra pessoa motorista agora.")
    if _reservada(c, m):
        raise HTTPException(409, "Esta chamada está reservada por alguns instantes para as motoristas favoritas da passageira.")
    c.update(status="a_caminho", motorista_id=a.motorista_id, aceita_em=agora(),
             posicao=a.posicao.model_dump() if a.posicao else None,
             posicao_aceite=a.posicao.model_dump() if a.posicao else None,
             motorista_favorito=a.motorista_id in (banco.usuario(c["passageira_id"]) or {}).get("favoritos", []))
    banco.salvar_corrida(c)
    banco.evento(cid, "info", f"{m['nome']} aceitou a corrida." + (" É motorista favorita da passageira." if c["motorista_favorito"] else ""))
    return _visao(c)


def _reservada(c: dict, m: dict) -> bool:
    ate = c.get("reservada_favoritos_ate")
    return bool(ate and agora() < ate and m["id"] not in (banco.usuario(c["passageira_id"]) or {}).get("favoritos", []))


def _pode_aceitar(c: dict, mid: str) -> dict:
    m = banco.usuario(mid, "motorista")
    if not m:
        raise HTTPException(404, "Motorista não encontrado(a).")
    if c["prefere_motorista_mulher"] and m["sexo"] != "F":
        raise HTTPException(403, "A passageira pediu motorista mulher.")
    if not m.get("recebimento_ok"):
        raise HTTPException(403, "Conecte sua conta de recebimento antes de aceitar corridas.")
    if m.get("status") == "suspensa":
        raise HTTPException(403, "Sua conta está suspensa enquanto a central analisa uma denúncia.")
    if not avaliacoes.pode_parear(banco.usuario(c["passageira_id"]) or {"id": c["passageira_id"]}, m):
        raise HTTPException(403, "Esta corrida não está disponível para você.")
    doc = _docs(m)
    if not doc["pode_dirigir"]:
        raise HTTPException(403, doc["mensagem"])
    if not vida.estado(m, agora())["valida"]:
        raise HTTPException(403, "Prova de vida vencida. Tire uma selfie no app para continuar recebendo corridas.")
    jt = jornada.atualizar(m, agora())
    banco.salvar_usuario(m["id"], "motorista", m)
    if jt["em_descanso"]:
        raise HTTPException(403, "Descanso obrigatório: depois de 12 horas de trabalho, são 6 horas de descanso.")
    if not jt["online"]:
        jornada.ficar_online(m, True, agora())   # aceitar uma corrida conta como estar disponível
        banco.salvar_usuario(m["id"], "motorista", m)
    if mid in c.get("recusada_por", []):
        raise HTTPException(403, "Você cancelou esta corrida.")
    return m


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


@app.post("/api/corridas/{cid}/compartilhar")
def compartilhar(cid: str, request: Request):
    """Link de acompanhamento e mensagem pronta para a passageira enviar pelo WhatsApp."""
    c = _corrida(cid)
    if c["status"] in ("concluida", "cancelada"):
        raise HTTPException(409, "A corrida terminou. O compartilhamento vale só durante a corrida.")
    link = _compartilhar(c, request, "manual")
    banco.salvar_corrida(c)
    p = banco.usuario(c["passageira_id"]) or {}
    m = banco.usuario(c["motorista_id"]) or {} if c.get("motorista_id") else {}
    carro = f" com {m.get('nome')} ({m.get('modelo', '')} {m.get('cor', '')}, placa {m.get('placa', '')})" if m else ""
    texto = (f"Estou em uma corrida Motora{carro}. Acompanhe minha rota ao vivo e abra a localização "
             f"no seu app de mapas: {link}")
    q = urllib.parse.quote(texto, safe="")
    tel = notificacoes.telefone_e164(p.get("contato_telefone", "")).lstrip("+")
    banco.evento(cid, "info", "Passageira compartilhou a rota pelo WhatsApp.")
    return {"link": link, "texto": texto, "whatsapp": f"https://wa.me/?text={q}",
            "whatsapp_contato": f"https://wa.me/{tel}?text={q}" if len(tel) > 4 else None,
            "contato": p.get("contato_nome")}


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
    c["posicao_em"] = agora()
    chegou = False
    if c["status"] == "a_caminho" and regras.dist(c["posicao"], c["origem"]) <= CHEGADA_AUTO_KM:
        # chegada automática pela marcação do GPS: perto do ponto de embarque, a espera começa sozinha
        c.update(status="no_embarque", chegada_em=agora(), chegada_automatica=True)
        banco.evento(cid, "info", "Chegada automática pelo GPS: motorista no ponto de embarque. Espera iniciada.")
        p_ = banco.usuario(c["passageira_id"]) or {}
        notificacoes.enviar(p_.get("telefone", ""), "Motora: seu motorista chegou ao ponto de embarque.")
        chegou = True
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
    return {"desvio_km": c["desvio_km"], "alerta_desvio": c["alerta_desvio"], "chegada_automatica": chegou,
            "status": c["status"]}


CHEGADA_AUTO_KM = 0.05  # 50 m do embarque
SEM_POSICAO_SEG = 30  # sem nova posição de quem dirige por mais que isso = sinal provavelmente ruim


def _sinal(c: dict) -> dict:
    """Qualidade do sinal de cada lado: o que o aparelho informou e, para quem dirige,
    também o tempo desde a última posição recebida."""
    s = {k: dict(v) for k, v in (c.get("sinal") or {}).items()}
    if c["status"] in ("a_caminho", "em_viagem") and c.get("posicao_em"):
        atraso = agora() - c["posicao_em"]
        m = s.setdefault("motorista", {"qualidade": "bom"})
        m["atraso_posicao_seg"] = round(atraso)
        if atraso > SEM_POSICAO_SEG and m["qualidade"] == "bom":
            m["qualidade"] = "fraco"
    return s


@app.post("/api/corridas/{cid}/sinal")
def sinal(cid: str, s: Sinal):
    """O aparelho informa a qualidade da própria internet; o outro lado é avisado pelo app."""
    c = _corrida(cid)
    antes = (c.get("sinal") or {}).get(s.por, {}).get("qualidade", "bom")
    c.setdefault("sinal", {})[s.por] = {"qualidade": s.qualidade, "rtt_ms": s.rtt_ms, "quando": agora()}
    if s.qualidade != "bom":  # alimenta o mapa de zonas de sinal fraco
        if s.x is not None and s.y is not None:
            onde = {"x": s.x, "y": s.y}
        elif s.por == "passageira" and c["status"] in ("buscando", "a_caminho", "no_embarque"):
            onde = c["origem"]
        else:
            onde = c.get("posicao") or c["origem"]
        zonas.relatar(onde, s.qualidade, s.por, cid)
    if s.qualidade != antes:
        txt = {"bom": "normalizado", "fraco": "fraco", "sem": "perdido"}[s.qualidade]
        banco.evento(cid, "info" if s.qualidade == "bom" else "desvio",
                     f"Sinal de internet {txt} na localização da {s.por}." if s.por == "passageira"
                     else f"Sinal de internet {txt} na localização de quem dirige.")
    banco.salvar_corrida(c)
    return _sinal(c)


@app.post("/api/corridas/{cid}/parada")
def parada(cid: str, p: Parada):
    c = _corrida(cid)
    _status(c, "em_viagem")
    if p.ativa and not c["parada_desde"]:
        c["parada_desde"] = agora()
        banco.evento(cid, "info", "Parada iniciada.")
    elif not p.ativa and c["parada_desde"]:
        _fechar_parada(c)
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
        pg = _cobrar(c, prop["valor"], "rota-alternativa")
        if pg["status"] not in ("pago", "isento"):
            raise HTTPException(402, "Não foi possível cobrar o valor adicional no cartão. A rota original foi mantida.")
        c["adicional"] = round(c["adicional"] + prop["valor"], 2)
        c["km_extra"] += prop["km_extra"]
        c["rota"] = prop["rota"]
        c["alerta_sinal"] = zonas.alerta_para(c["rota"])
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
    _fechar_parada(c)
    t = regras.calcular(c["km_base"], c["minutos"], c["espera_seg"], c["noturno"], c["fator"], c["adicional"],
                        fiel=c.get("fiel", False), paradas_cobraveis_seg=c.get("paradas_cobraveis_seg", 0.0),
                        volumes=c.get("volumes", 0))
    c.update(status="concluida", concluida_em=agora(), tarifa_final=t.dict(), posicao=c["destino"],
             ganho_motorista=round(t.cheio * (1 - regras.TAXA_PLATAFORMA), 2))
    m = banco.usuario(c["motorista_id"]) or {}
    if m:
        cancelamento.registrar(m, cancelou=False)   # corrida concluída conta na janela de 100
        banco.salvar_usuario(m["id"], "motorista", m)
    # o adicional da rota alternativa já foi cobrado; aqui cobra o restante.
    # Se o cartão recusar, a falha fica registrada em c["pagamentos"] para nova tentativa.
    _cobrar(c, round(t.total - c["adicional"], 2), "corrida", round(t.cheio - c["adicional"], 2),
            extra_motorista=t.deslocamento)
    banco.salvar_corrida(c)
    _ajusta_chamadas(c["regiao"], -1)
    banco.evento(cid, "info", f"Corrida concluída: R$ {t.total:.2f}.")
    if c.get("compartilhada_com"):
        p = banco.usuario(c["passageira_id"]) or {}
        notificacoes.enviar(p.get("contato_telefone", ""), f"Motora: {p.get('nome')} chegou ao destino.")
    return _visao(c)


class Cancelamento(BaseModel):
    por: str = Field(default="passageira", pattern="^(passageira|motorista)$")
    motivo: str | None = Field(default=None, max_length=200)


def _info_cancelamento(c: dict) -> dict:
    """Prévia para a passageira: se cancelar agora, paga ou não, quanto e por quê."""
    if c["status"] == "buscando" or not c.get("aceita_em"):
        return {"gratis": True, "carencia_restante_seg": None, "taxa": None, "sinal_fraco": False}
    passado = (agora() - c["aceita_em"]) * FATOR_TEMPO
    no_emb = c["status"] == "no_embarque"
    sinal = _sinal(c)
    fraco = any(v.get("qualidade", "bom") != "bom" for v in sinal.values())
    if passado <= cancelamento.CARENCIA_SEG and not no_emb:
        return {"gratis": True, "carencia_restante_seg": round(cancelamento.CARENCIA_SEG - passado), "taxa": None,
                "sinal_fraco": fraco}
    km_aprox = regras.dist(c["posicao_aceite"], c["origem"]) if c.get("posicao_aceite") else 0.0
    km = km_aprox if no_emb else min(km_aprox, passado / 3600 * regras.VELOCIDADE_MEDIA)
    return {"gratis": False, "carencia_restante_seg": 0, "sinal_fraco": fraco, "desistencia": no_emb,
            "taxa": cancelamento.taxa_deslocamento(km, _espera(c), no_emb)}


@app.get("/api/corridas/{cid}/cancelamento")
def previa_cancelamento(cid: str):
    return _info_cancelamento(_corrida(cid))


@app.post("/api/corridas/{cid}/cancelar")
def cancelar(cid: str, cn: Cancelamento | None = None):
    cn = cn or Cancelamento()
    c = _corrida(cid)
    _status(c, "agendada", "buscando", "a_caminho", "no_embarque")
    agendada = c["status"] == "agendada"
    if cn.por == "motorista":
        if c["status"] == "buscando" or not c.get("motorista_id"):
            raise HTTPException(409, "Esta corrida ainda não foi aceita.")
        m = banco.usuario(c["motorista_id"]) or {}
        cancelamento.registrar(m, cancelou=True)
        banco.salvar_usuario(m["id"], "motorista", m)
        if agendada:   # o agendamento volta a ficar aberto para outras pessoas motoristas
            c.update(motorista_id=None, confirmada_em=None, recusada_por=[*c.get("recusada_por", []), m["id"]])
            banco.salvar_corrida(c)
            banco.evento(cid, "desvio", f"{m.get('nome')} desistiu da corrida agendada para {_hora_txt(c['agendada_para'])}. "
                                        "O agendamento voltou a ficar aberto.")
            p = banco.usuario(c["passageira_id"]) or {}
            notificacoes.enviar(p.get("telefone", ""), "Motora: a pessoa motorista desistiu da sua corrida agendada. "
                                                       "Estamos procurando outra.")
            return {**_visao(c), "penalidade": cancelamento.resumo(m)}
        # a passageira volta para a fila e recebe outra pessoa motorista
        c.update(status="buscando", motorista_id=None, aceita_em=None, chegada_em=None, posicao=None,
                 recusada_por=[*c.get("recusada_por", []), m["id"]])
        banco.salvar_corrida(c)
        r = cancelamento.resumo(m)
        banco.evento(cid, "desvio", f"{m.get('nome')} cancelou a corrida{f' ({cn.motivo})' if cn.motivo else ''}. "
                                    f"{r['cancelamentos']} cancelamento(s) nas últimas {r['corridas_na_janela']} corridas; "
                                    f"penalidade na nota: {r['penalidade_pct']}%. Buscando outra pessoa motorista.")
        return {**_visao(c), "penalidade": r}
    info = _info_cancelamento(c)
    c["status"] = "cancelada"
    c["cancelamento"] = {"por": "passageira", "quando": agora(), "motivo": cn.motivo, **info}
    if not agendada:
        _ajusta_chamadas(c["regiao"], -1)
    if info["taxa"]:
        if info["sinal_fraco"]:
            c["cancelamento"]["status_taxa"] = "em_analise"
            banco.evento(cid, "desvio", f"Cancelamento com sinal de internet fraco. Taxa de deslocamento de "
                                        f"R$ {info['taxa']['total']:.2f} em análise pela central, sem cobrança automática.")
        else:
            banco.salvar_corrida(c)
            pg = _cobrar(c, info["taxa"]["total"], "taxa-cancelamento", sem_taxa=True)
            c = _corrida(cid)
            c["cancelamento"]["status_taxa"] = "cobrada" if pg["status"] in ("pago", "isento") else "cobranca_falhou"
    if c.get("motorista_id"):
        m = banco.usuario(c["motorista_id"]) or {}
        notificacoes.enviar(m.get("telefone", ""), "Motora: a passageira cancelou a corrida."
                            + (f" Você recebe a taxa de deslocamento de R$ {info['taxa']['total']:.2f}." if info["taxa"] and not info["sinal_fraco"] else ""))
    banco.salvar_corrida(c)
    banco.evento(cid, "info", "Corrida cancelada pela passageira" + (
        " (grátis, dentro de 3 minutos)." if not info["taxa"] else
        f" ({'desistência após a chegada' if info.get('desistencia') else 'após 3 minutos'}): taxa de R$ {info['taxa']['total']:.2f}."))
    return _visao(c)


@app.post("/api/corridas/{cid}/taxa-cancelamento/decisao")
def decidir_taxa_cancelamento(cid: str, d: DecisaoTaxa):
    c = _corrida(cid)
    cc = c.get("cancelamento") or {}
    if cc.get("status_taxa") != "em_analise":
        raise HTTPException(409, "Não há taxa de cancelamento em análise.")
    if d.aprovar:
        pg = _cobrar(c, cc["taxa"]["total"], "taxa-cancelamento", sem_taxa=True)
        c = _corrida(cid)
        c["cancelamento"]["status_taxa"] = "cobrada" if pg["status"] in ("pago", "isento") else "cobranca_falhou"
    else:
        c["cancelamento"]["status_taxa"] = "dispensada"
    banco.salvar_corrida(c)
    banco.evento(cid, "info", f"Central {'cobrou' if d.aprovar else 'dispensou'} a taxa de cancelamento.")
    return c["cancelamento"]


@app.get("/api/motoristas/{mid}/cancelamentos")
def cancelamentos_motorista(mid: str):
    return cancelamento.resumo(_motorista(mid))


@app.post("/api/corridas/{cid}/sos")
def sos(cid: str, s: Sos, request: Request):
    c = _corrida(cid)
    link = _compartilhar(c, request, "sos")
    quem = banco.usuario(c["passageira_id"] if s.por == "passageira" else c["motorista_id"]) or {}
    pos = c.get("posicao") or c["origem"]
    c["sos"] = {"por": s.por, "quando": agora(), "posicao": pos, "modo": s.modo}
    assalto = s.modo == "assalto"
    notificacoes.enviar(quem.get("contato_telefone", ""),
                        (f"Motora ALERTA SILENCIOSO: {quem.get('nome')} pode estar sendo assaltada(o). NÃO ligue para "
                         f"ela(e). A central já acionou a polícia. Localização ao vivo: {link}.") if assalto else
                        (f"Motora SOS: {quem.get('nome')} pediu socorro durante uma corrida. "
                         f"Localização ao vivo: {link}. Em perigo, ligue 190."))
    banco.salvar_corrida(c)
    banco.evento(cid, "sos", (f"MODO ASSALTO (silencioso) acionado por {quem.get('nome')} ({s.por}) em "
                              f"{regras.regiao_de(pos)}. Acionar a polícia e não ligar para o aparelho.") if assalto else
                 f"SOS acionado por {quem.get('nome')} ({s.por}) em {regras.regiao_de(pos)}.")
    return {"ok": True, "link": link}


@app.post("/api/corridas/{cid}/sos/encerrar")
def sos_fim(cid: str):
    c = _corrida(cid)
    c["sos"] = None
    banco.salvar_corrida(c)
    banco.evento(cid, "info", "Alerta de socorro encerrado.")
    return {"ok": True}


# ---------- pagamentos (Stripe Connect) ----------
def _cobrar(c: dict, valor: float, motivo: str, valor_cheio: float | None = None, sem_taxa: bool = False,
            extra_motorista: float = 0.0) -> dict:
    p = banco.usuario(c["passageira_id"]) or {}
    m = banco.usuario(c["motorista_id"]) or {}
    try:
        pg = pagamentos.cobrar(valor=valor, cliente_id=p.get("stripe_cliente"), metodo_id=(p.get("cartao") or {}).get("metodo"),
                               conta_motorista=m.get("stripe_conta"), corrida_id=c["id"], motivo=motivo,
                               valor_cheio=valor_cheio, sem_taxa=sem_taxa, extra_motorista=extra_motorista)
    except pagamentos.ErroPagamento as e:
        pg = {"motivo": motivo, "valor": valor, "status": "falhou", "erro": str(e), "quando": agora()}
    c.setdefault("pagamentos", []).append(pg)
    banco.salvar_corrida(c)
    if pg["status"] == "pago":
        banco.evento(c["id"], "info", f"Pagamento de R$ {valor:.2f} ({motivo}): R$ {pg['repasse_motorista']:.2f} "
                                      f"para quem dirigiu e R$ {pg['taxa_motora']:.2f} para a Motora.")
    elif pg["status"] != "isento":
        banco.evento(c["id"], "desvio", f"Cobrança de R$ {valor:.2f} ({motivo}) não aprovada: {pg.get('erro', pg['status'])}.")
    return pg


@app.post("/api/motoristas/{mid}/recebimentos")
def conectar_recebimentos(mid: str, request: Request):
    """Cria a conta Stripe da pessoa motorista e devolve o link do cadastro bancário."""
    m = banco.usuario(mid, "motorista")
    if not m:
        raise HTTPException(404, "Motorista não encontrado(a).")
    try:
        if not m.get("stripe_conta"):
            m["stripe_conta"] = pagamentos.criar_conta_motorista(m)
        url = pagamentos.link_cadastro(m["stripe_conta"], os.environ.get("MOTORA_URL_PUBLICA") or str(request.base_url))
        st = pagamentos.status_conta(m["stripe_conta"]) if not pagamentos.ativo() else {"ativo": m.get("recebimento_ok", False)}
    except pagamentos.ErroPagamento as e:
        raise HTTPException(502, f"Stripe: {e}")
    m["recebimento_ok"] = st["ativo"]
    banco.salvar_usuario(mid, "motorista", m)
    return {"url": url, "ativo": m["recebimento_ok"], "simulado": not pagamentos.ativo()}


@app.get("/api/motoristas/{mid}/recebimentos")
def status_recebimentos(mid: str):
    m = banco.usuario(mid, "motorista")
    if not m:
        raise HTTPException(404, "Motorista não encontrado(a).")
    if not m.get("stripe_conta"):
        return {"ativo": False, "pendencias": ["conectar_conta"]}
    try:
        st = pagamentos.status_conta(m["stripe_conta"])
    except pagamentos.ErroPagamento as e:
        raise HTTPException(502, f"Stripe: {e}")
    m["recebimento_ok"] = st["ativo"]
    banco.salvar_usuario(mid, "motorista", m)
    return st


@app.post("/api/passageiras/{pid}/fidelidade")
def fidelidade(pid: str, f: Fidelidade):
    """Entrar ou sair do Motora Fiel: 5% de desconto em km e tempo (a bandeirada não tem desconto)."""
    p = banco.usuario(pid, "passageira")
    if not p:
        raise HTTPException(404, "Passageira não encontrada.")
    p["fiel"] = f.ativo
    banco.salvar_usuario(pid, "passageira", p)
    banco.evento(None, "info", f"{p['nome']} {'entrou no' if f.ativo else 'saiu do'} Motora Fiel.")
    return {"fiel": f.ativo, "desconto": regras.DESCONTO_FIEL}


@app.post("/api/passageiras/{pid}/cartao")
def iniciar_cartao(pid: str):
    """Começa o cadastro do cartão. Com Stripe ativo, devolve o client_secret para o Stripe.js."""
    p = banco.usuario(pid, "passageira")
    if not p:
        raise HTTPException(404, "Passageira não encontrada.")
    try:
        if not p.get("stripe_cliente"):
            p["stripe_cliente"] = pagamentos.criar_cliente(p)
            banco.salvar_usuario(pid, "passageira", p)
        r = pagamentos.iniciar_cartao(p["stripe_cliente"])
        if r.get("simulado"):
            p["cartao"] = pagamentos.confirmar_cartao(None)
            banco.salvar_usuario(pid, "passageira", p)
            return {"cartao": _cartao_publico(p["cartao"]), "simulado": True}
        return r
    except pagamentos.ErroPagamento as e:
        raise HTTPException(502, f"Stripe: {e}")


@app.post("/api/passageiras/{pid}/cartao/confirmar")
def confirmar_cartao(pid: str, body: ConfirmaCartao):
    p = banco.usuario(pid, "passageira")
    if not p:
        raise HTTPException(404, "Passageira não encontrada.")
    try:
        p["cartao"] = pagamentos.confirmar_cartao(body.setup_intent_id)
    except pagamentos.ErroPagamento as e:
        raise HTTPException(402, str(e))
    banco.salvar_usuario(pid, "passageira", p)
    return {"cartao": _cartao_publico(p["cartao"])}


def _cartao_publico(c: dict) -> dict:
    return {"marca": c.get("marca"), "final": c.get("final"), "simulado": bool(c.get("simulado"))}


@app.post("/api/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request):
    payload = await request.body()
    try:
        ev = pagamentos.verificar_webhook(payload, request.headers.get("stripe-signature", ""))
    except pagamentos.ErroPagamento as e:
        raise HTTPException(400, str(e))
    obj = ev["data"]["object"]
    if ev["type"] == "account.updated":
        for m in banco.usuarios("motorista"):
            if m.get("stripe_conta") == obj["id"]:
                m["recebimento_ok"] = bool(obj.get("charges_enabled") and obj.get("payouts_enabled"))
                banco.salvar_usuario(m["id"], "motorista", m)
    elif ev["type"] in ("payment_intent.succeeded", "payment_intent.payment_failed"):
        cid = (obj.get("metadata") or {}).get("corrida_id")
        c = banco.corrida(cid) if cid else None
        if c:
            for pg in c.get("pagamentos", []):
                if pg.get("id") == obj["id"]:
                    pg["status"] = "pago" if ev["type"].endswith("succeeded") else "falhou"
            banco.salvar_corrida(c)
    return {"recebido": True}


# ---------- zonas de sinal fraco (gestão pela central) ----------
@app.get("/api/zonas-sinal")
def listar_zonas():
    """Zonas automáticas (formadas pelos relatos das corridas) e zonas marcadas pela central."""
    return {"celula_km": CELULA_KM, "zonas": zonas.listar()}


@app.get("/api/zonas-sinal/verificar")
def verificar_zona(x: float, y: float):
    z = zonas.zona_em({"x": x, "y": y})
    return {"em_zona": bool(z), "zona": z}


@app.post("/api/zonas-sinal", status_code=201)
def marcar_zona(z: ZonaManual):
    nova = zonas.marcar(z.x, z.y, z.raio_km, z.nota)
    banco.evento(None, "info", f"Central marcou zona de sinal fraco em {nova['regiao']} (raio {z.raio_km:g} km).")
    return nova


@app.delete("/api/zonas-sinal/{zid}")
def remover_zona(zid: str):
    if not zonas.remover(zid):
        raise HTTPException(404, "Zona não encontrada.")
    banco.evento(None, "info", "Central removeu uma zona de sinal fraco.")
    return {"ok": True}


# ---------- avaliação mútua, denúncias e bloqueio ----------
@app.get("/api/avaliacoes/criterios")
def criterios():
    return {"criterios": avaliacoes.CRITERIOS, "ocorrencias": avaliacoes.OCORRENCIAS,
            "graves": sorted(avaliacoes.GRAVES)}


@app.post("/api/corridas/{cid}/avaliacao")
def avaliar(cid: str, a: Avaliacao):
    c = _corrida(cid)
    _status(c, "concluida")
    if (c.get("avaliacoes") or {}).get(a.por):
        raise HTTPException(409, "Esta corrida já foi avaliada por você.")
    erros = avaliacoes.validar(a.por, a.notas, a.ocorrencias)
    if erros:
        raise HTTPException(422, {"erros": erros})
    autor_id, alvo_id = ((c["passageira_id"], c["motorista_id"]) if a.por == "passageira"
                         else (c["motorista_id"], c["passageira_id"]))
    alvo_tipo = "motorista" if a.por == "passageira" else "passageira"
    autor, alvo = banco.usuario(autor_id), banco.usuario(alvo_id)
    avaliacoes.aplicar_nota(alvo, a.notas)
    resposta = {"nota_registrada": True, "bloqueado": False, "denuncia": None}
    if a.bloquear:
        autor["bloqueados"] = sorted(avaliacoes.bloqueados(autor) | {alvo_id})
        resposta["bloqueado"] = True
        banco.evento(cid, "info", f"{autor['nome']} bloqueou {alvo['nome']}: não serão mais pareados.")
    if a.ocorrencias:
        d = denuncias.criar(corrida_id=cid, por=a.por, autor_id=autor_id, denunciado_id=alvo_id,
                            denunciado_tipo=alvo_tipo, ocorrencias=a.ocorrencias, comentario=a.comentario)
        if d["grave"]:
            alvo["status"] = "suspensa"
        resposta["denuncia"] = {"id": d["id"], "grave": d["grave"], "rotulos": d["rotulos"]}
        banco.evento(cid, "sos", f"Denúncia de {a.por} contra {alvo['nome']}: {', '.join(d['rotulos'])}."
                                 + (" Conta suspensa até análise." if d["grave"] else " Em análise pela central."))
    banco.salvar_usuario(autor_id, a.por, autor)
    banco.salvar_usuario(alvo_id, alvo_tipo, alvo)
    c.setdefault("avaliacoes", {})[a.por] = {"notas": a.notas, "ocorrencias": a.ocorrencias, "quando": agora()}
    banco.salvar_corrida(c)
    resposta["nota_atual_avaliado"] = alvo["nota"]
    return resposta


@app.get("/api/usuarios/{uid}/reputacao")
def reputacao(uid: str):
    u = banco.usuario(uid)
    if not u:
        raise HTTPException(404, "Pessoa não encontrada.")
    rep = u.get("reputacao") or {"avaliacoes": 0, "medias": {}}
    return {"nota": u.get("nota"), "avaliacoes": rep["avaliacoes"], "medias": rep.get("medias", {}),
            "status": u.get("status", "ativa")}


@app.get("/api/denuncias")
def listar_denuncias():
    out = []
    for d in denuncias.listar():
        alvo = banco.usuario(d["denunciado_id"]) or {}
        out.append({**d, "denunciado_nome": alvo.get("nome"), "status_conta": alvo.get("status", "ativa")})
    return out


@app.post("/api/denuncias/{did}/decisao")
def decidir_denuncia(did: str, dec: Decisao):
    d = denuncias.obter(did)
    if not d:
        raise HTTPException(404, "Denúncia não encontrada.")
    alvo = banco.usuario(d["denunciado_id"])
    if dec.decisao == "manter_suspensao":
        alvo["status"] = "suspensa"
    else:
        alvo["status"] = "ativa"
    banco.salvar_usuario(d["denunciado_id"], d["denunciado_tipo"], alvo)
    d.update(status={"manter_suspensao": "suspensao_mantida", "reativar": "reativada", "arquivar": "arquivada"}[dec.decisao],
             decidida_em=agora(), nota_central=dec.nota_central)
    denuncias.salvar(d)
    banco.evento(d["corrida_id"], "info", f"Central analisou a denúncia contra {alvo['nome']}: {d['status'].replace('_', ' ')}.")
    return d


# ---------- taxa de limpeza (comprovada por fotos e nota fiscal) ----------
@app.post("/api/corridas/{cid}/taxa-limpeza", status_code=201)
def pedir_taxa_limpeza(cid: str, t: TaxaLimpeza):
    """Quem dirige pede a taxa; só é cobrada se a central aprovar as fotos e a nota fiscal."""
    c = _corrida(cid)
    _status(c, "concluida")
    if c.get("taxa_limpeza"):
        raise HTTPException(409, "Já existe um pedido de taxa de limpeza para esta corrida.")
    if t.motivo not in MOTIVOS_LIMPEZA:
        raise HTTPException(422, "Motivo inválido.")
    if agora() - c.get("concluida_em", agora()) > 48 * 3600:
        raise HTTPException(422, "O prazo para pedir taxa de limpeza é de 48 horas após a corrida.")
    if any(len(x) > 2_800_000 for x in [*t.fotos, t.nota_fiscal]):
        raise HTTPException(422, "Cada arquivo pode ter no máximo 2 MB.")
    c["taxa_limpeza"] = {"id": uuid.uuid4().hex[:10], "valor": round(t.valor, 2), "motivo": t.motivo,
                         "motivo_texto": MOTIVOS_LIMPEZA[t.motivo], "descricao": t.descricao, "fotos": t.fotos,
                         "nota_fiscal": t.nota_fiscal, "numero_nota": t.numero_nota, "status": "em_analise",
                         "contestacao": None, "pedida_em": agora()}
    banco.salvar_corrida(c)
    banco.evento(cid, "info", f"Pedido de taxa de limpeza de R$ {t.valor:.2f} ({MOTIVOS_LIMPEZA[t.motivo]}), "
                              f"com {len(t.fotos)} foto(s) e nota fiscal {t.numero_nota}. Em análise.")
    p = banco.usuario(c["passageira_id"]) or {}
    notificacoes.enviar(p.get("telefone", ""), f"Motora: foi pedida uma taxa de limpeza de R$ {t.valor:.2f} pela sua "
                                               "última corrida. Ela só será cobrada se a central aprovar as provas. "
                                               "Você pode contestar no app.")
    return _taxa_publica(c["taxa_limpeza"])


def _taxa_publica(t: dict, com_arquivos: bool = False) -> dict:
    out = {k: v for k, v in t.items() if k not in ("fotos", "nota_fiscal")}
    out["fotos_qtd"] = len(t.get("fotos", []))
    if com_arquivos:
        out["fotos"], out["nota_fiscal"] = t.get("fotos", []), t.get("nota_fiscal")
    return out


@app.post("/api/corridas/{cid}/taxa-limpeza/contestar")
def contestar_taxa(cid: str, ct: Contestacao):
    c = _corrida(cid)
    t = c.get("taxa_limpeza")
    if not t or t["status"] != "em_analise":
        raise HTTPException(409, "Não há taxa de limpeza em análise para contestar.")
    t["contestacao"] = {"motivo": ct.motivo, "quando": agora()}
    banco.salvar_corrida(c)
    banco.evento(cid, "info", "Passageira contestou a taxa de limpeza.")
    return _taxa_publica(t)


@app.get("/api/taxas-limpeza")
def listar_taxas(com_arquivos: bool = False):
    return [{**_taxa_publica(c["taxa_limpeza"], com_arquivos), "corrida_id": c["id"]}
            for c in banco.corridas() if c.get("taxa_limpeza")]


@app.post("/api/corridas/{cid}/taxa-limpeza/decisao")
def decidir_taxa(cid: str, d: DecisaoTaxa):
    c = _corrida(cid)
    t = c.get("taxa_limpeza")
    if not t or t["status"] != "em_analise":
        raise HTTPException(409, "Não há taxa de limpeza em análise.")
    if d.aprovar:
        pg = _cobrar(c, t["valor"], "taxa-limpeza", sem_taxa=True)  # 100% para quem dirige
        c = _corrida(cid)
        t = c["taxa_limpeza"]
        t["status"] = "cobrada" if pg["status"] in ("pago", "isento") else "cobranca_falhou"
    else:
        t["status"] = "recusada"
    t.update(decidida_em=agora(), nota_central=d.nota_central)
    banco.salvar_corrida(c)
    banco.evento(cid, "info", f"Central {'aprovou' if d.aprovar else 'recusou'} a taxa de limpeza de R$ {t['valor']:.2f}.")
    return _taxa_publica(t)


# ---------- Socorro Motora: apoio da central durante a corrida ----------
@app.post("/api/corridas/{cid}/apoio", status_code=201)
def pedir_apoio(cid: str, a: Apoio):
    """Pedido de apoio à central (não é emergência policial; para perigo use o SOS)."""
    c = _corrida(cid)
    _status(c, "a_caminho", "no_embarque", "em_viagem", "concluida")
    if a.motivo not in MOTIVOS_APOIO:
        raise HTTPException(422, "Motivo inválido.")
    pos = c.get("posicao") or c["origem"]
    ap = {"id": uuid.uuid4().hex[:10], "por": a.por, "motivo": a.motivo, "motivo_texto": MOTIVOS_APOIO[a.motivo],
          "detalhe": a.detalhe, "status": "aguardando", "posicao": pos, "quando": agora(), "parceiro_sugerido": None}
    if a.motivo in ("pneu", "bateria"):   # sugere o parceiro credenciado mais perto
        perto = postos.listar(pos, [a.motivo])
        ap["parceiro_sugerido"] = perto[0] if perto else None
    c.setdefault("apoios", []).append(ap)
    banco.salvar_corrida(c)
    banco.evento(cid, "sos", f"Socorro Motora pedido pela {a.por}: {MOTIVOS_APOIO[a.motivo]}.")
    return ap


@app.get("/api/apoios")
def listar_apoios(abertos: bool = True):
    out = []
    for c in banco.corridas():
        for ap in c.get("apoios", []):
            if not abertos or ap["status"] != "encerrado":
                out.append({**ap, "corrida_id": c["id"], "status_corrida": c["status"]})
    return sorted(out, key=lambda x: -x["quando"])


@app.post("/api/corridas/{cid}/apoio/{aid}/{acao}")
def atualizar_apoio(cid: str, aid: str, acao: str):
    if acao not in ("atender", "encerrar"):
        raise HTTPException(404, "Ação inválida.")
    c = _corrida(cid)
    ap = next((x for x in c.get("apoios", []) if x["id"] == aid), None)
    if not ap:
        raise HTTPException(404, "Pedido de apoio não encontrado.")
    ap["status"] = "em_atendimento" if acao == "atender" else "encerrado"
    ap[f"{acao}_em"] = agora()
    banco.salvar_corrida(c)
    banco.evento(cid, "info", f"Central {'está atendendo' if acao == 'atender' else 'encerrou'} o pedido de apoio.")
    return ap


# ---------- Motora Verde: desconto em postos credenciados ----------
class NovoPosto(BaseModel):
    nome: str = Field(min_length=3, max_length=80)
    tipo: str = Field(pattern="^(recarga|etanol|pneu|bateria)$")
    x: float = Field(ge=0, le=regras.MAPA_KM)
    y: float = Field(ge=0, le=regras.MAPA_KM)


class PedidoCupom(BaseModel):
    posto_id: str


class UsoCupom(BaseModel):
    codigo: str
    posto_id: str
    valor: float = Field(gt=0, le=2000)


def _motorista_verde(mid: str) -> tuple[dict, dict]:
    m = banco.usuario(mid, "motorista")
    if not m:
        raise HTTPException(404, "Cadastro de motorista não encontrado.")
    desde = agora() - 30 * 86400
    n = sum(1 for c in banco.corridas()
            if c.get("motorista_id") == mid and c["status"] == "concluida" and c.get("concluida_em", 0) >= desde)
    nivel = postos_mod.nivel_por_corridas(n)
    usado = postos.usado_no_mes(mid)
    nota = m.get("nota")
    nivel.update(nota=nota, nota_minima=postos_mod.NOTA_MINIMA,
                 elegivel=nota is None or nota >= postos_mod.NOTA_MINIMA,
                 tipos=postos_mod.tipos_do_veiculo(m.get("modelo", "")), teto_mensal=postos_mod.TETO_MENSAL,
                 usado_no_mes=usado, saldo=round(max(0.0, postos_mod.TETO_MENSAL - usado), 2))
    return m, nivel


@app.get("/api/postos")
def listar_postos(x: float | None = None, y: float | None = None, tipo: str | None = None):
    return postos.listar({"x": x, "y": y} if x is not None and y is not None else None, [tipo] if tipo else None)


@app.post("/api/postos", status_code=201)
def credenciar_posto(p: NovoPosto):
    posto = postos.credenciar(p.nome, p.tipo, p.x, p.y)
    banco.evento(None, "info", f"Central credenciou o posto {p.nome} ({posto['tipo_texto']}).")
    return posto


@app.delete("/api/postos/{pid}")
def descredenciar_posto(pid: str):
    if not postos.descredenciar(pid):
        raise HTTPException(404, "Posto não encontrado.")
    return {"ok": True}


@app.get("/api/motoristas/{mid}/motora-verde")
def motora_verde(mid: str, x: float | None = None, y: float | None = None):
    m, nivel = _motorista_verde(mid)
    perto = {"x": x, "y": y} if x is not None and y is not None else None
    return {**nivel, "postos": postos.listar(perto, nivel["tipos"]), "historico": postos.historico(mid)[:10]}


@app.post("/api/motoristas/{mid}/motora-verde/cupom", status_code=201)
def gerar_cupom(mid: str, pc: PedidoCupom):
    m, nivel = _motorista_verde(mid)
    if m.get("status") == "suspensa":
        raise HTTPException(403, "Conta suspensa: o desconto volta quando a central reativar a conta.")
    if not nivel["elegivel"]:
        raise HTTPException(403, f"O Motora Verde é só para quem tem nota 4 ou 5 estrelas. Sua nota hoje é "
                                 f"{nivel['nota']:.1f}.".replace(".", ",", 1))
    posto = postos.obter(pc.posto_id)
    if not posto:
        raise HTTPException(404, "Posto não credenciado.")
    if posto["tipo"] not in nivel["tipos"]:
        raise HTTPException(422, f"Seu veículo não usa {posto['tipo_texto'].lower()}. O desconto vale só para "
                                 "o combustível limpo do seu veículo.")
    if nivel["saldo"] <= 0:
        raise HTTPException(422, "Você já usou todo o desconto deste mês.")
    return postos.gerar_cupom(mid, posto, nivel)


@app.post("/api/postos/cupom")
def usar_cupom(u: UsoCupom):
    """Chamado pelo posto credenciado ao receber o cupom."""
    try:
        return postos.usar_cupom(u.codigo, u.posto_id, u.valor)
    except ValueError as e:
        raise HTTPException(422, str(e))


# ---------- mensagens entre motorista e passageira ----------
class Mensagem(BaseModel):
    por: str = Field(pattern="^(passageira|motorista)$")
    texto: str = Field(min_length=1, max_length=300)


_TELEFONE = re.compile(r"(?:\+?\d[\s().-]?){8,}")


@app.post("/api/corridas/{cid}/mensagens", status_code=201)
def enviar_mensagem(cid: str, msg: Mensagem):
    """Chat da corrida. Números de telefone são ocultados: a conversa fica dentro do app, onde a central
    pode consultar em caso de denúncia."""
    c = _corrida(cid)
    if c["status"] in ("concluida", "cancelada"):
        raise HTTPException(409, "O chat foi encerrado com o fim da corrida. Para falar sobre esta corrida, "
                                 "abra uma reclamação para a central.")
    _status(c, "a_caminho", "no_embarque", "em_viagem")
    texto = _TELEFONE.sub("[número oculto]", msg.texto.strip())
    m = {"id": uuid.uuid4().hex[:8], "por": msg.por, "texto": texto, "quando": agora(),
         "oculto": texto != msg.texto.strip()}
    c.setdefault("mensagens", []).append(m)
    banco.salvar_corrida(c)
    return m


@app.get("/api/corridas/{cid}/mensagens")
def listar_mensagens(cid: str, desde: float = 0):
    return [m for m in _corrida(cid).get("mensagens", []) if m["quando"] > desde]


# ---------- prova de vida e bem-estar de quem dirige ----------
class ProvaVida(BaseModel):
    selfie: str = Field(max_length=2_800_000)


class BemEstar(BaseModel):
    resposta: str = Field(pattern="^(ok|ajuda|policia|assalto|sem_resposta)$")
    parado_seg: float = Field(default=0, ge=0)
    corrida_id: str | None = None
    motivo: str = Field(default="parada", pattern="^(parada|bluetooth)$")


class Conexao(BaseModel):
    """O app de quem dirige avisa quando o celular conecta ou desconecta do Bluetooth do carro."""
    bluetooth: bool
    bateria: int | None = Field(default=None, ge=0, le=100)
    corrida_id: str | None = None


def _motorista(mid: str) -> dict:
    m = banco.usuario(mid, "motorista")
    if not m:
        raise HTTPException(404, "Cadastro de motorista não encontrado.")
    return m


@app.get("/api/motoristas/{mid}/prova-de-vida")
def ver_prova_vida(mid: str):
    return vida.estado(_motorista(mid), agora())


@app.post("/api/motoristas/{mid}/prova-de-vida")
def prova_vida(mid: str, p: ProvaVida):
    m = _motorista(mid)
    try:
        st = vida.comprovar(m, p.selfie, agora())
    except ValueError as e:
        raise HTTPException(422, str(e))
    banco.salvar_usuario(mid, "motorista", m)
    banco.evento(None, "info", f"Prova de vida de {m['nome']} confirmada. Vale {st['validade_min']} min.")
    return st


@app.post("/api/motoristas/{mid}/bem-estar")
def bem_estar(mid: str, b: BemEstar, request: Request):
    """Resposta à pergunta "Está tudo bem?" feita depois de uma parada longa."""
    m = _motorista(mid)
    minutos = round(b.parado_seg / 60)
    texto = (f"Parada longa de {m['nome']} ({minutos} min): {vida.RESPOSTAS[b.resposta]}." if b.motivo == "parada" else
             f"Celular de {m['nome']} desconectou do Bluetooth do carro: {vida.RESPOSTAS[b.resposta]}.")
    out = {"resposta": b.resposta}
    if b.resposta == "ok":
        banco.evento(b.corrida_id, "info", texto)
    elif b.resposta == "ajuda" and b.corrida_id:
        out["apoio"] = pedir_apoio(b.corrida_id, Apoio(por="motorista", motivo="outro", detalhe="Parada longa: pediu ajuda"))
    elif b.resposta == "assalto" and b.corrida_id:
        out["sos"] = sos(b.corrida_id, Sos(por="motorista", modo="assalto"), request)
    else:
        banco.evento(b.corrida_id, "sos", texto + (" A central deve ligar agora." if b.resposta == "sem_resposta" else
                                                   " Acompanhar a localização." if b.resposta == "policia" else
                                                   " Acionar a polícia sem ligar para o aparelho." if b.resposta == "assalto" else ""))
    return out


@app.post("/api/motoristas/{mid}/conexao")
def conexao(mid: str, c: Conexao):
    """Mudança repentina de cenário: o celular se afastou do carro ou perdeu a conexão Bluetooth.
    O app pergunta se está tudo bem; sem resposta, ele chama /bem-estar com sem_resposta."""
    m = _motorista(mid)
    antes = m.get("conexao", {}).get("bluetooth", True)
    m["conexao"] = {"bluetooth": c.bluetooth, "bateria": c.bateria, "quando": agora()}
    banco.salvar_usuario(mid, "motorista", m)
    if antes and not c.bluetooth:
        banco.evento(c.corrida_id, "desvio", f"Celular de {m['nome']} desconectou do Bluetooth do carro. "
                                             "Perguntamos se está tudo bem.")
    elif not antes and c.bluetooth:
        banco.evento(c.corrida_id, "info", f"Celular de {m['nome']} reconectou ao Bluetooth do carro.")
    if c.bateria is not None and c.bateria <= 15:
        banco.evento(c.corrida_id, "desvio", f"Bateria do celular de {m['nome']} em {c.bateria}%.")
    return m["conexao"]


# ---------- vencimento da CNH e do CRLV ----------
def _docs(m: dict) -> dict:
    return documentos.situacao(m, datetime.now(FUSO).date())


class Documentos(BaseModel):
    cnh_validade: date | None = None
    crlv_validade: date | None = None
    arquivo: str | None = Field(default=None, description="data URL do documento renovado")
    bluetooth_tipo: str | None = Field(default=None, pattern="^(multimidia|adaptador|nenhum)$")
    bluetooth_nome: str | None = Field(default=None, max_length=60)


@app.get("/api/motoristas/{mid}/documentos")
def ver_documentos(mid: str):
    m = banco.usuario(mid, "motorista")
    if not m:
        raise HTTPException(404, "Cadastro de motorista não encontrado.")
    return _docs(m)


@app.put("/api/motoristas/{mid}/documentos")
def renovar_documentos(mid: str, d: Documentos):
    m = banco.usuario(mid, "motorista")
    if not m:
        raise HTTPException(404, "Cadastro de motorista não encontrado.")
    hoje = datetime.now(FUSO).date()
    for campo in ("cnh_validade", "crlv_validade"):
        v = getattr(d, campo)
        if v is not None:
            if v < hoje:
                raise HTTPException(422, f"A nova validade da {documentos.NOMES[campo]} já passou.")
            m[campo] = v.isoformat()
    if d.bluetooth_tipo:
        if d.bluetooth_tipo != "nenhum" and not (d.bluetooth_nome or "").strip():
            raise HTTPException(422, "Informe o nome do Bluetooth do veículo (como aparece no celular).")
        m.update(bluetooth_tipo=d.bluetooth_tipo, bluetooth_nome=d.bluetooth_nome)
    banco.salvar_usuario(mid, "motorista", m)
    banco.evento(None, "info", f"{m['nome']} enviou documento renovado.")
    return _docs(m)


@app.get("/api/documentos/vencimentos")
def vencimentos():
    """Para a central: quem dirige com documento vencido ou vencendo em até 30 dias."""
    out = []
    for m in banco.usuarios("motorista"):
        st = _docs(m)
        if st["vencidos"] or st["avisos"]:
            out.append({"id": m["id"], "nome": m["nome"], **st})
    return out


# ---------- jornada: até 12 h de trabalho, 6 h de descanso ----------
class Disponibilidade(BaseModel):
    online: bool


@app.post("/api/motoristas/{mid}/disponibilidade")
def disponibilidade(mid: str, d: Disponibilidade):
    m = banco.usuario(mid, "motorista")
    if not m:
        raise HTTPException(404, "Cadastro de motorista não encontrado.")
    if d.online and not _docs(m)["pode_dirigir"]:
        raise HTTPException(403, _docs(m)["mensagem"])
    if d.online and not vida.estado(m, agora())["valida"]:
        raise HTTPException(403, "Faça a prova de vida (selfie) para ficar disponível.")
    if not d.online and m.get("online", True):   # o app abre disponível após o cadastro
        vida.registrar_saida(m, agora())
    try:
        st = jornada.ficar_online(m, d.online, agora())
    except ValueError as e:
        banco.salvar_usuario(mid, "motorista", m)
        raise HTTPException(403, str(e))
    banco.salvar_usuario(mid, "motorista", m)
    return st


@app.get("/api/motoristas/{mid}/jornada")
def ver_jornada(mid: str):
    m = banco.usuario(mid, "motorista")
    if not m:
        raise HTTPException(404, "Cadastro de motorista não encontrado.")
    st = jornada.atualizar(m, agora())
    banco.salvar_usuario(mid, "motorista", m)
    return st


# ---------- ocorrências de trânsito e melhor rota ----------
class NovaOcorrencia(BaseModel):
    tipo: str
    x: float = Field(ge=0, le=regras.MAPA_KM)
    y: float = Field(ge=0, le=regras.MAPA_KM)
    por: str = Field(default="motorista", pattern="^(motorista|central)$")
    por_id: str | None = None
    obs: str | None = Field(default=None, max_length=200)


class Trajeto(BaseModel):
    origem: XY
    destino: XY


@app.get("/api/ocorrencias")
def listar_ocorrencias():
    return {"tipos": trafego.TIPOS, "ocorrencias": ocorr.ativas(agora())}


@app.post("/api/ocorrencias", status_code=201)
def nova_ocorrencia(n: NovaOcorrencia):
    if n.tipo not in trafego.TIPOS:
        raise HTTPException(422, "Tipo de ocorrência inválido.")
    nome = "Central"
    if n.por == "motorista":
        m = banco.usuario(n.por_id or "", "motorista")
        if not m:
            raise HTTPException(404, "Motorista não encontrado(a).")
        nome = m["nome"]
    o = ocorr.criar(n.tipo, n.x, n.y, n.por, n.por_id, nome, n.obs, agora())
    banco.evento(None, "desvio" if n.tipo in ("acidente", "blitz", "policiamento") else "info",
                 f"{nome} alertou a central: {o['tipo_texto'].lower()} em {o['regiao']}" + (f" ({n.obs})" if n.obs else "") + ".")
    return o


@app.delete("/api/ocorrencias/{oid}")
def remover_ocorrencia(oid: str):
    if not ocorr.remover(oid):
        raise HTTPException(404, "Ocorrência não encontrada.")
    return {"ok": True}


@app.post("/api/rota/melhor")
def rota_melhor(t: Trajeto):
    return trafego.melhor_rota(t.origem.model_dump(), t.destino.model_dump(), ocorr.ativas(agora()))


# ---------- motoristas favoritas ----------
class Favorito(BaseModel):
    motorista_id: str


@app.post("/api/passageiras/{pid}/favoritos", status_code=201)
def favoritar(pid: str, f: Favorito):
    """A passageira favorita quem dirigiu com excelência: precisa ter dado 5 estrelas em conduta numa corrida com essa pessoa."""
    p, m = banco.usuario(pid, "passageira"), banco.usuario(f.motorista_id, "motorista")
    if not p or not m:
        raise HTTPException(404, "Cadastro não encontrado.")
    if f.motorista_id in avaliacoes.bloqueados(p) or pid in avaliacoes.bloqueados(m):
        raise HTTPException(409, "Vocês estão bloqueados e não podem ser pareados.")
    excelente = any(c.get("passageira_id") == pid and c.get("motorista_id") == f.motorista_id
                    and ((c.get("avaliacoes") or {}).get("passageira") or {}).get("notas", {}).get("conduta") == 5
                    for c in banco.corridas())
    if not excelente:
        raise HTTPException(422, "Para favoritar, avalie a conduta desta pessoa motorista com 5 estrelas em uma corrida.")
    p["favoritos"] = sorted(set(p.get("favoritos", [])) | {f.motorista_id})
    banco.salvar_usuario(pid, "passageira", p)
    banco.evento(None, "info", f"{p['nome']} marcou {m['nome']} como motorista favorita.")
    return {"favoritos": p["favoritos"]}


def _desfavoritar(pid: str, mid: str, por: str) -> dict:
    p, m = banco.usuario(pid, "passageira"), banco.usuario(mid, "motorista")
    if not p or not m or mid not in p.get("favoritos", []):
        raise HTTPException(404, "Favorito não encontrado.")
    p["favoritos"] = [x for x in p["favoritos"] if x != mid]
    banco.salvar_usuario(pid, "passageira", p)
    banco.evento(None, "info", f"{p['nome'] if por == 'passageira' else m['nome']} excluiu o favorito entre {p['nome']} e {m['nome']}.")
    return {"favoritos": p["favoritos"]}


@app.delete("/api/passageiras/{pid}/favoritos/{mid}")
def desfavoritar_pela_passageira(pid: str, mid: str):
    return _desfavoritar(pid, mid, "passageira")


@app.delete("/api/motoristas/{mid}/favoritada-por/{pid}")
def desfavoritar_pela_motorista(mid: str, pid: str):
    return _desfavoritar(pid, mid, "motorista")


@app.get("/api/passageiras/{pid}/favoritos")
def ver_favoritos(pid: str):
    p = banco.usuario(pid, "passageira")
    if not p:
        raise HTTPException(404, "Passageira não encontrada.")
    return [{"id": mid, **_publico(banco.usuario(mid) or {})} for mid in p.get("favoritos", []) if banco.usuario(mid)]


@app.get("/api/motoristas/{mid}/favoritada-por")
def favoritada_por(mid: str):
    return [{"id": p["id"], "nome": p.get("nome", "").split(" ")[0]} for p in banco.usuarios("passageira")
            if mid in p.get("favoritos", [])]


# ---------- corridas agendadas ----------
def _faltam(c: dict) -> float:
    """Segundos até o embarque. No modo demo, o relógio da corrida agendada anda FATOR_TEMPO vezes mais rápido."""
    return (c["agendada_para"] - c["criada_em"]) - (agora() - c["criada_em"]) * FATOR_TEMPO


def _prazo(c: dict, pos: dict | None) -> dict:
    falta = _faltam(c)
    desloc = trafego.melhor_rota(pos, c["origem"], ocorr.ativas(agora())) if pos else None
    desloc_seg = desloc["minutos"] * 60 if desloc else 0
    sair = falta - desloc_seg - MARGEM_SAIDA_SEG
    alerta = "atrasado" if falta < desloc_seg else "hora_de_sair" if sair <= 0 else "no_prazo"
    obs = list(desloc["observacoes"]) if desloc else []
    if desloc and desloc["atraso_min"]:
        obs.insert(0, f"Trânsito até o embarque: +{desloc['atraso_min']} min. Saia mais cedo.")
    return {"faltam_seg": round(falta), "deslocamento_min": round(desloc_seg / 60), "sair_em_seg": round(sair),
            "atraso_transito_min": desloc["atraso_min"] if desloc else 0, "alerta": alerta, "observacoes": obs,
            "embarque": _hora_txt(c["agendada_para"])}


@app.get("/api/agendamentos")
def agendamentos(motorista_id: str | None = None, passageira_id: str | None = None):
    out = []
    for c in banco.corridas():
        if c["status"] != "agendada":
            continue
        if passageira_id and c["passageira_id"] != passageira_id:
            continue
        if motorista_id:
            minha = c.get("motorista_id") == motorista_id
            aberta = not c.get("motorista_id") and c.get("motorista_preferido") in (None, motorista_id) \
                and motorista_id not in c.get("recusada_por", [])
            if not (minha or aberta):
                continue
        v = _visao(c) | {"pin": None if motorista_id else c["pin"]}
        if c.get("motorista_id"):
            m = banco.usuario(c["motorista_id"]) or {}
            v["prazo"] = _prazo(c, m.get("posicao_atual"))
        out.append(v)
    return sorted(out, key=lambda c: c["agendada_para"])


@app.post("/api/corridas/{cid}/agendamento/aceitar")
def aceitar_agendamento(cid: str, a: Aceite):
    c = _corrida(cid)
    _status(c, "agendada")
    if c.get("motorista_id"):
        raise HTTPException(409, "Esta corrida agendada já tem motorista.")
    if c.get("motorista_preferido") not in (None, a.motorista_id):
        raise HTTPException(403, "A passageira escolheu outra pessoa motorista para esta corrida.")
    m = _pode_aceitar(c, a.motorista_id)
    if a.posicao:
        m["posicao_atual"] = a.posicao.model_dump()
        banco.salvar_usuario(m["id"], "motorista", m)
    c.update(motorista_id=a.motorista_id, confirmada_em=agora())
    banco.salvar_corrida(c)
    p = banco.usuario(c["passageira_id"]) or {}
    notificacoes.enviar(p.get("telefone", ""), f"Motora: {m['nome']} confirmou sua corrida agendada para {_hora_txt(c['agendada_para'])}.")
    banco.evento(cid, "info", f"{m['nome']} confirmou a corrida agendada para {_hora_txt(c['agendada_para'])}.")
    return _visao(c) | {"prazo": _prazo(c, a.posicao.model_dump() if a.posicao else None)}


@app.get("/api/corridas/{cid}/prazo")
def prazo_agendamento(cid: str, x: float | None = None, y: float | None = None):
    """Alerta de cumprimento do prazo para quem dirige, com o trânsito até o embarque."""
    c = _corrida(cid)
    _status(c, "agendada")
    pos = {"x": x, "y": y} if x is not None and y is not None else None
    p = _prazo(c, pos)
    if c.get("motorista_id") and p["alerta"] == "atrasado" and not c.get("aviso_atraso"):
        c["aviso_atraso"] = agora()
        banco.salvar_corrida(c)
        m = banco.usuario(c["motorista_id"]) or {}
        banco.evento(cid, "desvio", f"{m.get('nome')} não vai chegar a tempo na corrida agendada para "
                                    f"{_hora_txt(c['agendada_para'])}. Central deve acompanhar.")
        pas = banco.usuario(c["passageira_id"]) or {}
        notificacoes.enviar(pas.get("telefone", ""), "Motora: a pessoa motorista pode se atrasar para a sua corrida agendada.")
    return p


@app.post("/api/corridas/{cid}/agendamento/iniciar")
def iniciar_agendamento(cid: str, a: Aceite):
    """Quem dirige sai para buscar a passageira: a corrida agendada vira uma corrida a caminho."""
    c = _corrida(cid)
    _status(c, "agendada")
    if c.get("motorista_id") != a.motorista_id:
        raise HTTPException(403, "Esta corrida agendada não é sua.")
    m = _pode_aceitar(c, a.motorista_id)
    pos = a.posicao.model_dump() if a.posicao else None
    c.update(status="a_caminho", aceita_em=agora(), posicao=pos, posicao_aceite=pos,
             motorista_favorito=a.motorista_id in (banco.usuario(c["passageira_id"]) or {}).get("favoritos", []))
    banco.salvar_corrida(c)
    _ajusta_chamadas(c["regiao"], +1)
    banco.evento(cid, "info", f"{m['nome']} saiu para buscar a passageira da corrida agendada.")
    return _visao(c)


# ---------- reclamações à central ----------
class NovaReclamacao(BaseModel):
    por: str = Field(pattern="^(passageira|motorista)$")
    autor_id: str
    categoria: str
    descricao: str = Field(min_length=5, max_length=1000)
    corrida_id: str | None = None
    objeto_descricao: str | None = Field(default=None, max_length=200)
    entrega: str = Field(default="retirar", pattern="^(retirar|destino|embarque)$")


class Andamento(BaseModel):
    status: str
    resposta: str | None = Field(default=None, max_length=500)


@app.get("/api/reclamacoes/regras")
def regras_reclamacoes():
    return {"categorias": reclamacoes.CATEGORIAS, "status": reclamacoes.STATUS, "aviso_objetos": reclamacoes.AVISO_OBJETOS,
            "entrega": {"base": reclamacoes.ENTREGA_BASE, "km_base": reclamacoes.ENTREGA_KM_BASE,
                        "por_km": reclamacoes.ENTREGA_POR_KM, "maximo": reclamacoes.ENTREGA_MAX}}


@app.post("/api/reclamacoes", status_code=201)
def abrir_reclamacao(n: NovaReclamacao):
    u = banco.usuario(n.autor_id, n.por)
    if not u:
        raise HTTPException(404, "Cadastro não encontrado.")
    if n.categoria not in reclamacoes.CATEGORIAS[n.por]:
        raise HTTPException(422, "Escolha o assunto da reclamação.")
    c = None
    if n.corrida_id:
        c = _corrida(n.corrida_id)
        if n.autor_id not in (c.get("passageira_id"), c.get("motorista_id")):
            raise HTTPException(403, "Esta corrida não é sua.")
    objeto = None
    if n.categoria in reclamacoes.OBJETO:
        if not c:
            raise HTTPException(422, "Escolha a corrida em que o objeto ficou no carro.")
        if not (n.objeto_descricao or "").strip():
            raise HTTPException(422, "Descreva o objeto.")
        objeto = {"descricao": n.objeto_descricao.strip(), "entrega": n.entrega if n.por == "passageira" else "retirar"}
        if objeto["entrega"] != "retirar":
            # distância do destino da corrida (onde quem dirige deixou a passageira) até o endereço de entrega
            ponto = c["destino"] if objeto["entrega"] == "destino" else c["origem"]
            km = round(regras.dist(c["destino"], ponto), 1)
            objeto.update(km=km, taxa=reclamacoes.taxa_entrega(km))
    r = recl.criar(por=n.por, autor_id=n.autor_id, autor_nome=u.get("nome", ""), categoria=n.categoria,
                   descricao=n.descricao.strip(), corrida_id=n.corrida_id, objeto=objeto)
    banco.evento(n.corrida_id, "desvio" if n.categoria not in reclamacoes.OBJETO else "info",
                 f"Reclamação {r['protocolo']} aberta por {u.get('nome')} ({n.por}): {r['categoria_texto']}.")
    notificacoes.enviar(u.get("telefone", ""), f"Motora: recebemos sua reclamação. Protocolo {r['protocolo']}."
                        + (f" {reclamacoes.AVISO_OBJETOS}" if objeto else ""))
    return r


@app.get("/api/reclamacoes")
def listar_reclamacoes(autor_id: str | None = None):
    return recl.listar(autor_id)


@app.post("/api/reclamacoes/{rid}/andamento")
def andamento_reclamacao(rid: str, a: Andamento):
    """Central atualiza a reclamação. Na entrega do objeto, a taxa é cobrada da passageira e vai 100% para quem dirige."""
    r = recl.obter(rid)
    if not r:
        raise HTTPException(404, "Reclamação não encontrada.")
    if a.status not in reclamacoes.STATUS:
        raise HTTPException(422, "Situação inválida.")
    if r["status"] in reclamacoes.FINAIS:
        raise HTTPException(409, "Esta reclamação já foi encerrada.")
    obj = r.get("objeto")
    if a.status in ("objeto_encontrado", "objeto_nao_encontrado", "entregue") and not obj:
        raise HTTPException(409, "Esta reclamação não é de objeto esquecido.")
    if a.status == "entregue" and obj.get("taxa"):
        c = _corrida(r["corrida_id"])
        pg = _cobrar(c, obj["taxa"], "entrega-objeto", sem_taxa=True)
        obj["pagamento"] = pg["status"]
    r["status"] = a.status
    r["historico"].append({"status": a.status, "resposta": a.resposta, "quando": agora()})
    if a.resposta:
        r["resposta"] = a.resposta
    recl.salvar(r)
    u = banco.usuario(r["autor_id"]) or {}
    notificacoes.enviar(u.get("telefone", ""), f"Motora: reclamação {r['protocolo']}: {reclamacoes.STATUS[a.status]}."
                        + (f" {a.resposta}" if a.resposta else ""))
    banco.evento(r.get("corrida_id"), "info", f"Reclamação {r['protocolo']}: {reclamacoes.STATUS[a.status]}.")
    return r


@app.get("/api/eventos")
def eventos():
    return banco.eventos()


# ---------- acompanhamento pelo contato de emergência ----------
@app.get("/api/acompanhar/{token}")
def acompanhar_dados(token: str):
    c = banco.corrida_por_token(token)
    if not c:
        raise HTTPException(404, "Link inválido ou expirado.")
    if c["status"] in ("concluida", "cancelada"):
        raise HTTPException(410, "Esta corrida terminou e o link de acompanhamento foi encerrado.")
    p = banco.usuario(c["passageira_id"]) or {}
    m = banco.usuario(c["motorista_id"]) or {} if c.get("motorista_id") else {}
    restante = regras.dist(c.get("posicao") or c["origem"], c["destino"])
    return {
        "status": c["status"], "passageira": p.get("nome", "").split(" ")[0],
        "motorista": {"nome": m.get("nome"), "foto": m.get("foto"), "modelo": m.get("modelo"),
                      "cor": m.get("cor"), "placa": m.get("placa")},
        "origem": c["origem"], "destino": c["destino"], "rota": c["rota"], "posicao": c.get("posicao"),
        "alerta_desvio": c["alerta_desvio"], "parada": bool(c.get("parada_desde")), "sos": bool(c.get("sos")),
        "zona_sinal": (c.get("alerta_sinal") or {}).get("mensagem"),
        "sinal_fraco": any(v.get("qualidade") != "bom" for v in _sinal(c).values()),
        "atraso_posicao_seg": _sinal(c).get("motorista", {}).get("atraso_posicao_seg"),
        "chegada_min": round(restante / regras.VELOCIDADE_MEDIA * 60), "mapa_km": regras.MAPA_KM,
        "geo": {"posicao": regras.latlng(c.get("posicao") or c["origem"]), "destino": regras.latlng(c["destino"])},
    }


@app.get("/acompanhar/{token}", include_in_schema=False)
def acompanhar_pagina(token: str):
    return FileResponse(STATIC / "acompanhar.html")


@app.get("/", include_in_schema=False)
def inicio():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
