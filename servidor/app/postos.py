"""Motora Verde: rede credenciada com desconto para quem dirige.

Postos de recarga elétrica e de etanol, borracharias e serviços de bateria.

Fluxo: quem dirige gera um cupom no app para um posto; o posto valida o cupom
informando o valor abastecido e aplica o desconto do nível de quem dirige.
O nível sobe com as corridas concluídas nos últimos 30 dias.
"""
from __future__ import annotations

import json
import math
import os
import random
import string
import time
import uuid

from . import regras

TIPOS = {"recarga": "Recarga elétrica", "etanol": "Etanol", "pneu": "Reparo de pneu", "bateria": "Bateria"}
SERVICOS = ["pneu", "bateria"]   # valem para qualquer veículo da frota
_demo = os.environ.get("MOTORA_MODO_DEMO", "1") == "1"
# (nome, corridas concluídas nos últimos 30 dias, % de desconto); no modo demo os mínimos são menores
NIVEIS = [("Ouro", 3 if _demo else 120, 0.15), ("Prata", 1 if _demo else 60, 0.10), ("Verde", 0, 0.05)]
NOTA_MINIMA = 4.0          # só quem tem 4 ou 5 estrelas participa (conta nova, ainda sem avaliação, entra)
TETO_MENSAL = 300.0          # desconto máximo por mês, em reais
VALIDADE_CUPOM_SEG = 30 * 60

POSTOS_INICIAIS = [
    ("Eletroposto Centro Verde", "recarga", 118, 125), ("Eletroposto Av. Paulista", "recarga", 140, 95),
    ("Posto Etanol Bom Caminho", "etanol", 85, 150), ("Eletroposto Shopping Norte", "recarga", 60, 50),
    ("Posto Etanol Sul", "etanol", 190, 200), ("Eletroposto Leste Rápido", "recarga", 210, 70),
    ("Borracharia 24h Centro", "pneu", 130, 140), ("Borracharia Rota Norte", "pneu", 70, 80),
    ("Baterias Já Oeste", "bateria", 90, 175), ("Centro de Baterias Leste", "bateria", 175, 110),
]


def combustiveis_do_veiculo(modelo: str) -> list[str]:
    """Combustível limpo que o veículo usa: elétrico e plug-in recarregam; híbrido flex usa etanol."""
    if modelo in regras.ELETRICOS:
        return ["recarga"]
    kind = regras.HIBRIDOS.get(modelo, "")
    if "plug-in" in kind:
        return ["recarga"]
    if "flex" in kind:
        return ["etanol"]
    return []


def tipos_do_veiculo(modelo: str) -> list[str]:
    return combustiveis_do_veiculo(modelo) + SERVICOS


def nivel_por_corridas(n: int) -> dict:
    nome, minimo, pct = next(x for x in NIVEIS if n >= x[1])
    prox = next((x for x in reversed(NIVEIS) if x[1] > n), None)
    return {"nivel": nome, "desconto_pct": pct, "corridas_30d": n,
            "proximo": {"nivel": prox[0], "faltam": prox[1] - n, "desconto_pct": prox[2]} if prox else None}


class Postos:
    def __init__(self, banco):
        self.banco = banco
        banco.con.executescript(
            """
            create table if not exists postos (id text primary key, dados text);
            create table if not exists abastecimentos (id text primary key, motorista_id text, quando real, dados text);
            create table if not exists cupons (codigo text primary key, dados text);
            """)
        if not banco.con.execute("select 1 from postos limit 1").fetchone():
            for nome, tipo, x, y in POSTOS_INICIAIS:
                self.credenciar(nome, tipo, x, y)

    # ---------- postos ----------
    def credenciar(self, nome: str, tipo: str, x: float, y: float) -> dict:
        p = {"id": "posto-" + uuid.uuid4().hex[:8], "nome": nome, "tipo": tipo, "tipo_texto": TIPOS[tipo],
             "x": x, "y": y, "regiao": regras.regiao_de({"x": x, "y": y}), "credenciado_em": time.time()}
        self.banco.con.execute("insert into postos values (?, ?)", (p["id"], json.dumps(p)))
        self.banco.con.commit()
        return p

    def descredenciar(self, pid: str) -> bool:
        cur = self.banco.con.execute("delete from postos where id = ?", (pid,))
        self.banco.con.commit()
        return cur.rowcount > 0

    def listar(self, perto: dict | None = None, tipos: list[str] | None = None) -> list[dict]:
        ps = [json.loads(r[0]) for r in self.banco.con.execute("select dados from postos")]
        if tipos is not None:
            ps = [p for p in ps if p["tipo"] in tipos]
        if perto:
            for p in ps:
                p["distancia_km"] = round(math.hypot(p["x"] - perto["x"], p["y"] - perto["y"]), 1)
            ps.sort(key=lambda p: p["distancia_km"])
        return ps

    def obter(self, pid: str) -> dict | None:
        row = self.banco.con.execute("select dados from postos where id = ?", (pid,)).fetchone()
        return json.loads(row[0]) if row else None

    # ---------- saldo ----------
    def usado_no_mes(self, mid: str) -> float:
        desde = time.time() - 30 * 86400
        rows = self.banco.con.execute("select dados from abastecimentos where motorista_id = ? and quando >= ?",
                                      (mid, desde)).fetchall()
        return round(sum(json.loads(r[0])["desconto"] for r in rows), 2)

    def historico(self, mid: str) -> list[dict]:
        rows = self.banco.con.execute("select dados from abastecimentos where motorista_id = ? order by quando desc",
                                      (mid,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---------- cupom ----------
    def gerar_cupom(self, mid: str, posto: dict, nivel: dict) -> dict:
        codigo = "MV-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        c = {"codigo": codigo, "motorista_id": mid, "posto_id": posto["id"], "posto": posto["nome"],
             "tipo": posto["tipo"], "desconto_pct": nivel["desconto_pct"], "nivel": nivel["nivel"],
             "valido_ate": time.time() + VALIDADE_CUPOM_SEG, "usado": False}
        self.banco.con.execute("insert into cupons values (?, ?)", (codigo, json.dumps(c)))
        self.banco.con.commit()
        return c

    def usar_cupom(self, codigo: str, posto_id: str, valor: float) -> dict:
        row = self.banco.con.execute("select dados from cupons where codigo = ?", (codigo.upper(),)).fetchone()
        if not row:
            raise ValueError("Cupom não encontrado.")
        c = json.loads(row[0])
        if c["usado"]:
            raise ValueError("Este cupom já foi usado.")
        if time.time() > c["valido_ate"]:
            raise ValueError("Cupom vencido. Gere outro no app.")
        if c["posto_id"] != posto_id:
            raise ValueError("Este cupom é de outro posto.")
        saldo = max(0.0, TETO_MENSAL - self.usado_no_mes(c["motorista_id"]))
        desconto = round(min(valor * c["desconto_pct"], saldo), 2)
        a = {"id": uuid.uuid4().hex[:10], "codigo": c["codigo"], "posto": c["posto"], "tipo": c["tipo"],
             "valor": round(valor, 2), "desconto": desconto, "pago": round(valor - desconto, 2),
             "desconto_pct": c["desconto_pct"], "nivel": c["nivel"], "quando": time.time(),
             "limitado_pelo_teto": desconto < round(valor * c["desconto_pct"], 2)}
        c["usado"] = True
        self.banco.con.execute("update cupons set dados = ? where codigo = ?", (json.dumps(c), c["codigo"]))
        self.banco.con.execute("insert into abastecimentos values (?, ?, ?, ?)",
                               (a["id"], c["motorista_id"], a["quando"], json.dumps(a)))
        self.banco.con.commit()
        return a
