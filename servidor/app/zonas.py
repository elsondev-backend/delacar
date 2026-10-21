"""Mapa de zonas de sinal fraco.

Cada aviso de sinal fraco vindo de uma corrida vira um "relato" guardado na célula
do mapa onde aconteceu (quadrados de CELULA_KM). Uma célula vira zona automática
quando junta MIN_RELATOS relatos diferentes (corrida + lado) nos últimos JANELA_DIAS.
A central também pode marcar zonas à mão (centro + raio) e dispensar zonas automáticas.

Com GPS real, troque a célula em km por um geohash de ~500 m (precisão 6/7).
"""
from __future__ import annotations

import math
import os
import time
import uuid

from . import regras

CELULA_KM = 10
JANELA_DIAS = 7
_demo = os.environ.get("MOTORA_MODO_DEMO", "1") == "1"
MIN_RELATOS = int(os.environ.get("MOTORA_ZONA_MIN_RELATOS", "1" if _demo else "3"))


def celula_de(p: dict) -> str:
    return f"{int(p['x'] // CELULA_KM)}:{int(p['y'] // CELULA_KM)}"


def centro_da_celula(cel: str) -> dict:
    i, j = (int(v) for v in cel.split(":"))
    return {"x": (i + 0.5) * CELULA_KM, "y": (j + 0.5) * CELULA_KM}


def celulas_da_rota(rota: list[dict], passo_km: float = 1.0) -> list[str]:
    """Células atravessadas pela rota, na ordem, sem repetir."""
    vistas: list[str] = []
    total = regras.comprimento(rota)
    n = max(1, int(total / passo_km))
    for k in range(n + 1):
        p = _ponto_na_rota(rota, total * k / n)
        cel = celula_de(p)
        if cel not in vistas:
            vistas.append(cel)
    return vistas


def _ponto_na_rota(rota: list[dict], d: float) -> dict:
    for a, b in zip(rota, rota[1:]):
        L = regras.dist(a, b)
        if d <= L or b is rota[-1]:
            f = min(1.0, d / L) if L else 1.0
            return {"x": a["x"] + (b["x"] - a["x"]) * f, "y": a["y"] + (b["y"] - a["y"]) * f}
        d -= L
    return rota[-1]


class Zonas:
    def __init__(self, banco):
        self.banco = banco
        banco.con.executescript(
            """
            create table if not exists sinal_relatos (id integer primary key autoincrement, celula text,
                x real, y real, qualidade text, por text, corrida_id text, quando real);
            create table if not exists zonas_manuais (id text primary key, x real, y real, raio_km real,
                nota text, criada_em real);
            create table if not exists zonas_dispensadas (celula text primary key, quando real);
            """
        )

    # ---------- registro ----------
    def relatar(self, p: dict, qualidade: str, por: str, corrida_id: str) -> None:
        self.banco.con.execute(
            "insert into sinal_relatos (celula, x, y, qualidade, por, corrida_id, quando) values (?,?,?,?,?,?,?)",
            (celula_de(p), p["x"], p["y"], qualidade, por, corrida_id, time.time()))
        self.banco.con.commit()

    # ---------- leitura ----------
    def listar(self) -> list[dict]:
        desde = time.time() - JANELA_DIAS * 86400
        rows = self.banco.con.execute(
            """select celula, count(distinct corrida_id || por), sum(qualidade = 'sem'), max(quando)
               from sinal_relatos where quando >= ? group by celula""", (desde,)).fetchall()
        # zona dispensada pela central volta a aparecer se chegarem relatos novos depois da dispensa
        dispensadas = dict(self.banco.con.execute("select celula, quando from zonas_dispensadas").fetchall())
        zonas = []
        for cel, n, sem, ultima in rows:
            if n >= MIN_RELATOS and not (cel in dispensadas and ultima <= dispensadas[cel]):
                c = centro_da_celula(cel)
                zonas.append({"id": f"auto-{cel}", "tipo": "automatica", "celula": cel, "centro": c,
                              "lado_km": CELULA_KM, "relatos": n, "nivel": "sem sinal" if sem else "fraco",
                              "ultima": ultima, "regiao": regras.regiao_de(c), "nota": None})
        for zid, x, y, raio, nota, criada in self.banco.con.execute(
                "select id, x, y, raio_km, nota, criada_em from zonas_manuais"):
            c = {"x": x, "y": y}
            zonas.append({"id": zid, "tipo": "manual", "centro": c, "raio_km": raio, "nota": nota,
                          "relatos": None, "nivel": "fraco", "ultima": criada, "regiao": regras.regiao_de(c)})
        return sorted(zonas, key=lambda z: -(z["ultima"] or 0))

    def zona_em(self, p: dict, zonas: list[dict] | None = None) -> dict | None:
        cel = celula_de(p)
        for z in zonas if zonas is not None else self.listar():
            if z["tipo"] == "automatica" and z["celula"] == cel:
                return z
            if z["tipo"] == "manual" and math.hypot(p["x"] - z["centro"]["x"], p["y"] - z["centro"]["y"]) <= z["raio_km"]:
                return z
        return None

    def alerta_para(self, rota: list[dict]) -> dict:
        """Quais pontos de uma corrida caem em zona de sinal fraco."""
        zonas = self.listar()
        embarque = self.zona_em(rota[0], zonas)
        destino = self.zona_em(rota[-1], zonas)
        trajeto, vistos = [], set()
        for cel in celulas_da_rota(rota):
            z = self.zona_em(centro_da_celula(cel), zonas)
            if z and z["id"] not in vistos:
                vistos.add(z["id"])
                trajeto.append(z["id"])
        tem = bool(embarque or destino or trajeto)
        partes = [n for n, v in (("no embarque", embarque), ("no destino", destino)) if v]
        if trajeto and not partes:
            partes.append("no trajeto")
        return {"tem_alerta": tem, "embarque": bool(embarque), "destino": bool(destino), "zonas_no_trajeto": trajeto,
                "mensagem": (f"Região com sinal de internet fraco {' e '.join(partes)}. A localização e o horário "
                             "de chegada podem atrasar.") if tem else None}

    # ---------- gestão pela central ----------
    def marcar(self, x: float, y: float, raio_km: float, nota: str | None) -> dict:
        zid = "manual-" + uuid.uuid4().hex[:8]
        self.banco.con.execute("insert into zonas_manuais values (?,?,?,?,?,?)", (zid, x, y, raio_km, nota, time.time()))
        self.banco.con.commit()
        return next(z for z in self.listar() if z["id"] == zid)

    def remover(self, zid: str) -> bool:
        if zid.startswith("auto-"):
            self.banco.con.execute("insert or replace into zonas_dispensadas values (?, ?)", (zid[5:], time.time()))
        else:
            cur = self.banco.con.execute("delete from zonas_manuais where id = ?", (zid,))
            if cur.rowcount == 0:
                return False
        self.banco.con.commit()
        return True
