"""Jornada de quem dirige: no máximo 12 h de trabalho e 6 h de descanso para voltar.

Conta como trabalho todo o tempo disponível no app (esperando chamadas ou em corrida).
Ficar offline menos de 6 h é só uma pausa: as horas continuam somando. Depois de 6 h
seguidas offline, a jornada zera. Ao chegar a 12 h, o app desliga a disponibilidade
(a corrida em andamento termina normalmente) e só libera depois de 6 h de descanso.
"""
from __future__ import annotations

MAX_TRABALHO_SEG = 12 * 3600
DESCANSO_SEG = 6 * 3600


def _j(m: dict) -> dict:
    return m.setdefault("jornada", {"trabalhado_seg": 0.0, "online_desde": None, "offline_desde": None,
                                    "descanso_ate": None})


def atualizar(m: dict, agora: float) -> dict:
    """Fecha o tempo corrido até agora e aplica as regras. Devolve o estado público."""
    j = _j(m)
    if j["online_desde"] is not None:
        j["trabalhado_seg"] += agora - j["online_desde"]
        j["online_desde"] = agora
        if j["trabalhado_seg"] >= MAX_TRABALHO_SEG:
            j.update(online_desde=None, offline_desde=agora, descanso_ate=agora + DESCANSO_SEG)
            m["online"] = False
    elif j["offline_desde"] is not None and agora - j["offline_desde"] >= DESCANSO_SEG:
        j.update(trabalhado_seg=0.0, descanso_ate=None)
    if j["descanso_ate"] and agora >= j["descanso_ate"]:
        j.update(descanso_ate=None, trabalhado_seg=0.0)
    return estado(m, agora)


def estado(m: dict, agora: float) -> dict:
    j = _j(m)
    em_descanso = bool(j["descanso_ate"] and agora < j["descanso_ate"])
    return {"online": j["online_desde"] is not None, "trabalhado_seg": round(j["trabalhado_seg"]),
            "restante_seg": round(max(0.0, MAX_TRABALHO_SEG - j["trabalhado_seg"])),
            "em_descanso": em_descanso, "descanso_restante_seg": round(j["descanso_ate"] - agora) if em_descanso else 0,
            "max_trabalho_seg": MAX_TRABALHO_SEG, "descanso_seg": DESCANSO_SEG}


def ficar_online(m: dict, online: bool, agora: float) -> dict:
    st = atualizar(m, agora)
    j = _j(m)
    if online:
        if st["em_descanso"]:
            h, r = divmod(st["descanso_restante_seg"] // 60, 60)
            raise ValueError(f"Descanso obrigatório: você trabalhou 12 horas. Pode voltar a dirigir em {h}h{r:02d}.")
        if j["online_desde"] is None:
            j.update(online_desde=agora, offline_desde=None)
    elif j["online_desde"] is not None:
        j.update(online_desde=None, offline_desde=agora)
    m["online"] = online
    return estado(m, agora)
