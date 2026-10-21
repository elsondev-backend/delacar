"""Prova de vida de quem dirige e checagem de bem-estar em paradas longas.

Prova de vida: selfie ao ficar disponível, comparada com a foto do cadastro. Vale 1 h.
Cada nova saída do app (ficar offline) nas últimas 24 h encurta a validade da próxima:
1 saída -> 60 min, 2 -> 45, 3 -> 30, 4 ou mais -> 15 (mínimo).
Com GPS e câmera reais, troque a checagem simulada por um serviço de reconhecimento facial com
detecção de vivacidade (piscar, virar o rosto).

Parada longa: se quem dirige fica parada(o) mais que PARADO_ALERTA_SEG, o app pergunta se está
tudo bem; sem resposta em SEM_RESPOSTA_SEG, a central é avisada.
"""
from __future__ import annotations

VALIDADES_MIN = [60, 45, 30, 15]
JANELA_SAIDAS_SEG = 24 * 3600
PARADO_ALERTA_SEG = 10 * 60
SEM_RESPOSTA_SEG = 2 * 60
RESPOSTAS = {"ok": "Está tudo bem", "ajuda": "Pediu apoio da central", "policia": "Ligou para a polícia (190)",
             "assalto": "Acionou o Modo assalto", "sem_resposta": "Não respondeu"}


def _v(m: dict) -> dict:
    return m.setdefault("vida", {"valida_ate": None, "saidas": [], "ultima": None})


def saidas_recentes(m: dict, agora: float) -> int:
    v = _v(m)
    v["saidas"] = [t for t in v["saidas"] if agora - t < JANELA_SAIDAS_SEG]
    return len(v["saidas"])


def validade_min(m: dict, agora: float) -> int:
    n = saidas_recentes(m, agora)
    return VALIDADES_MIN[min(max(n - 1, 0), len(VALIDADES_MIN) - 1)]


def registrar_saida(m: dict, agora: float) -> None:
    v = _v(m)
    v["saidas"].append(agora)
    v["valida_ate"] = None          # voltar ao app pede nova prova de vida


def estado(m: dict, agora: float) -> dict:
    v = _v(m)
    valida = bool(v["valida_ate"] and agora < v["valida_ate"])
    return {"valida": valida, "restante_seg": round(v["valida_ate"] - agora) if valida else 0,
            "proxima_validade_min": validade_min(m, agora), "saidas_24h": saidas_recentes(m, agora),
            "ultima": v["ultima"]}


def comprovar(m: dict, selfie: str, agora: float) -> dict:
    """selfie=None só no cadastro, que já passa pela verificação facial."""
    if selfie is not None and not selfie.startswith("data:image"):
        raise ValueError("Envie uma selfie tirada agora pela câmera.")
    # protótipo: o reconhecimento facial é simulado e sempre confere com a foto do cadastro
    v = _v(m)
    minutos = validade_min(m, agora)
    v.update(valida_ate=agora + minutos * 60, ultima=agora)
    return {**estado(m, agora), "validade_min": minutos, "confere_com_cadastro": True}
