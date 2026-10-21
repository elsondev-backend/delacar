"""Ocorrências de trânsito avisadas por quem dirige (ou pela central) e escolha da melhor rota.

Acidente, obra e engarrafamento atrasam quem passa a até 1 km deles, e a rota é escolhida pelo menor
tempo estimado. Policiamento e blitz não atrasam: aparecem como observação no trajeto.
"""
from __future__ import annotations

import json
import time
import uuid

from . import regras

TIPOS = {
    "acidente": {"nome": "Acidente", "atraso_min": 15, "validade_h": 2},
    "obra": {"nome": "Obra", "atraso_min": 10, "validade_h": 24 * 7},
    "engarrafamento": {"nome": "Engarrafamento", "atraso_min": 20, "validade_h": 1},
    "policiamento": {"nome": "Policiamento", "atraso_min": 0, "validade_h": 2},
    "blitz": {"nome": "Blitz", "atraso_min": 0, "validade_h": 3},
}
RAIO_KM = 1.0
DESVIO_KM = 6          # quanto as rotas alternativas se afastam do trajeto direto


def _candidatas(a: dict, b: dict) -> list[list[dict]]:
    a, b = {"x": a["x"], "y": a["y"]}, {"x": b["x"], "y": b["y"]}
    rotas = [[a, {"x": b["x"], "y": a["y"]}, b], [a, {"x": a["x"], "y": b["y"]}, b]]
    lo_y, hi_y = min(a["y"], b["y"]), max(a["y"], b["y"])
    lo_x, hi_x = min(a["x"], b["x"]), max(a["x"], b["x"])
    for y in ((lo_y + hi_y) / 2, lo_y - DESVIO_KM, hi_y + DESVIO_KM):
        y = max(0, min(regras.MAPA_KM, y))
        rotas.append([a, {"x": a["x"], "y": y}, {"x": b["x"], "y": y}, b])
    for x in ((lo_x + hi_x) / 2, lo_x - DESVIO_KM, hi_x + DESVIO_KM):
        x = max(0, min(regras.MAPA_KM, x))
        rotas.append([a, {"x": x, "y": a["y"]}, {"x": x, "y": b["y"]}, b])
    return rotas


def comprimento(rota: list[dict]) -> float:
    return sum(regras.dist(p, q) for p, q in zip(rota, rota[1:]))


def no_trajeto(rota: list[dict], ocorrencias: list[dict]) -> list[dict]:
    return [o for o in ocorrencias if regras.distancia_da_rota(o, rota) <= RAIO_KM]


def melhor_rota(a: dict, b: dict, ocorrencias: list[dict] | None = None) -> dict:
    """Menor tempo estimado entre as rotas candidatas; empate fica com a mais curta (e a primeira, a rota padrão)."""
    ocorrencias = ocorrencias or []
    melhor = None
    padrao = None
    for rota in _candidatas(a, b):
        km = comprimento(rota)
        perto = no_trajeto(rota, ocorrencias)
        atraso = sum(TIPOS[o["tipo"]]["atraso_min"] for o in perto)
        minutos = km / regras.VELOCIDADE_MEDIA * 60 + atraso
        item = {"rota": rota, "km": round(km, 2), "minutos": minutos, "atraso_min": atraso, "ocorrencias": perto}
        padrao = padrao or item
        if melhor is None or (round(minutos, 2), km) < (round(melhor["minutos"], 2), melhor["km"]):
            melhor = item
    evitadas = [o for o in padrao["ocorrencias"] if o not in melhor["ocorrencias"]]
    obs = [f"{TIPOS[o['tipo']]['nome']} no trajeto" + (f" (+{TIPOS[o['tipo']]['atraso_min']} min)" if TIPOS[o['tipo']]['atraso_min'] else "")
           for o in melhor["ocorrencias"]]
    if evitadas:
        obs.insert(0, "Rota desviada para evitar: " + ", ".join(TIPOS[o["tipo"]]["nome"].lower() for o in evitadas)
                   + f" ({'mesma distância' if melhor['km'] - padrao['km'] < 0.05 else f'+{melhor["km"] - padrao["km"]:.1f} km'}, "
                   f"{padrao['minutos'] - melhor['minutos']:.0f} min a menos)")
    return {**melhor, "evitadas": evitadas, "observacoes": obs}


class Ocorrencias:
    def __init__(self, banco):
        self.banco = banco
        banco.con.execute("create table if not exists ocorrencias (id text primary key, dados text)")
        banco.con.commit()

    def criar(self, tipo: str, x: float, y: float, por: str, por_id: str | None, por_nome: str,
              obs: str | None, agora: float) -> dict:
        o = {"id": "oc-" + uuid.uuid4().hex[:8], "tipo": tipo, "tipo_texto": TIPOS[tipo]["nome"], "x": x, "y": y,
             "regiao": regras.regiao_de({"x": x, "y": y}), "por": por, "por_id": por_id, "por_nome": por_nome,
             "obs": obs, "criada_em": agora, "expira_em": agora + TIPOS[tipo]["validade_h"] * 3600, "confirmacoes": 0}
        self.salvar(o)
        return o

    def salvar(self, o: dict) -> None:
        self.banco.con.execute("insert or replace into ocorrencias values (?, ?)", (o["id"], json.dumps(o)))
        self.banco.con.commit()

    def obter(self, oid: str) -> dict | None:
        row = self.banco.con.execute("select dados from ocorrencias where id = ?", (oid,)).fetchone()
        return json.loads(row[0]) if row else None

    def remover(self, oid: str) -> bool:
        n = self.banco.con.execute("delete from ocorrencias where id = ?", (oid,)).rowcount
        self.banco.con.commit()
        return n > 0

    def ativas(self, agora: float | None = None) -> list[dict]:
        agora = agora or time.time()
        os_ = [json.loads(r[0]) for r in self.banco.con.execute("select dados from ocorrencias").fetchall()]
        return sorted((o for o in os_ if o["expira_em"] > agora), key=lambda o: -o["criada_em"])
