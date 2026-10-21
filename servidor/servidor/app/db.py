"""Armazenamento em SQLite. Cada registro é guardado como JSON para manter o protótipo simples.

Para produção, troque por PostgreSQL (mesmas tabelas) apontando MOTORA_DB para o banco.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from . import regras

_lock = threading.RLock()


class _Resultado:
    """Resultado já lido dentro do lock (fetchone, fetchall, rowcount e iteração)."""
    def __init__(self, cur):
        self.rows, self.rowcount = cur.fetchall(), cur.rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _ConexaoSegura:
    """A mesma conexão SQLite é usada por várias requisições ao mesmo tempo; cada comando passa pelo lock."""
    def __init__(self, con):
        self._con = con

    def execute(self, sql, args=()):
        with _lock:
            return _Resultado(self._con.execute(sql, args))

    def executescript(self, sql):
        with _lock:
            self._con.executescript(sql)

    def commit(self):
        with _lock:
            self._con.commit()


class Banco:
    def __init__(self, caminho: str | None = None):
        caminho = caminho or os.environ.get("MOTORA_DB", "motora.db")
        if caminho != ":memory:":
            Path(caminho).parent.mkdir(parents=True, exist_ok=True)
        self.con = _ConexaoSegura(sqlite3.connect(caminho, check_same_thread=False))
        self.con.executescript(
            """
            create table if not exists usuarios (id text primary key, tipo text, dados text);
            create table if not exists corridas (id text primary key, token text unique, dados text);
            create table if not exists regioes (nome text primary key, dados text);
            create table if not exists eventos (id integer primary key autoincrement, corrida_id text,
                                                tipo text, texto text, quando real);
            """
        )
        with _lock:
            for nome, d in regras.REGIOES_INICIAIS.items():
                self.con.execute("insert or ignore into regioes values (?, ?)", (nome, json.dumps(d)))
            self.con.commit()

    # leituras também passam pelo lock: a mesma conexão é usada por várias requisições ao mesmo tempo
    def _ler(self, sql: str, args: tuple = ()) -> list:
        with _lock:
            return self.con.execute(sql, args).fetchall()

    def _um(self, sql: str, args: tuple = ()):
        rows = self._ler(sql, args)
        return rows[0] if rows else None

    # usuários
    def salvar_usuario(self, uid: str, tipo: str, dados: dict) -> None:
        with _lock:
            self.con.execute("insert or replace into usuarios values (?, ?, ?)", (uid, tipo, json.dumps(dados)))
            self.con.commit()

    def usuario(self, uid: str, tipo: str | None = None) -> dict | None:
        row = self._um("select tipo, dados from usuarios where id = ?", (uid,))
        if not row or (tipo and row[0] != tipo):
            return None
        return json.loads(row[1])

    def usuarios(self, tipo: str) -> list[dict]:
        rows = self._ler("select dados from usuarios where tipo = ?", (tipo,))
        return [json.loads(r[0]) for r in rows]

    # corridas
    def salvar_corrida(self, c: dict) -> None:
        with _lock:
            self.con.execute("insert or replace into corridas values (?, ?, ?)",
                             (c["id"], c.get("token"), json.dumps(c)))
            self.con.commit()

    def corrida(self, cid: str) -> dict | None:
        row = self._um("select dados from corridas where id = ?", (cid,))
        return json.loads(row[0]) if row else None

    def corrida_por_token(self, token: str) -> dict | None:
        row = self._um("select dados from corridas where token = ?", (token,))
        return json.loads(row[0]) if row else None

    def corridas(self) -> list[dict]:
        return [json.loads(r[0]) for r in self._ler("select dados from corridas")]

    def corridas_buscando(self) -> list[dict]:
        rows = self._ler("select dados from corridas")
        return [c for c in (json.loads(r[0]) for r in rows) if c["status"] == "buscando"]

    # regiões
    def regioes(self) -> dict:
        return {n: json.loads(d) for n, d in self._ler("select nome, dados from regioes")}

    def salvar_regiao(self, nome: str, dados: dict) -> None:
        with _lock:
            self.con.execute("insert or replace into regioes values (?, ?)", (nome, json.dumps(dados)))
            self.con.commit()

    # eventos da central
    def evento(self, corrida_id: str | None, tipo: str, texto: str) -> None:
        with _lock:
            self.con.execute("insert into eventos (corrida_id, tipo, texto, quando) values (?, ?, ?, ?)",
                             (corrida_id, tipo, texto, time.time()))
            self.con.commit()

    def eventos(self, limite: int = 100) -> list[dict]:
        rows = self._ler(
            "select corrida_id, tipo, texto, quando from eventos order by id desc limit ?", (limite,))
        return [{"corrida_id": a, "tipo": b, "texto": c, "quando": d} for a, b, c, d in rows]
