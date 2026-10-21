"""Vencimento da CNH e do CRLV de quem dirige.

- A partir de AVISO_DIAS antes do vencimento, o app avisa todos os dias.
- Vencido, quem dirige não fica disponível nem aceita corridas até enviar o documento novo.
- O cadastro não aceita documento já vencido.
"""
from __future__ import annotations

from datetime import date

AVISO_DIAS = 30
NOMES = {"cnh_validade": "CNH", "crlv_validade": "CRLV (licenciamento do veículo)"}


def situacao(m: dict, hoje: date) -> dict:
    docs, vencidos, avisos = {}, [], []
    for campo, nome in NOMES.items():
        v = m.get(campo)
        if not v:
            docs[campo] = {"nome": nome, "validade": None, "dias": None, "status": "pendente"}
            vencidos.append(nome)
            continue
        dias = (date.fromisoformat(v) - hoje).days
        st = "vencido" if dias < 0 else "vence_logo" if dias <= AVISO_DIAS else "ok"
        docs[campo] = {"nome": nome, "validade": v, "dias": dias, "status": st}
        if st == "vencido":
            vencidos.append(nome)
        elif st == "vence_logo":
            avisos.append(f"Sua {nome} vence em {dias} dia{'s' if dias != 1 else ''}. Renove e envie o documento novo.")
    return {"documentos": docs, "pode_dirigir": not vencidos, "vencidos": vencidos, "avisos": avisos,
            "mensagem": (f"{' e '.join(vencidos)} vencid{'os' if len(vencidos) > 1 else 'o'}. Envie o documento "
                         "renovado para voltar a receber corridas.") if vencidos else None}
