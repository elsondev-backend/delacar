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
    pid = r.json()["id"]
    assert cli.post(f"/api/passageiras/{pid}/cartao").json()["cartao"]["final"] == "4242"
    return pid


SELFIE = "data:image/jpeg;base64,/9j/4AAQ"


def motorista(sexo="F", modelo="BYD Dolphin"):
    r = cli.post("/api/motoristas", json={**PESSOA, "nome": "Rosana Lima", "contato_telefone": "(11) 93456-7788",
                                          "sexo": sexo, "declaracao_veracidade": True, "cnh": "04512378901",
                                          "cnh_validade": "2030-01-01", "crlv_validade": "2030-01-01",
                                          "bluetooth_tipo": "multimidia", "bluetooth_nome": "BYD Dolphin",
                                          "placa": "RTA-4F21", "modelo": modelo, "cor": "Branco", "ano": 2021})
    return r


def motorista_pronta(sexo="F"):
    mid = motorista(sexo).json()["id"]
    assert cli.post(f"/api/motoristas/{mid}/recebimentos").json()["ativo"] is True
    assert cli.post(f"/api/motoristas/{mid}/prova-de-vida", json={"selfie": SELFIE}).json()["valida"] is True
    return mid


def corrida_em_viagem(destino=None):
    pid, mid = passageira(), motorista_pronta()
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


def test_compartilhar_rota_pelo_whatsapp_abre_no_app_de_mapas():
    c = corrida_em_viagem()
    w = cli.post(f"/api/corridas/{c['id']}/compartilhar").json()
    assert w["link"] == c["compartilhada_com"]["link"]          # mesmo link do compartilhamento automático
    assert w["whatsapp_contato"].startswith("https://wa.me/5511912345678?text=")
    assert w["whatsapp"].startswith("https://wa.me/?text=") and "placa%20RTA-4F21" in w["whatsapp"]
    d = cli.get(f"/api/acompanhar/{w['link'].rsplit('/', 1)[1]}").json()
    g = d["geo"]["posicao"]
    assert -24.7 < g["lat"] < -22.4 and -47.9 < g["lng"] < -45.3   # dentro da Grande São Paulo
    assert regras.latlng({"x": 120, "y": 120}) == {"lat": -23.5505, "lng": -46.6333}
    assert regras.latlng({"x": 0, "y": 0, "lat": -22.9, "lng": -43.2}) == {"lat": -22.9, "lng": -43.2}


def test_pin_errado_nao_inicia():
    pid, mid = passageira(), motorista_pronta()
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 110, "y": 140}}).json()
    cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid})
    cli.post(f"/api/corridas/{c['id']}/chegada")
    assert cli.post(f"/api/corridas/{c['id']}/iniciar", json={"pin": "xxxx"}).status_code == 403


def test_preferencia_por_motorista_mulher():
    pid = passageira()
    homem = motorista_pronta("M")
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
    sub = corrida * 1.4 + 3 * 0.6   # 300 s de espera, 2 min grátis
    assert t.total == pytest.approx(sub * 1.2 + 3)   # + taxa fixa de deslocamento
    assert t.deslocamento == 3


def test_pedido_exige_cartao_e_aceite_exige_conta_de_recebimento():
    r = cli.post("/api/passageiras", json={**PESSOA, "nome": "Bia Lopes", "declaracao_mulher": True})
    sem_cartao = r.json()["id"]
    r = cli.post("/api/corridas", json={"passageira_id": sem_cartao, "origem": {"x": 100, "y": 140}, "destino": {"x": 110, "y": 140}})
    assert r.status_code == 402
    pid, sem_conta = passageira(), motorista().json()["id"]
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 110, "y": 140}}).json()
    r = cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": sem_conta})
    assert r.status_code == 403 and "recebimento" in r.text


def test_pagamento_divide_85_para_motorista_e_15_para_motora():
    c = corrida_em_viagem()
    fim = cli.post(f"/api/corridas/{c['id']}/finalizar").json()
    pg = fim["pagamentos"][-1]
    total = fim["tarifa_final"]["total"]
    assert pg["status"] == "pago" and pg["valor"] == pytest.approx(total)
    # os R$ 3 de deslocamento vão inteiros para quem dirige, fora da divisão 85/15
    assert pg["taxa_motora"] == pytest.approx(round((total - 3) * 0.15, 2), abs=0.01)
    assert pg["taxa_motora"] + pg["repasse_motorista"] == pytest.approx(total, abs=0.01)


def test_rota_alternativa_gera_duas_cobrancas_sem_cobrar_em_dobro():
    c = corrida_em_viagem()
    cid = c["id"]
    cli.post(f"/api/corridas/{cid}/posicao", json={"x": 110, "y": 140})
    alt = [{"x": 110, "y": 140}, {"x": 110, "y": 104}, {"x": 150, "y": 104}, {"x": 150, "y": 110}]
    cli.post(f"/api/corridas/{cid}/rota-alternativa", json={"rota": alt})
    cli.post(f"/api/corridas/{cid}/rota-alternativa/resposta", json={"aceita": True, "viu_engarrafamento": True})
    fim = cli.post(f"/api/corridas/{cid}/finalizar").json()
    soma = sum(p["valor"] for p in fim["pagamentos"])
    assert [p["motivo"] for p in fim["pagamentos"]] == ["rota-alternativa", "corrida"]
    assert soma == pytest.approx(fim["tarifa_final"]["total"], abs=0.01)


def test_achatar_parametros_no_formato_da_stripe():
    from app.pagamentos import _achatar
    out = dict(_achatar({"transfer_data": {"destination": "acct_1"}, "payment_method_types": ["card"], "confirm": True}))
    assert out == {"transfer_data[destination]": "acct_1", "payment_method_types[0]": "card", "confirm": "true"}


def test_webhook_confere_assinatura(monkeypatch):
    import hashlib, hmac, json, time
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_teste")
    corpo = json.dumps({"type": "ping", "data": {"object": {"id": "x"}}}).encode()
    t = str(int(time.time()))
    sig = hmac.new(b"whsec_teste", f"{t}.".encode() + corpo, hashlib.sha256).hexdigest()
    ok = cli.post("/api/stripe/webhook", content=corpo, headers={"stripe-signature": f"t={t},v1={sig}"})
    assert ok.status_code == 200
    ruim = cli.post("/api/stripe/webhook", content=corpo, headers={"stripe-signature": f"t={t},v1=errada"})
    assert ruim.status_code == 400


def test_modo_stripe_real_monta_cobranca_com_divisao(monkeypatch):
    from app import pagamentos
    chamadas = []

    def falso(metodo, caminho, dados=None, idem=None):
        chamadas.append((metodo, caminho, dados, idem))
        return {"id": "pi_123", "status": "succeeded"}

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(pagamentos, "_stripe", falso)
    pg = pagamentos.cobrar(valor=100.0, cliente_id="cus_1", metodo_id="pm_1", conta_motorista="acct_9",
                           corrida_id="abc123", motivo="corrida")
    _, caminho, dados, idem = chamadas[0]
    assert caminho == "/payment_intents" and idem == "abc123-corrida"
    assert dados["amount"] == 10000 and dados["currency"] == "brl"
    assert dados["application_fee_amount"] == 1500
    assert dados["transfer_data"] == {"destination": "acct_9"}
    assert dados["off_session"] is True and dados["confirm"] is True
    assert pg["status"] == "pago" and pg["repasse_motorista"] == 85.0


def test_desconto_fiel_de_5_por_cento_sem_bandeirada():
    normal = regras.calcular(10, 15, 0, False, 1.4)
    fiel = regras.calcular(10, 15, 0, False, 1.4, fiel=True)
    esperado = (10 * 2.2 + 15 * 0.4) * 1.4 * 0.05   # bandeirada (R$ 4) fora da conta
    assert fiel.desconto == pytest.approx(esperado)
    assert normal.total - fiel.total == pytest.approx(esperado)
    assert fiel.cheio == pytest.approx(normal.total)
    noite = regras.calcular(10, 15, 0, True, 1.0, fiel=True)
    assert noite.desconto == pytest.approx((22 + 6) * 0.05 * 1.2)


def test_fiel_paga_menos_e_motorista_recebe_o_mesmo():
    def corrida(fiel):
        pid, mid = passageira(), motorista_pronta()
        cli.post(f"/api/passageiras/{pid}/fidelidade", json={"ativo": fiel})
        c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140},
                                           "destino": {"x": 130, "y": 140}, "hora": 9}).json()
        cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid})
        cli.post(f"/api/corridas/{c['id']}/chegada")
        cli.post(f"/api/corridas/{c['id']}/iniciar", json={"pin": c["pin"]})
        return cli.post(f"/api/corridas/{c['id']}/finalizar").json()
    normal, fiel = corrida(False), corrida(True)
    assert fiel["tarifa_final"]["desconto"] > 0
    assert fiel["pagamentos"][-1]["valor"] < normal["pagamentos"][-1]["valor"]
    assert fiel["pagamentos"][-1]["repasse_motorista"] == pytest.approx(normal["pagamentos"][-1]["repasse_motorista"], abs=0.02)
    assert fiel["ganho_motorista"] == pytest.approx(normal["ganho_motorista"], abs=0.02)


def test_sinal_fraco_informado_e_atraso_de_posicao(monkeypatch):
    from app import main
    c = corrida_em_viagem()
    cid = c["id"]
    s = cli.post(f"/api/corridas/{cid}/sinal", json={"por": "passageira", "qualidade": "fraco", "rtt_ms": 1800}).json()
    assert s["passageira"]["qualidade"] == "fraco"
    assert any("Sinal de internet fraco" in e["texto"] for e in cli.get("/api/eventos").json())
    cli.post(f"/api/corridas/{cid}/posicao", json={"x": 105, "y": 140})
    real = main.agora
    monkeypatch.setattr(main, "agora", lambda: real() + 45)
    v = cli.get(f"/api/corridas/{cid}").json()
    assert v["sinal"]["motorista"]["qualidade"] == "fraco" and v["sinal"]["motorista"]["atraso_posicao_seg"] >= 44
    token = v["compartilhada_com"]["link"].rsplit("/", 1)[1]
    assert cli.get(f"/api/acompanhar/{token}").json()["sinal_fraco"] is True


def test_zona_de_sinal_fraco_formada_por_relatos_e_alerta_no_pedido():
    c = corrida_em_viagem(destino={"x": 150, "y": 110})
    cid = c["id"]
    cli.post(f"/api/corridas/{cid}/posicao", json={"x": 123, "y": 140})
    cli.post(f"/api/corridas/{cid}/sinal", json={"por": "motorista", "qualidade": "fraco"})
    z = [z for z in cli.get("/api/zonas-sinal").json()["zonas"] if z["tipo"] == "automatica"]
    assert any(x["celula"] == "12:14" for x in z)
    assert cli.get("/api/zonas-sinal/verificar?x=125&y=145").json()["em_zona"] is True
    # nova corrida que passa pela zona recebe o alerta
    pid = passageira()
    nova = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 62, "y": 147},
                                          "destino": {"x": 140, "y": 147}}).json()
    assert nova["alerta_sinal"]["tem_alerta"] and "no trajeto" in nova["alerta_sinal"]["mensagem"]


def test_central_marca_e_remove_zona_manual():
    z = cli.post("/api/zonas-sinal", json={"x": 30, "y": 30, "raio_km": 5, "nota": "Túnel"}).json()
    assert z["tipo"] == "manual" and z["regiao"] == "Região Norte"
    e = cli.post("/api/estimativa", json={"origem": {"x": 31, "y": 31}, "destino": {"x": 60, "y": 31}}).json()
    assert e["alerta_sinal"]["embarque"] is True
    assert cli.delete(f"/api/zonas-sinal/{z['id']}").json()["ok"] is True
    e = cli.post("/api/estimativa", json={"origem": {"x": 31, "y": 31}, "destino": {"x": 60, "y": 31}}).json()
    assert e["alerta_sinal"]["embarque"] is False


def test_central_dispensa_zona_automatica():
    c = corrida_em_viagem()
    cli.post(f"/api/corridas/{c['id']}/posicao", json={"x": 205, "y": 205})
    cli.post(f"/api/corridas/{c['id']}/sinal", json={"por": "motorista", "qualidade": "sem", "x": 205, "y": 205})
    zid = "auto-20:20"
    assert any(z["id"] == zid and z["nivel"] == "sem sinal" for z in cli.get("/api/zonas-sinal").json()["zonas"])
    cli.delete(f"/api/zonas-sinal/{zid}")
    assert not any(z["id"] == zid for z in cli.get("/api/zonas-sinal").json()["zonas"])


NOTAS_PASS = {"conduta": 5, "tratamento": 4, "respeito": 5, "conducao": 3, "zelo_veiculo": 5}
NOTAS_DRV = {"conduta": 5, "tratamento": 5, "respeito": 5, "zelo_veiculo": 4}


def corrida_concluida():
    c = corrida_em_viagem()
    return cli.post(f"/api/corridas/{c['id']}/finalizar").json()


def test_avaliacao_mutua_por_criterio_atualiza_nota():
    c = corrida_concluida()
    r = cli.post(f"/api/corridas/{c['id']}/avaliacao", json={"por": "passageira", "notas": NOTAS_PASS}).json()
    assert r["nota_atual_avaliado"] == pytest.approx(4.4)
    rep = cli.get(f"/api/usuarios/{c['motorista_id']}/reputacao").json()
    assert rep["avaliacoes"] == 1 and rep["medias"]["conducao"] == 3
    assert cli.post(f"/api/corridas/{c['id']}/avaliacao", json={"por": "motorista", "notas": NOTAS_DRV}).status_code == 200
    assert cli.post(f"/api/corridas/{c['id']}/avaliacao", json={"por": "passageira", "notas": NOTAS_PASS}).status_code == 409


def test_avaliacao_exige_todos_criterios_e_notas_de_1_a_5():
    c = corrida_concluida()
    r = cli.post(f"/api/corridas/{c['id']}/avaliacao", json={"por": "passageira", "notas": {"conduta": 5}})
    assert r.status_code == 422
    r = cli.post(f"/api/corridas/{c['id']}/avaliacao", json={"por": "motorista", "notas": {**NOTAS_DRV, "conduta": 6}})
    assert r.status_code == 422


def test_bloqueio_impede_novo_pareamento():
    c = corrida_concluida()
    cli.post(f"/api/corridas/{c['id']}/avaliacao", json={"por": "passageira", "notas": NOTAS_PASS, "bloquear": True})
    nova = cli.post("/api/corridas", json={"passageira_id": c["passageira_id"], "origem": {"x": 100, "y": 140},
                                          "destino": {"x": 110, "y": 140}}).json()
    disp = cli.get(f"/api/corridas/disponiveis?motorista_id={c['motorista_id']}").json()
    assert nova["id"] not in [x["id"] for x in disp]
    assert cli.post(f"/api/corridas/{nova['id']}/aceitar", json={"motorista_id": c["motorista_id"]}).status_code == 403


def test_denuncia_grave_suspende_e_central_decide():
    c = corrida_concluida()
    r = cli.post(f"/api/corridas/{c['id']}/avaliacao", json={
        "por": "passageira", "notas": {k: 1 for k in NOTAS_PASS}, "ocorrencias": ["assedio", "violencia_verbal"],
        "comentario": "Comentários ofensivos durante a viagem."}).json()
    assert r["denuncia"]["grave"] is True and r["denuncia"]["rotulos"] == ["Assédio", "Violência verbal"]
    assert cli.get(f"/api/usuarios/{c['motorista_id']}/reputacao").json()["status"] == "suspensa"
    assert cli.get(f"/api/corridas/disponiveis?motorista_id={c['motorista_id']}").json() == []
    d = next(x for x in cli.get("/api/denuncias").json() if x["id"] == r["denuncia"]["id"])
    assert d["status"] == "em_analise" and d["status_conta"] == "suspensa"
    cli.post(f"/api/denuncias/{d['id']}/decisao", json={"decisao": "reativar"})
    assert cli.get(f"/api/usuarios/{c['motorista_id']}/reputacao").json()["status"] == "ativa"


def test_denuncia_leve_nao_suspende():
    c = corrida_concluida()
    r = cli.post(f"/api/corridas/{c['id']}/avaliacao", json={
        "por": "motorista", "notas": NOTAS_DRV, "ocorrencias": ["desrespeito"]}).json()
    assert r["denuncia"]["grave"] is False
    assert cli.get(f"/api/usuarios/{c['passageira_id']}/reputacao").json()["status"] == "ativa"


FOTO = "data:image/png;base64,iVBORw0KGgo="


def test_taxa_limpeza_exige_fotos_e_nota_e_so_cobra_se_aprovada():
    c = corrida_concluida()
    cid = c["id"]
    sem_nota = {"valor": 80, "motivo": "roupa_molhada", "fotos": [FOTO], "numero_nota": "123"}
    assert cli.post(f"/api/corridas/{cid}/taxa-limpeza", json=sem_nota).status_code == 422
    assert cli.post(f"/api/corridas/{cid}/taxa-limpeza", json={**sem_nota, "fotos": [], "nota_fiscal": FOTO}).status_code == 422
    t = cli.post(f"/api/corridas/{cid}/taxa-limpeza", json={**sem_nota, "nota_fiscal": FOTO}).json()
    assert t["status"] == "em_analise" and t["fotos_qtd"] == 1 and "fotos" not in t
    pagos_antes = len(cli.get(f"/api/corridas/{cid}").json()["pagamentos"])
    cli.post(f"/api/corridas/{cid}/taxa-limpeza/contestar", json={"motivo": "Estava seca."})
    r = cli.post(f"/api/corridas/{cid}/taxa-limpeza/decisao", json={"aprovar": True}).json()
    assert r["status"] == "cobrada" and r["contestacao"]["motivo"] == "Estava seca."
    pg = cli.get(f"/api/corridas/{cid}").json()["pagamentos"]
    assert len(pg) == pagos_antes + 1 and pg[-1]["motivo"] == "taxa-limpeza"
    assert pg[-1]["repasse_motorista"] == 80 and pg[-1]["taxa_motora"] == 0


def test_taxa_limpeza_recusada_nao_cobra():
    c = corrida_concluida()
    cid = c["id"]
    cli.post(f"/api/corridas/{cid}/taxa-limpeza", json={"valor": 50, "motivo": "sujeira", "fotos": [FOTO],
                                                       "nota_fiscal": FOTO, "numero_nota": "9"})
    n = len(cli.get(f"/api/corridas/{cid}").json()["pagamentos"])
    assert cli.post(f"/api/corridas/{cid}/taxa-limpeza/decisao", json={"aprovar": False}).json()["status"] == "recusada"
    assert len(cli.get(f"/api/corridas/{cid}").json()["pagamentos"]) == n


def test_socorro_motora_alerta_central_e_atendimento():
    c = corrida_em_viagem()
    ap = cli.post(f"/api/corridas/{c['id']}/apoio", json={"por": "motorista", "motivo": "discussao",
                                                          "detalhe": "Passageira alterada"}).json()
    assert ap["status"] == "aguardando"
    assert any(x["id"] == ap["id"] for x in cli.get("/api/apoios").json())
    assert any(e["tipo"] == "sos" and "Socorro Motora" in e["texto"] for e in cli.get("/api/eventos").json())
    assert cli.post(f"/api/corridas/{c['id']}/apoio/{ap['id']}/atender").json()["status"] == "em_atendimento"
    cli.post(f"/api/corridas/{c['id']}/apoio/{ap['id']}/encerrar")
    assert not any(x["id"] == ap["id"] for x in cli.get("/api/apoios").json())


def test_dinamica_tem_tres_niveis_acima_de_1_8():
    from app import regras as rg
    assert [rg.fator_dinamico(c, 1) for c in (1, 2, 3, 4, 5, 6, 7, 20)] == [1.0, 1.4, 1.6, 1.8, 2.0, 2.2, 2.4, 2.4]


def test_espera_2_min_no_embarque_e_1_min_por_parada():
    from app import regras as rg
    base = rg.calcular(10, 15, 0, False, 1.0).total
    assert rg.calcular(10, 15, 120, False, 1.0).total == pytest.approx(base)
    assert rg.calcular(10, 15, 180, False, 1.0).total == pytest.approx(base + 0.60)
    # duas paradas de 90 s: 30 s cobráveis em cada
    cobr = rg.parada_cobravel(90) + rg.parada_cobravel(90) + rg.parada_cobravel(50)
    assert cobr == 60
    assert rg.calcular(10, 15, 0, False, 1.0, paradas_cobraveis_seg=cobr).total == pytest.approx(base + 0.60)


def test_porta_malas_acrescimo_por_volumes():
    from app import regras as rg
    assert [rg.percentual_bagagem(n) for n in (0, 2, 3, 4, 5, 6, 7, 8, 9, 15)] == [0, 0, .03, .03, .05, .05, .07, .07, .10, .10]
    sem = rg.calcular(10, 15, 0, False, 1.4)
    com = rg.calcular(10, 15, 0, False, 1.4, volumes=5)
    assert com.bagagem == pytest.approx(sem.corrida * 1.4 * 0.05)
    assert com.total - sem.total == pytest.approx(com.bagagem)
    p = passageira()
    e = cli.post("/api/estimativa", json={"origem": {"x": 100, "y": 140}, "destino": {"x": 120, "y": 140}, "volumes": 7}).json()
    assert e["bagagem_pct"] == 0.07 and e["tarifa"]["bagagem"] > 0
    c = cli.post("/api/corridas", json={"passageira_id": p, "origem": {"x": 100, "y": 140},
                                        "destino": {"x": 120, "y": 140}, "volumes": 3}).json()
    assert c["volumes"] == 3 and c["estimativa"]["bagagem"] > 0
    assert cli.post("/api/corridas", json={"passageira_id": p, "origem": {"x": 100, "y": 140},
                                           "destino": {"x": 120, "y": 140}, "volumes": 99}).status_code == 422


def test_motora_verde_cupom_desconto_e_compatibilidade():
    mid = motorista_pronta()
    v = cli.get(f"/api/motoristas/{mid}/motora-verde", params={"x": 118, "y": 125}).json()
    assert v["tipos"] == ["recarga", "pneu", "bateria"] and not any(p["tipo"] == "etanol" for p in v["postos"])
    assert v["postos"][0]["distancia_km"] <= v["postos"][-1]["distancia_km"]
    etanol = next(p for p in cli.get("/api/postos", params={"tipo": "etanol"}).json())
    assert cli.post(f"/api/motoristas/{mid}/motora-verde/cupom", json={"posto_id": etanol["id"]}).status_code == 422
    posto = v["postos"][0]
    c = cli.post(f"/api/motoristas/{mid}/motora-verde/cupom", json={"posto_id": posto["id"]}).json()
    assert c["codigo"].startswith("MV-")
    assert cli.post("/api/postos/cupom", json={"codigo": c["codigo"], "posto_id": etanol["id"], "valor": 100}).status_code == 422
    a = cli.post("/api/postos/cupom", json={"codigo": c["codigo"], "posto_id": posto["id"], "valor": 100}).json()
    assert a["desconto"] == pytest.approx(100 * v["desconto_pct"]) and a["pago"] == pytest.approx(100 - a["desconto"])
    assert cli.post("/api/postos/cupom", json={"codigo": c["codigo"], "posto_id": posto["id"], "valor": 100}).status_code == 422
    v2 = cli.get(f"/api/motoristas/{mid}/motora-verde").json()
    assert v2["usado_no_mes"] == a["desconto"] and v2["historico"][0]["codigo"] == c["codigo"]


def test_motora_verde_niveis_e_credenciamento():
    from app import postos as pm
    assert pm.tipos_do_veiculo("Toyota Corolla Hybrid") == ["etanol", "pneu", "bateria"]
    assert pm.combustiveis_do_veiculo("BYD King") == ["recarga"] and pm.tipos_do_veiculo("Honda Civic e:HEV") == ["pneu", "bateria"]
    assert pm.nivel_por_corridas(0)["nivel"] == "Verde"
    # nota abaixo de 4 estrelas fica fora do programa
    from app.main import banco
    mid = motorista_pronta()
    m = banco.usuario(mid)
    m["nota"] = 3.6
    banco.salvar_usuario(mid, "motorista", m)
    v = cli.get(f"/api/motoristas/{mid}/motora-verde").json()
    assert v["elegivel"] is False
    pid = v["postos"][0]["id"]
    assert cli.post(f"/api/motoristas/{mid}/motora-verde/cupom", json={"posto_id": pid}).status_code == 403
    m["nota"] = 4.0
    banco.salvar_usuario(mid, "motorista", m)
    assert cli.post(f"/api/motoristas/{mid}/motora-verde/cupom", json={"posto_id": pid}).status_code == 201
    p = cli.post("/api/postos", json={"nome": "Eletroposto Teste", "tipo": "recarga", "x": 10, "y": 10}).json()
    assert any(x["id"] == p["id"] for x in cli.get("/api/postos").json())
    assert cli.delete(f"/api/postos/{p['id']}").status_code == 200
    assert cli.post("/api/postos", json={"nome": "Posto Gasolina", "tipo": "gasolina", "x": 1, "y": 1}).status_code == 422


def test_jornada_12h_e_descanso_de_6h():
    from app import jornada as jn
    m = {"id": "x"}
    t0 = 1_000_000.0
    jn.ficar_online(m, True, t0)
    st = jn.atualizar(m, t0 + 5 * 3600)
    assert st["online"] and st["trabalhado_seg"] == 5 * 3600
    jn.ficar_online(m, False, t0 + 5 * 3600)          # pausa de 2 h não zera
    jn.ficar_online(m, True, t0 + 7 * 3600)
    st = jn.atualizar(m, t0 + 14 * 3600)              # 5 h + 7 h = 12 h
    assert st["em_descanso"] and not st["online"]
    with pytest.raises(ValueError, match="Descanso obrigatório"):
        jn.ficar_online(m, True, t0 + 15 * 3600)
    st = jn.ficar_online(m, True, t0 + 20 * 3600)     # 6 h depois
    assert st["online"] and st["trabalhado_seg"] == 0
    jn.ficar_online(m, False, t0 + 21 * 3600)
    assert jn.atualizar(m, t0 + 27 * 3600)["trabalhado_seg"] == 0   # 6 h offline zera a jornada


def test_aceite_bloqueado_no_descanso_obrigatorio():
    from app.main import banco
    mid = motorista_pronta()
    st = cli.post(f"/api/motoristas/{mid}/disponibilidade", json={"online": True}).json()
    assert st["online"] and st["restante_seg"] > 11 * 3600
    m = banco.usuario(mid)
    m["jornada"]["trabalhado_seg"] = 12 * 3600 + 1
    banco.salvar_usuario(mid, "motorista", m)
    assert cli.get(f"/api/motoristas/{mid}/jornada").json()["em_descanso"] is True
    assert cli.post(f"/api/motoristas/{mid}/disponibilidade", json={"online": True}).status_code == 403
    pid = passageira()
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 110, "y": 140}}).json()
    assert cli.get("/api/corridas/disponiveis", params={"motorista_id": mid}).json() == []
    r = cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid})
    assert r.status_code == 403 and "Descanso" in r.json()["detail"]


def test_socorro_motora_pneu_e_mecanico():
    c = corrida_em_viagem()
    for mot in ("pneu", "mecanico", "bateria"):
        ap = cli.post(f"/api/corridas/{c['id']}/apoio", json={"por": "motorista", "motivo": mot}).json()
        assert ap["status"] == "aguardando" and ap["motivo"] == mot
        if mot != "mecanico":
            assert ap["parceiro_sugerido"]["tipo"] == mot


def test_vencimento_cnh_e_crlv():
    from datetime import date, timedelta
    from app import documentos as dc
    hoje = date(2026, 10, 20)
    m = {"cnh_validade": "2026-11-05", "crlv_validade": "2027-06-01"}
    st = dc.situacao(m, hoje)
    assert st["pode_dirigir"] and st["documentos"]["cnh_validade"]["status"] == "vence_logo" and "16 dias" in st["avisos"][0]
    m["crlv_validade"] = "2026-10-19"
    st = dc.situacao(m, hoje)
    assert not st["pode_dirigir"] and st["vencidos"] == ["CRLV (licenciamento do veículo)"]
    # cadastro recusa documento vencido
    ontem = (date.today() - timedelta(days=2)).isoformat()
    r = cli.post("/api/motoristas", json={**PESSOA, "nome": "Rosana Lima", "sexo": "F", "declaracao_veracidade": True,
                                          "cnh": "04512378901", "cnh_validade": ontem, "crlv_validade": "2030-01-01",
                                          "placa": "RTA-4F21", "modelo": "BYD Dolphin", "cor": "Branco", "ano": 2021})
    assert r.status_code == 422 and "CNH está vencida" in str(r.json())
    # documento vence depois do cadastro: bloqueia, e o envio do novo libera
    from app.main import banco
    mid = motorista_pronta()
    u = banco.usuario(mid)
    u["cnh_validade"] = ontem
    banco.salvar_usuario(mid, "motorista", u)
    assert cli.post(f"/api/motoristas/{mid}/disponibilidade", json={"online": True}).status_code == 403
    assert any(x["id"] == mid for x in cli.get("/api/documentos/vencimentos").json())
    pid = passageira()
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 110, "y": 140}}).json()
    assert cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid}).status_code == 403
    assert cli.put(f"/api/motoristas/{mid}/documentos", json={"cnh_validade": ontem}).status_code == 422
    assert cli.put(f"/api/motoristas/{mid}/documentos", json={"cnh_validade": "2031-03-01"}).json()["pode_dirigir"]
    assert cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid}).status_code == 200


def test_prova_de_vida_encurta_a_cada_saida():
    from app import vida as vd
    m = {}
    t = 1_000_000.0
    assert vd.comprovar(m, SELFIE, t)["validade_min"] == 60
    mins = []
    for k in range(5):
        vd.registrar_saida(m, t + k * 60)
        mins.append(vd.comprovar(m, SELFIE, t + k * 60 + 1)["validade_min"])
    assert mins == [60, 45, 30, 15, 15]
    assert vd.validade_min(m, t + 25 * 3600) == 60           # saídas antigas (mais de 24 h) não contam
    assert not vd.estado({"vida": {"valida_ate": t, "saidas": [], "ultima": t}}, t + 1)["valida"]


def test_prova_de_vida_exigida_para_ficar_disponivel_e_aceitar():
    from app.main import banco
    mid = motorista_pronta()
    assert cli.post(f"/api/motoristas/{mid}/disponibilidade", json={"online": True}).status_code == 200
    assert cli.post(f"/api/motoristas/{mid}/disponibilidade", json={"online": False}).status_code == 200
    st = cli.get(f"/api/motoristas/{mid}/prova-de-vida").json()
    assert st["valida"] is False and st["saidas_24h"] == 1
    assert cli.post(f"/api/motoristas/{mid}/disponibilidade", json={"online": True}).status_code == 403
    assert cli.post(f"/api/motoristas/{mid}/prova-de-vida", json={"selfie": "texto"}).status_code == 422
    pid = passageira()
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 110, "y": 140}}).json()
    r = cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid})
    assert r.status_code == 403 and "Prova de vida" in r.json()["detail"]
    assert cli.post(f"/api/motoristas/{mid}/prova-de-vida", json={"selfie": SELFIE}).json()["validade_min"] == 60
    assert cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid}).status_code == 200


def test_bem_estar_em_parada_longa_e_modo_assalto():
    c = corrida_em_viagem()
    mid = c["motorista_id"]
    assert cli.post(f"/api/motoristas/{mid}/bem-estar", json={"resposta": "ok", "parado_seg": 700, "corrida_id": c["id"]}).status_code == 200
    cli.post(f"/api/motoristas/{mid}/bem-estar", json={"resposta": "sem_resposta", "parado_seg": 840, "corrida_id": c["id"]})
    ev = cli.get("/api/eventos").json()
    assert any("Não respondeu" in e["texto"] and e["tipo"] == "sos" for e in ev)
    notificacoes.enviadas.clear()
    r = cli.post(f"/api/motoristas/{mid}/bem-estar", json={"resposta": "assalto", "parado_seg": 900, "corrida_id": c["id"]}).json()
    assert r["sos"]["ok"]
    assert cli.get(f"/api/corridas/{c['id']}").json()["sos"]["modo"] == "assalto"
    assert any("ALERTA SILENCIOSO" in n["texto"] for n in notificacoes.enviadas)
    assert any("MODO ASSALTO" in e["texto"] for e in cli.get("/api/eventos").json())


def test_bluetooth_desconectado_avisa_central():
    c = corrida_em_viagem()
    mid = c["motorista_id"]
    assert cli.post(f"/api/motoristas/{mid}/conexao", json={"bluetooth": False, "bateria": 12, "corrida_id": c["id"]}).json()["bluetooth"] is False
    cli.post(f"/api/motoristas/{mid}/bem-estar", json={"resposta": "sem_resposta", "motivo": "bluetooth", "corrida_id": c["id"]})
    ev = [e["texto"] for e in cli.get("/api/eventos").json()]
    assert any("desconectou do Bluetooth" in t and "Perguntamos" in t for t in ev)
    assert any("desconectou do Bluetooth" in t and "Não respondeu" in t for t in ev)
    assert any("Bateria do celular" in t for t in ev)
    cli.post(f"/api/motoristas/{mid}/conexao", json={"bluetooth": True})
    assert any("reconectou" in e["texto"] for e in cli.get("/api/eventos").json())


def test_cadastro_bluetooth_do_veiculo():
    base = {**PESSOA, "nome": "Rosana Lima", "sexo": "F", "declaracao_veracidade": True, "cnh": "04512378901",
            "cnh_validade": "2030-01-01", "crlv_validade": "2030-01-01", "placa": "RTA-4F21", "modelo": "BYD Dolphin",
            "cor": "Branco", "ano": 2021}
    r = cli.post("/api/motoristas", json={**base, "bluetooth_tipo": "adaptador"})
    assert r.status_code == 422 and "nome do Bluetooth" in str(r.json())
    assert cli.post("/api/motoristas", json={**base, "bluetooth_tipo": "nenhum"}).status_code == 201
    assert cli.post("/api/motoristas", json={**base, "bluetooth_tipo": "radio"}).status_code == 422


def test_cancelamento_passageira_gratis_ate_3_min_depois_taxa():
    from app.main import banco
    pid, mid = passageira(), motorista_pronta()
    def pedir():
        c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 120, "y": 140}}).json()
        cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid, "posicao": {"x": 100, "y": 150}})
        return c["id"]
    cid = pedir()
    assert cli.get(f"/api/corridas/{cid}/cancelamento").json()["gratis"] is True
    n = len(cli.get(f"/api/corridas/{cid}").json()["pagamentos"])
    cli.post(f"/api/corridas/{cid}/cancelar", json={"por": "passageira"})
    assert len(cli.get(f"/api/corridas/{cid}").json()["pagamentos"]) == n
    # depois de 3 minutos (no modo demo o relógio da espera anda 20x)
    cid = pedir()
    c = banco.corrida(cid); c["aceita_em"] -= 200; banco.salvar_corrida(c)
    prev = cli.get(f"/api/corridas/{cid}/cancelamento").json()
    assert prev["gratis"] is False and prev["taxa"]["total"] >= 5
    cli.post(f"/api/corridas/{cid}/cancelar", json={"por": "passageira"})
    r = cli.get(f"/api/corridas/{cid}").json()
    assert r["cancelamento"]["status_taxa"] == "cobrada"
    assert r["pagamentos"][-1]["motivo"] == "taxa-cancelamento" and r["pagamentos"][-1]["taxa_motora"] == 0
    # desistência após a chegada: soma a espera
    cid = pedir()
    cli.post(f"/api/corridas/{cid}/chegada")
    c = banco.corrida(cid); c["chegada_em"] -= 30; banco.salvar_corrida(c)   # 600 s de espera no relógio demo
    prev = cli.get(f"/api/corridas/{cid}/cancelamento").json()
    assert prev["desistencia"] and prev["taxa"]["deslocamento"] == 20.0  # 5 + 10 km x 2,20 passa do teto de R$ 20 and prev["taxa"]["espera"] > 0


def test_cancelamento_com_sinal_fraco_vai_para_analise():
    from app.main import banco
    pid, mid = passageira(), motorista_pronta()
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 120, "y": 140}}).json()
    cid = c["id"]
    cli.post(f"/api/corridas/{cid}/aceitar", json={"motorista_id": mid, "posicao": {"x": 100, "y": 145}})
    cli.post(f"/api/corridas/{cid}/sinal", json={"por": "passageira", "qualidade": "fraco"})
    c = banco.corrida(cid); c["aceita_em"] -= 200; banco.salvar_corrida(c)
    n = len(cli.get(f"/api/corridas/{cid}").json()["pagamentos"])
    cli.post(f"/api/corridas/{cid}/cancelar", json={"por": "passageira"})
    r = cli.get(f"/api/corridas/{cid}").json()
    assert r["cancelamento"]["status_taxa"] == "em_analise" and len(r["pagamentos"]) == n
    assert any("sinal de internet fraco" in e["texto"] for e in cli.get("/api/eventos").json())
    assert cli.post(f"/api/corridas/{cid}/taxa-cancelamento/decisao", json={"aprovar": False}).json()["status_taxa"] == "dispensada"


def test_cancelamento_motorista_penaliza_nota():
    from app import cancelamento as cn
    m = {"nota": 4.8}
    for _ in range(9):
        cn.registrar(m, True)
    assert cn.penalidade(m) == 0 and m["nota"] == 4.8
    cn.registrar(m, True)
    assert cn.penalidade(m) == 0.02 and m["nota"] == round(4.8 * 0.98, 2)
    for _ in range(50):
        cn.registrar(m, True)
    assert cn.penalidade(m) == 0.10
    for _ in range(100):
        cn.registrar(m, False)          # 100 corridas boas tiram os cancelamentos da janela
    assert cn.penalidade(m) == 0 and m["nota"] == 4.8
    # pela API: a corrida volta para a fila e quem cancelou não pode aceitar de novo
    pid, mid = passageira(), motorista_pronta()
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 120, "y": 140}}).json()
    cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid})
    r = cli.post(f"/api/corridas/{c['id']}/cancelar", json={"por": "motorista", "motivo": "pneu furado"}).json()
    assert r["status"] == "buscando" and r["penalidade"]["cancelamentos"] == 1
    assert cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid}).status_code == 403


def test_chegada_automatica_pelo_gps():
    pid, mid = passageira(), motorista_pronta()
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 120, "y": 140}}).json()
    cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": mid, "posicao": {"x": 100, "y": 150}})
    assert cli.post(f"/api/corridas/{c['id']}/posicao", json={"x": 100, "y": 145}).json()["chegada_automatica"] is False
    r = cli.post(f"/api/corridas/{c['id']}/posicao", json={"x": 100.02, "y": 140.02}).json()
    assert r["chegada_automatica"] is True and r["status"] == "no_embarque"
    assert cli.get(f"/api/corridas/{c['id']}").json()["chegada_automatica"] is True


def test_chat_da_corrida_oculta_telefone():
    c = corrida_em_viagem()
    cli.post(f"/api/corridas/{c['id']}/mensagens", json={"por": "passageira", "texto": "Estou de blusa azul"})
    m = cli.post(f"/api/corridas/{c['id']}/mensagens", json={"por": "motorista", "texto": "Me liga no (11) 98765-4321"}).json()
    assert m["oculto"] and "98765" not in m["texto"] and "[número oculto]" in m["texto"]
    lista = cli.get(f"/api/corridas/{c['id']}/mensagens").json()
    assert [x["por"] for x in lista] == ["passageira", "motorista"]
    assert cli.get(f"/api/corridas/{c['id']}/mensagens", params={"desde": lista[0]["quando"]}).json()[0]["por"] == "motorista"


def test_chat_e_link_de_acompanhamento_encerram_com_a_corrida():
    c = corrida_em_viagem()
    token = c["compartilhada_com"]["link"].rsplit("/", 1)[1]
    assert cli.get(f"/api/acompanhar/{token}").status_code == 200
    assert cli.post(f"/api/corridas/{c['id']}/mensagens", json={"por": "passageira", "texto": "Oi"}).status_code == 201
    cli.post(f"/api/corridas/{c['id']}/finalizar")
    r = cli.post(f"/api/corridas/{c['id']}/mensagens", json={"por": "motorista", "texto": "Oi"})
    assert r.status_code == 409 and "reclamação" in r.json()["detail"]
    assert len(cli.get(f"/api/corridas/{c['id']}/mensagens").json()) == 1     # histórico fica guardado
    assert cli.get(f"/api/acompanhar/{token}").status_code == 410
    assert cli.post(f"/api/corridas/{c['id']}/compartilhar").status_code == 409


def test_taxa_de_entrega_de_objeto():
    from app import reclamacoes as rc
    assert rc.taxa_entrega(0) == 30 and rc.taxa_entrega(15) == 30
    assert rc.taxa_entrega(25) == 40 and rc.taxa_entrega(35) == 50 and rc.taxa_entrega(90) == 50


def test_reclamacao_de_objeto_esquecido_com_entrega_cobrada():
    c = corrida_em_viagem()
    cli.post(f"/api/corridas/{c['id']}/finalizar")
    pid = c["passageira_id"]
    assert "não se responsabiliza" in cli.get("/api/reclamacoes/regras").json()["aviso_objetos"]
    r = cli.post("/api/reclamacoes", json={"por": "passageira", "autor_id": pid, "categoria": "objeto_esquecido",
                                           "corrida_id": c["id"], "descricao": "Deixei no banco de trás",
                                           "objeto_descricao": "Guarda-chuva preto", "entrega": "embarque"}).json()
    assert r["protocolo"].startswith("MOT-") and r["status"] == "aberta"
    assert r["objeto"]["taxa"] == rc_taxa(r["objeto"]["km"])
    assert any(x["id"] == r["id"] for x in cli.get("/api/reclamacoes", params={"autor_id": pid}).json())
    cli.post(f"/api/reclamacoes/{r['id']}/andamento", json={"status": "objeto_encontrado"})
    fim = cli.post(f"/api/reclamacoes/{r['id']}/andamento", json={"status": "entregue", "resposta": "Entregue na portaria"}).json()
    assert fim["status"] == "entregue" and fim["objeto"]["pagamento"] in ("pago", "isento")
    pg = cli.get(f"/api/corridas/{c['id']}").json()["pagamentos"][-1]
    assert pg["motivo"] == "entrega-objeto" and pg["valor"] == r["objeto"]["taxa"]
    assert cli.post(f"/api/reclamacoes/{r['id']}/andamento", json={"status": "resolvida"}).status_code == 409


def rc_taxa(km):
    from app import reclamacoes as rc
    return rc.taxa_entrega(km)


def test_reclamacao_de_regras_pelo_motorista_e_validacoes():
    c = corrida_em_viagem()
    mid = c["motorista_id"]
    ok = cli.post("/api/reclamacoes", json={"por": "motorista", "autor_id": mid, "categoria": "conduta",
                                            "corrida_id": c["id"], "descricao": "Passageira fumou no carro"})
    assert ok.status_code == 201 and ok.json()["categoria_texto"] == "Conduta da passageira"
    sem_corrida = cli.post("/api/reclamacoes", json={"por": "passageira", "autor_id": c["passageira_id"],
                                                     "categoria": "objeto_esquecido", "descricao": "Perdi meu fone"})
    assert sem_corrida.status_code == 422
    errada = cli.post("/api/reclamacoes", json={"por": "passageira", "autor_id": passageira(), "categoria": "conduta",
                                                "corrida_id": c["id"], "descricao": "Não fui eu nessa corrida"})
    assert errada.status_code == 403


def test_melhor_rota_desvia_de_acidente_e_obra():
    from app import transito as tr
    a, b = {"x": 100, "y": 140}, {"x": 150, "y": 110}
    padrao = tr.melhor_rota(a, b)
    assert padrao["rota"][1] == {"x": 150, "y": 140} and padrao["km"] == 80 and not padrao["observacoes"]
    acidente = {"tipo": "acidente", "x": 125, "y": 140}
    r = tr.melhor_rota(a, b, [acidente])
    assert r["rota"][1] == {"x": 100, "y": 110} and r["km"] == 80 and r["atraso_min"] == 0
    assert "acidente" in r["observacoes"][0]
    # bloqueio nas duas rotas diretas: escolhe um desvio pelo menor tempo
    obra = {"tipo": "obra", "x": 125, "y": 110}
    r2 = tr.melhor_rota(a, b, [acidente, obra])
    assert r2["atraso_min"] == 0 and r2["km"] == 80          # rota pelo meio (y=125) não passa perto de nenhuma
    blitz = tr.melhor_rota(a, b, [{"tipo": "blitz", "x": 125, "y": 140}])
    assert blitz["km"] == 80 and blitz["observacoes"] == ["Blitz no trajeto"]


def test_motorista_alerta_central_e_estimativa_usa_melhor_rota():
    mid = motorista_pronta()
    o = cli.post("/api/ocorrencias", json={"tipo": "acidente", "x": 125, "y": 140, "por_id": mid, "obs": "Pista da direita"})
    assert o.status_code == 201 and o.json()["regiao"]
    oc = o.json()
    assert any(x["id"] == oc["id"] for x in cli.get("/api/ocorrencias").json()["ocorrencias"])
    assert "alertou a central: acidente" in cli.get("/api/eventos").json()[0]["texto"]
    e = cli.post("/api/estimativa", json={"origem": {"x": 100, "y": 140}, "destino": {"x": 150, "y": 110}, "hora": 9}).json()
    assert e["rota"][1] == {"x": 100, "y": 110} and "Rota desviada" in e["observacoes_rota"][0]
    assert cli.post("/api/ocorrencias", json={"tipo": "show", "x": 1, "y": 1, "por_id": mid}).status_code == 422
    assert cli.delete(f"/api/ocorrencias/{oc['id']}").status_code == 200
    assert cli.post("/api/rota/melhor", json={"origem": {"x": 100, "y": 140}, "destino": {"x": 150, "y": 110}}).json()["rota"][1] == {"x": 150, "y": 140}


def test_favorita_exige_conduta_5_e_qualquer_lado_exclui():
    c = corrida_concluida()
    pid, mid = c["passageira_id"], c["motorista_id"]
    assert cli.post(f"/api/passageiras/{pid}/favoritos", json={"motorista_id": mid}).status_code == 422
    cli.post(f"/api/corridas/{c['id']}/avaliacao", json={"por": "passageira", "notas": NOTAS_PASS})   # conduta 5
    assert cli.post(f"/api/passageiras/{pid}/favoritos", json={"motorista_id": mid}).json()["favoritos"] == [mid]
    assert cli.get(f"/api/passageiras/{pid}/favoritos").json()[0]["nome"] == "Rosana Lima"
    assert cli.get(f"/api/motoristas/{mid}/favoritada-por").json() == [{"id": pid, "nome": "Juliana"}]
    assert cli.delete(f"/api/motoristas/{mid}/favoritada-por/{pid}").json()["favoritos"] == []
    cli.post(f"/api/passageiras/{pid}/favoritos", json={"motorista_id": mid})
    assert cli.delete(f"/api/passageiras/{pid}/favoritos/{mid}").json()["favoritos"] == []


def test_chamada_reservada_para_favoritas():
    c = corrida_concluida()
    pid, fav = c["passageira_id"], c["motorista_id"]
    cli.post(f"/api/corridas/{c['id']}/avaliacao", json={"por": "passageira", "notas": NOTAS_PASS})
    cli.post(f"/api/passageiras/{pid}/favoritos", json={"motorista_id": fav})
    outra = motorista_pronta()
    n = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 150, "y": 110},
                                       "hora": 9, "favoritos_primeiro": True}).json()
    assert n["favoritos_primeiro"] and n["reservada_favoritos_ate"]
    assert all(x["id"] != n["id"] for x in cli.get("/api/corridas/disponiveis", params={"motorista_id": outra}).json())
    assert cli.post(f"/api/corridas/{n['id']}/aceitar", json={"motorista_id": outra}).status_code == 409
    ok = cli.post(f"/api/corridas/{n['id']}/aceitar", json={"motorista_id": fav}).json()
    assert ok["status"] == "a_caminho" and ok["motorista_favorito"] is True


def test_corrida_agendada_com_prazo_e_alerta_de_transito():
    import time as _t
    pid, mid = passageira(), motorista_pronta()
    cedo = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 150, "y": 110},
                                          "hora": 9, "agendada_para": _t.time() + 600})
    assert cedo.status_code == 422
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 150, "y": 110},
                                       "hora": 9, "agendada_para": _t.time() + 7200}).json()
    assert c["status"] == "agendada"
    assert any(x["id"] == c["id"] for x in cli.get("/api/agendamentos", params={"motorista_id": mid}).json())
    assert all(x["id"] != c["id"] for x in cli.get("/api/corridas/disponiveis", params={"motorista_id": mid}).json())
    ac = cli.post(f"/api/corridas/{c['id']}/agendamento/aceitar", json={"motorista_id": mid, "posicao": {"x": 60, "y": 140}}).json()
    assert ac["motorista_id"] == mid and ac["prazo"]["alerta"] in ("no_prazo", "hora_de_sair")
    oc = cli.post("/api/ocorrencias", json={"tipo": "engarrafamento", "x": 80, "y": 140, "por_id": mid}).json()
    p = cli.get(f"/api/corridas/{c['id']}/prazo", params={"x": 60, "y": 140}).json()
    assert p["deslocamento_min"] == 78 and "Rota desviada" in p["observacoes"][0]   # 40 km a 40 km/h = 60 min; desvio de 52 km
    cli.delete(f"/api/ocorrencias/{oc['id']}")
    livre = cli.get(f"/api/corridas/{c['id']}/prazo", params={"x": 60, "y": 140}).json()
    assert livre["deslocamento_min"] == 60 and livre["sair_em_seg"] < p["faltam_seg"]
    ini = cli.post(f"/api/corridas/{c['id']}/agendamento/iniciar", json={"motorista_id": mid, "posicao": {"x": 60, "y": 140}}).json()
    assert ini["status"] == "a_caminho"


def test_cancelar_agendada_e_gratis_e_desistencia_reabre():
    import time as _t
    pid, mid = passageira(), motorista_pronta()
    novo = lambda: cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 150, "y": 110},
                                                   "hora": 9, "agendada_para": _t.time() + 7200}).json()
    c = novo()
    cli.post(f"/api/corridas/{c['id']}/agendamento/aceitar", json={"motorista_id": mid})
    d = cli.post(f"/api/corridas/{c['id']}/cancelar", json={"por": "motorista"}).json()
    assert d["status"] == "agendada" and d["motorista_id"] is None
    assert all(x["id"] != c["id"] for x in cli.get("/api/agendamentos", params={"motorista_id": mid}).json())
    x = cli.post(f"/api/corridas/{c['id']}/cancelar", json={"por": "passageira"}).json()
    assert x["status"] == "cancelada" and x["cancelamento"]["gratis"] is True


def _limpar_buscando():
    from app.main import banco
    for c in banco_corridas():
        if c["status"] == "buscando":
            cli.post(f"/api/corridas/{c['id']}/cancelar", json={"por": "passageira"})
    for m in banco.usuarios("motorista"):          # motoristas de outros testes saem do ar
        m["ultimo_ping"] = 0
        banco.salvar_usuario(m["id"], "motorista", m)


def banco_corridas():
    from app.main import banco
    return banco.corridas()


def _ofertas(mid, x, y):
    return cli.get("/api/corridas/disponiveis", params={"motorista_id": mid, "x": x, "y": y}).json()


def test_chamada_20s_ate_3_vezes_e_concentrada_com_poucas_motoristas(monkeypatch):
    from app import main as mm
    _limpar_buscando()
    pid = passageira()
    a, b = motorista_pronta(), motorista_pronta()
    relogio = [mm.agora()]
    monkeypatch.setattr(mm, "agora", lambda: relogio[0])
    _ofertas(a, 101, 140); _ofertas(b, 103, 140)                 # as duas a até 6 km do embarque
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 150, "y": 110}, "hora": 9}).json()
    quem = []
    for _ in range(6):
        oa, ob = _ofertas(a, 101, 140), _ofertas(b, 103, 140)
        assert len(oa) + len(ob) == 1                               # uma pessoa por vez
        of = (oa or ob)[0]["oferta"]
        assert of["expira_em_seg"] <= 20 and not of["concentrada"]
        quem.append(of["motorista_id"])
        relogio[0] += 21                                           # deixou passar os 20 s
    assert quem.count(a) == 3 and quem.count(b) == 3 and quem[0] == a   # a mais perto primeiro, alternando
    # depois de 3 vezes cada, a chamada se concentra na mais perto
    of = (_ofertas(a, 101, 140) or _ofertas(b, 103, 140))[0]["oferta"]
    assert of["concentrada"] and of["motorista_id"] == a
    # quem não tem a oferta não consegue aceitar
    relogio[0] += 21
    of = (_ofertas(a, 101, 140) or _ofertas(b, 103, 140))[0]["oferta"]
    outra = b if of["motorista_id"] == a else a
    assert cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": outra}).status_code == 409
    assert cli.post(f"/api/corridas/{c['id']}/aceitar", json={"motorista_id": of["motorista_id"]}).status_code == 200


def test_embarque_ate_6km_salvo_poucas_motoristas_e_recusa(monkeypatch):
    from app import main as mm
    _limpar_buscando()
    pid = passageira()
    perto1, perto2, longe = motorista_pronta(), motorista_pronta(), motorista_pronta()
    for mid, x in ((perto1, 102), (perto2, 104), (longe, 130)):
        _ofertas(mid, x, 140)
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 150, "y": 110}, "hora": 9}).json()
    vistos = set()
    for _ in range(4):
        for mid, x in ((perto1, 102), (perto2, 104), (longe, 130)):
            o = _ofertas(mid, x, 140)
            if o:
                vistos.add(mid)
                cli.post(f"/api/corridas/{c['id']}/recusar", json={"motorista_id": mid})
    assert longe not in vistos and vistos == {perto1, perto2}       # duas a até 6 km: a de 30 km não recebe
    cli.post(f"/api/corridas/{c['id']}/cancelar", json={"por": "passageira"})
    # com uma só a até 6 km, a regra é ignorada e a de 30 km também entra na rodada
    _limpar_buscando()
    s1, s2 = motorista_pronta(), motorista_pronta()
    _ofertas(s1, 103, 140); _ofertas(s2, 125, 140)
    c2 = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 140}, "destino": {"x": 150, "y": 110}, "hora": 9}).json()
    assert _ofertas(s1, 103, 140)[0]["oferta"]["motorista_id"] == s1
    cli.post(f"/api/corridas/{c2['id']}/recusar", json={"motorista_id": s1})
    of = _ofertas(s2, 125, 140)[0]["oferta"]
    assert of["regra_6km_ignorada"] and of["distancia_km"] == 25


def test_area_de_atuacao_15km_em_linha_reta():
    _limpar_buscando()
    pid, mid = passageira(), motorista_pronta()
    assert any(b["nome"] == "Pinheiros" for b in cli.get("/api/bairros").json()["bairros"])
    assert cli.put(f"/api/motoristas/{mid}/area", json={"bairro": "Atlântida"}).status_code == 422
    ar = cli.put(f"/api/motoristas/{mid}/area", json={"x": 100, "y": 100, "nome": "Rua das Flores, 10"}).json()
    assert ar["raio_km"] == 15 and ar["nome"] == "Rua das Flores, 10"
    # embarque a 14,1 km em linha reta (mas 20 km pelas ruas): entra; a 20 km em linha reta: fica fora
    c = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 110, "y": 110}, "destino": {"x": 150, "y": 110}, "hora": 9}).json()
    assert _ofertas(mid, 108, 110)[0]["id"] == c["id"]
    cli.post(f"/api/corridas/{c['id']}/cancelar", json={"por": "passageira"})
    c2 = cli.post("/api/corridas", json={"passageira_id": pid, "origem": {"x": 100, "y": 120}, "destino": {"x": 150, "y": 110}, "hora": 9}).json()
    assert _ofertas(mid, 100, 118) == []
    cli.delete(f"/api/motoristas/{mid}/area")
    assert _ofertas(mid, 100, 118)[0]["id"] == c2["id"]
    cli.post(f"/api/corridas/{c2['id']}/cancelar", json={"por": "passageira"})
