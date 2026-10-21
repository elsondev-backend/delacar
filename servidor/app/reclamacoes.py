"""Reclamações à central: descumprimento de regras por passageiras ou motoristas e objetos esquecidos.

A Motora não se responsabiliza por objetos esquecidos. Se o objeto for encontrado, a entrega custa
R$ 30 até 15 km e, acima disso, R$ 1 por km a mais, até o teto de R$ 50. Retirar o objeto
combinando com a central não tem custo.
"""
from __future__ import annotations

import json
import time
import uuid

CATEGORIAS = {
    "passageira": {
        "objeto_esquecido": "Esqueci um objeto no carro",
        "conduta": "Conduta de quem dirigiu",
        "direcao": "Direção perigosa ou desrespeito às leis de trânsito",
        "veiculo": "Veículo diferente do cadastro, sujo ou em más condições",
        "cobranca": "Rota ou cobrança",
        "regras": "Outro descumprimento das regras do app",
        "outro": "Outro assunto",
    },
    "motorista": {
        "objeto_encontrado": "Objeto esquecido no carro",
        "conduta": "Conduta da passageira",
        "veiculo": "Dano ao veículo",
        "cobranca": "Rota ou cobrança",
        "regras": "Outro descumprimento das regras do app",
        "outro": "Outro assunto",
    },
}
OBJETO = {"objeto_esquecido", "objeto_encontrado"}

STATUS = {
    "aberta": "Aberta",
    "em_andamento": "Em andamento",
    "objeto_encontrado": "Objeto encontrado",
    "objeto_nao_encontrado": "Objeto não encontrado",
    "entregue": "Objeto entregue",
    "resolvida": "Resolvida",
}
FINAIS = {"objeto_nao_encontrado", "entregue", "resolvida"}

AVISO_OBJETOS = ("A Motora não se responsabiliza por objetos esquecidos ou perdidos no veículo. Se o objeto for "
                 "encontrado, a entrega custa R$ 30,00 até 15 km e até R$ 50,00 acima disso.")

ENTREGA_BASE = 30.00
ENTREGA_KM_BASE = 15
ENTREGA_POR_KM = 1.00     # acima de 15 km
ENTREGA_MAX = 50.00


def taxa_entrega(km: float) -> float:
    if km <= ENTREGA_KM_BASE:
        return ENTREGA_BASE
    return round(min(ENTREGA_MAX, ENTREGA_BASE + (km - ENTREGA_KM_BASE) * ENTREGA_POR_KM), 2)


class Reclamacoes:
    def __init__(self, banco):
        self.banco = banco
        banco.con.execute("create table if not exists reclamacoes (id text primary key, dados text)")
        banco.con.commit()

    def criar(self, *, por: str, autor_id: str, autor_nome: str, categoria: str, descricao: str,
              corrida_id: str | None, objeto: dict | None) -> dict:
        n = len(self.listar()) + 1
        r = {"id": uuid.uuid4().hex[:12], "protocolo": f"MOT-{time.strftime('%Y')}-{n:05d}", "por": por,
             "autor_id": autor_id, "autor_nome": autor_nome, "categoria": categoria,
             "categoria_texto": CATEGORIAS[por][categoria], "descricao": descricao, "corrida_id": corrida_id,
             "objeto": objeto, "status": "aberta", "historico": [], "quando": time.time()}
        self.salvar(r)
        return r

    def salvar(self, r: dict) -> None:
        self.banco.con.execute("insert or replace into reclamacoes values (?, ?)", (r["id"], json.dumps(r)))
        self.banco.con.commit()

    def obter(self, rid: str) -> dict | None:
        row = self.banco.con.execute("select dados from reclamacoes where id = ?", (rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def listar(self, autor_id: str | None = None) -> list[dict]:
        rs = [json.loads(r[0]) for r in self.banco.con.execute("select dados from reclamacoes").fetchall()]
        if autor_id:
            rs = [r for r in rs if r["autor_id"] == autor_id]
        return sorted(rs, key=lambda r: -r["quando"])
