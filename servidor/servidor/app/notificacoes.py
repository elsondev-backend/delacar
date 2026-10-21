"""Envio de mensagens ao contato de emergência.

Sem configuração, as mensagens só aparecem no log do servidor (modo de teste).
Com as variáveis abaixo definidas, saem por SMS ou WhatsApp via Twilio:

    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM   (ex.: +5511999990000)
    MOTORA_CANAL=whatsapp                                (opcional; padrão sms)
"""
from __future__ import annotations

import base64
import logging
import os
import re
import urllib.parse
import urllib.request

log = logging.getLogger("motora.notificacoes")

# mensagens enviadas ficam aqui também, para os testes e para a central
enviadas: list[dict] = []


def telefone_e164(tel: str) -> str:
    dig = re.sub(r"\D", "", tel)
    if not dig.startswith("55"):
        dig = "55" + dig
    return "+" + dig


def enviar(telefone: str, texto: str) -> bool:
    destino = telefone_e164(telefone)
    enviadas.append({"para": destino, "texto": texto})
    sid, token, origem = (os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))
    if not (sid and token and origem):
        log.warning("[modo teste] mensagem para %s: %s", destino, texto)
        return True
    canal = os.environ.get("MOTORA_CANAL", "sms")
    para, de = (f"whatsapp:{destino}", f"whatsapp:{origem}") if canal == "whatsapp" else (destino, origem)
    corpo = urllib.parse.urlencode({"To": para, "From": de, "Body": texto}).encode()
    req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=corpo)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:  # não derruba a corrida se o envio falhar
        log.exception("falha ao enviar mensagem para %s", destino)
        return False
