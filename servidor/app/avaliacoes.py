"""Avaliação mútua, denúncias e bloqueios.

Ao fim da corrida, passageira e quem dirige avaliam uma à outra de 1 a 5 estrelas
em cada critério. Também podem relatar ocorrências e bloquear a outra pessoa, que
deixa de ser pareada com ela. Ocorrências graves suspendem a conta denunciada até
a central analisar.
"""
from __future__ import annotations

import json
import time
import uuid

CRITERIOS = {
    # quem avalia -> critérios sobre a outra pessoa
    "passageira": {"conduta": "Conduta", "tratamento": "Tratamento", "respeito": "Respeito mútuo",
                   "conducao": "Condução do veículo", "zelo_veiculo": "Zelo pelo veículo (limpeza e conservação)"},
    "motorista": {"conduta": "Conduta", "tratamento": "Tratamento", "respeito": "Respeito mútuo",
                  "zelo_veiculo": "Zelo pelo veículo (cuidado durante a corrida)"},
}
OCORRENCIAS = {
    "assedio": "Assédio", "desrespeito": "Desrespeito", "imoralidade": "Imoralidade",
    "humilhacao": "Humilhação", "violencia_fisica": "Violência física", "violencia_verbal": "Violência verbal",
}
# estas suspendem a conta denunciada na hora, até a central decidir
GRAVES = {"assedio", "imoralidade", "violencia_fisica"}


def validar(por: str, notas: dict, ocorrencias: list[str]) -> list[str]:
    erros = []
    esperados = set(CRITERIOS[por])
    if set(notas) != esperados:
        erros.append("Avalie todos os critérios: " + ", ".join(CRITERIOS[por].values()) + ".")
    if any(not isinstance(v, int) or not 1 <= v <= 5 for v in notas.values()):
        erros.append("As notas vão de 1 a 5 estrelas.")
    invalidas = [o for o in ocorrencias if o not in OCORRENCIAS]
    if invalidas:
        erros.append("Ocorrência desconhecida: " + ", ".join(invalidas))
    return erros


def aplicar_nota(u: dict, notas: dict) -> None:
    """Atualiza a reputação: média por critério e nota geral (média dos critérios)."""
    rep = u.setdefault("reputacao", {"avaliacoes": 0, "soma": {}})
    rep["avaliacoes"] += 1
    for k, v in notas.items():
        rep["soma"][k] = rep["soma"].get(k, 0) + v
    medias = {k: s / rep["avaliacoes"] for k, s in rep["soma"].items()}
    rep["medias"] = {k: round(v, 2) for k, v in medias.items()}
    u["nota_avaliacoes"] = round(sum(medias.values()) / len(medias), 2)
    # quem dirige perde até 10% da nota por cancelamentos (ver cancelamento.py)
    u["nota"] = round(u["nota_avaliacoes"] * (1 - u.get("penalidade_cancelamento", 0.0)), 2)


def bloqueados(u: dict) -> set[str]:
    return set(u.get("bloqueados", []))


def pode_parear(passageira: dict, motorista: dict) -> bool:
    return (motorista["id"] not in bloqueados(passageira)) and (passageira["id"] not in bloqueados(motorista))


class Denuncias:
    def __init__(self, banco):
        self.banco = banco
        banco.con.execute("create table if not exists denuncias (id text primary key, dados text)")
        banco.con.commit()

    def criar(self, *, corrida_id: str, por: str, autor_id: str, denunciado_id: str, denunciado_tipo: str,
              ocorrencias: list[str], comentario: str | None) -> dict:
        d = {"id": uuid.uuid4().hex[:12], "corrida_id": corrida_id, "por": por, "autor_id": autor_id,
             "denunciado_id": denunciado_id, "denunciado_tipo": denunciado_tipo, "ocorrencias": ocorrencias,
             "rotulos": [OCORRENCIAS[o] for o in ocorrencias], "comentario": comentario,
             "grave": bool(GRAVES & set(ocorrencias)), "status": "em_analise", "quando": time.time()}
        self.salvar(d)
        return d

    def salvar(self, d: dict) -> None:
        self.banco.con.execute("insert or replace into denuncias values (?, ?)", (d["id"], json.dumps(d)))
        self.banco.con.commit()

    def obter(self, did: str) -> dict | None:
        row = self.banco.con.execute("select dados from denuncias where id = ?", (did,)).fetchone()
        return json.loads(row[0]) if row else None

    def listar(self) -> list[dict]:
        rows = self.banco.con.execute("select dados from denuncias").fetchall()
        return sorted((json.loads(r[0]) for r in rows), key=lambda d: -d["quando"])
