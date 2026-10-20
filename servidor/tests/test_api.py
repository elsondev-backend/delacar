import os

os.environ["MOTORA_DB"] = ":memory:"
os.environ["MOTORA_MODO_DEMO"] = "1"

import pytest
from fastapi.testclient import TestClient

from app import notificacoes, regras
from app.main import app

cli = TestClient(app)

PESSOA = {"cpf": "123.456.789-09", "nascimento": "1994-05-12", "telefone": "(11) 98765-4321",
          "email": "x@exemplo.com", "contato_nome": "Marta Ferreira", "contato_telefone": "(11) 91234-5678",
          "aceite_monitoramento": True}


def passageira():
    r = cli.post("/api/passageiras", json={**PESSOA, "nome": "Juliana Ferreira", "declaracao_mulher": True})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def motorista(sexo="F", modelo="BYD Dolphin"):
    r = cli.post("/api/motoristas", json={**PESSOA, "nome": "Rosana Lima", "contato_telefone": "(11) 93456-7788",
                                          "sexo": sexo, "declaracao_veracidade": True, "cnh": "04512378901",
                                          "placa": "RTA-4F21", "modelo": modelo, "cor": "Branco", "ano": 2021})
    return r


def corrida_em_viagem(destino=None):
    pid, mid = passageira(), motorista().json()["id"]
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140},
                                       "destino": destino or {"x": 150, "y": 110}, "hora": 9}).json()
    cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid})
    cli.post(f"/api/corridas/{c['id']}/chegada")
    r = cli.post(f"/api/corridas/{c['id']}/iniciar", json={"pin": c["pin"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_passageira_precisa_declarar_mulher():
    r = cli.post("/api/passageiras", json={**PESSOA, "nome": "Ana Souza", "declaracao_mulher": False})
    assert r.status_code == 422


def test_motorista_homem_aceito_e_carro_a_combustao_recusado():
    assert motorista("M", "Toyota Corolla Cross Hybrid").status_code == 201
    r = motorista("F", "Chevrolet Onix")
    assert r.status_code == 422 and "elétricos ou híbridos" in r.text


def test_limite_de_100_km():
    pid = passageira()
    r = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 0, "y": 0}, "destino": {"x": 80, "y": 40}})
    assert r.status_code == 422 and "100 km" in r.text


def test_rota_compartilhada_automaticamente_ao_iniciar():
    notificacoes.enviadas.clear()
    c = corrida_em_viagem()
    share = c["compartilhada_com"]
    assert share["nome"] == "Marta Ferreira" and "/acompanhar/" in share["link"]
    assert notificacoes.enviadas[-1]["para"] == "+5511912345678"
    assert share["link"] in notificacoes.enviadas[-1]["texto"]
    token = share["link"].rsplit("/", 1)[1]
    d = cli.get(f"/api/acompanhar/{token}").json()
    assert d["status"] == "em_viagem" and d["motorista"]["placa"] == "RTA-4F21"
    assert cli.get(f"/acompanhar/{token}").status_code == 200


def test_pin_errado_nao_inicia():
    pid, mid = passageira(), motorista().json()["id"]
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 110, "y": 140}}).json()
    cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid})
    cli.post(f"/api/corridas/{c['id']}/chegada")
    assert cli.post(f"/api/corridas/{c['id']}/iniciar", json={"pin": "xxxx"}).status_code == 403


def test_preferencia_por_motorista_mulher():
    pid = passageira()
    homem = motorista("M").json()["id"]
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140},
                                       "destino": {"x": 110, "y": 140}, "prefere_motorista_mulher": True}).json()
    assert c["id"] not in [x["id"] for x in cli.get(f"/api/corridas/disponiveis?motorista_id={homem}").json()]
    assert cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": homem}).status_code == 403


def test_desvio_de_rota_gera_alerta():
    c = corrida_em_viagem()
    r = cli.post(f"/api/corridas/{c['id']}/posicao", json={"x": 120, "y": 146}).json()
    assert r["alerta_desvio"] is True
    assert any(e["tipo"] == "desvio" for e in cli.get("/api/eventos").json())


def test_rota_alternativa_exige_confirmacao_visual_e_cobra_antes():
    c = corrida_em_viagem()
    cid = c["id"]
    cli.post(f"/api/corridas/{cid}/posicao", json={"x": 110, "y": 140})
    cli.post(f"/api/corridas/{cid}/transito")
    alt = [{"x": 110, "y": 140}, {"x": 110, "y": 104}, {"x": 150, "y": 104}, {"x": 150, "y": 110}]
    p = cli.post(f"/api/corridas/{cid}/rota-alternativa", json={"rota": alt}).json()["proposta"]
    assert p["km_extra"] == pytest.approx(12) and p["valor"] > 0
    r = cli.post(f"/api/corridas/{cid}/rota-alternativa/resposta", json={"aceita": True, "viu_engarrafamento": False})
    assert r.status_code == 422
    c = cli.post(f"/api/corridas/{cid}/rota-alternativa/resposta", json={"aceita": True, "viu_engarrafamento": True}).json()
    assert c["adicional"] == p["valor"] and c["rota"] == alt
    fim = cli.post(f"/api/corridas/{cid}/finalizar").json()
    assert fim["tarifa_final"]["adicional"] == p["valor"]
    assert fim["tarifa_final"]["total"] >= p["valor"] + regras.TARIFA_MINIMA


def test_sos_avisa_contato_com_link():
    notificacoes.enviadas.clear()
    c = corrida_em_viagem()
    r = cli.post(f"/api/corridas/{c['id']}/sos", json={"por": "passageira"}).json()
    assert "SOS" in notificacoes.enviadas[-1]["texto"] and r["link"] in notificacoes.enviadas[-1]["texto"]


def test_tarifa_noturna_e_dinamica():
    t = regras.calcular(10, 15, 300, True, 1.4)
    corrida = 4 + 10 * 2.2 + 15 * 0.4
    sub = corrida * 1.4 + 2 * 0.6
    assert t.total == pytest.approx(sub * 1.2)
