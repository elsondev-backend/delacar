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

_lock = threading.Lock()


class Banco:
    def __init__(self, caminho: str | None = None):
        caminho = caminho or os.environ.get("MOTORA_DB", "motora.db")
        if caminho != ":memory:":
            Path(caminho).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(caminho, check_same_thread=False)
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

    # usuários
    def salvar_usuario(self, uid: str, tipo: str, dados: dict) -> None:
        with _lock:
            self.con.execute("insert or replace into usuarios values (?, ?, ?)", (uid, tipo, json.dumps(dados)))
            self.con.commit()

    def usuario(self, uid: str, tipo: str | None = None) -> dict | None:
        row = self.con.execute("select tipo, dados from usuarios where id = ?", (uid,)).fetchone()
        if not row or (tipo and row[0] != tipo):
            return None
        return json.loads(row[1])

    # corridas
    def salvar_corrida(self, c: dict) -> None:
        with _lock:
            self.con.execute("insert or replace into corridas values (?, ?, ?)",
                             (c["id"], c.get("token"), json.dumps(c)))
            self.con.commit()

    def corrida(self, cid: str) -> dict | None:
        row = self.con.execute("select dados from corridas where id = ?", (cid,)).fetchone()
        return json.loads(row[0]) if row else None

    def corrida_por_token(self, token: str) -> dict | None:
        row = self.con.execute("select dados from corridas where token = ?", (token,)).fetchone()
        return json.loads(row[0]) if row else None

    def corridas_buscando(self) -> list[dict]:
        rows = self.con.execute("select dados from corridas").fetchall()
        return [c for c in (json.loads(r[0]) for r in rows) if c["status"] == "buscando"]

    # regiões
    def regioes(self) -> dict:
        return {n: json.loads(d) for n, d in self.con.execute("select nome, dados from regioes")}

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
        rows = self.con.execute(
            "select corrida_id, tipo, texto, quando from eventos order by id desc limit ?", (limite,))
        return [{"corrida_id": a, "tipo": b, "texto": c, "quando": d} for a, b, c, d in rows]
