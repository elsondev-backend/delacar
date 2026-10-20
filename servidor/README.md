# Motora: servidor em Python

App de corridas para passageiras mulheres, com motoristas homens ou mulheres e frota só elétrica ou híbrida. O servidor guarda cadastros e corridas, calcula a tarifa e avisa o contato de emergência. Ele também serve o próprio app, então tudo fica num endereço só.

## O que o servidor faz

- **Cadastro com validação.** Passageira precisa declarar que é mulher. A pessoa motorista informa o sexo, e o carro precisa ser elétrico ou híbrido da lista. Todos precisam ter 18 anos ou mais, CPF e contato de emergência.
- **Corridas de até 100 km**, com tarifa calculada no servidor: bandeirada, km, minuto, espera após 3 minutos grátis, +20% das 22h às 6h e dinâmica por região.
- **Compartilhamento automático.** Quando a corrida começa (código de embarque confirmado), o contato de emergência da passageira recebe por SMS ou WhatsApp um link para acompanhar a rota ao vivo. Na chegada, recebe outra mensagem avisando.
- **Página de acompanhamento** em `/acompanhar/<código>`, sem login. Mostra o mapa, o carro, a placa e os alertas, e expira 1 hora depois do fim da corrida.
- **Monitoramento de rota.** Quando o carro passa de 1 km fora do trajeto, a central é alertada.
- **Rota alternativa no trânsito.** A pessoa motorista propõe a rota e a passageira só aceita depois de confirmar que viu o engarrafamento. O valor extra é cobrado antes.
- **SOS** da passageira ou da pessoa motorista. Avisa o contato de emergência com o link ao vivo e registra o alerta na central.

## Rodar no seu computador

É preciso ter Python 3.10 ou mais novo.

```bash
cd servidor
python -m venv .venv
source .venv/bin/activate        # no Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Depois abra:

- http://localhost:8000 para o app
- http://localhost:8000/docs para a documentação da API, com botão para testar cada rota

Sem as chaves da Twilio configuradas, as mensagens ao contato aparecem no terminal com o aviso `[modo teste]`.

Para rodar os testes:

```bash
pip install -r requirements-dev.txt
pytest
```

## Publicar na internet (Render, plano gratuito)

1. Crie um repositório no GitHub e envie a pasta `servidor`.
2. Em render.com, escolha **New > Blueprint** e selecione o repositório. O arquivo `render.yaml` já traz a configuração.
3. Quando terminar, o Render mostra um endereço como `https://motora.onrender.com`. Esse é o link do app.

O Railway e o Fly.io também funcionam: o `Procfile` e o `Dockerfile` já estão prontos.

No plano gratuito do Render, o arquivo do banco SQLite é apagado a cada nova publicação. Para guardar dados de verdade, use um disco persistente ou troque para PostgreSQL.

## Configuração (variáveis de ambiente)

| Variável | Para que serve | Padrão |
|---|---|---|
| `MOTORA_MODO_DEMO` | `1` aceita o horário escolhido no app e acelera o relógio da espera. Use `0` em produção. | `1` |
| `MOTORA_FATOR_TEMPO` | Quantos segundos de espera contam por segundo real | `20` no demo, `1` fora dele |
| `MOTORA_DB` | Caminho do banco SQLite | `motora.db` |
| `MOTORA_URL_PUBLICA` | Endereço público usado nos links enviados (ex.: `https://motora.onrender.com`) | endereço da requisição |
| `MOTORA_FUSO` | Fuso usado para a tarifa noturna | `America/Sao_Paulo` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` | Envio real de SMS ou WhatsApp | sem envio real |
| `MOTORA_CANAL` | `sms` ou `whatsapp` | `sms` |

## Arquivos

```
servidor/
  app/main.py           rotas da API e fluxo da corrida
  app/regras.py         tarifa, limite de 100 km, regiões, frota permitida
  app/db.py             banco SQLite
  app/notificacoes.py   SMS/WhatsApp para o contato de emergência
  static/index.html     o app (passageira, motorista e central)
  static/acompanhar.html  página que o contato de emergência abre
  gerar_index.py        recria static/index.html a partir do motora.html
  tests/test_api.py     testes automáticos
```

## Antes de lançar de verdade

Este servidor é a base do produto, mas ainda faltam algumas peças:

- **Login e senha** (por exemplo, código por SMS). Hoje qualquer pessoa com o ID de uma corrida consegue agir sobre ela.
- **GPS e mapa reais.** O app usa um mapa simulado em km. Para ruas de verdade, troque as funções de `app/regras.py` por latitude/longitude e uma API de rotas (Google Maps, Mapbox ou OSRM).
- **Pagamento** (Pix ou cartão) no ponto marcado em `responder_rota` e `finalizar`.
- **Verificação de identidade e de antecedentes**, com um serviço de biometria facial e consulta de documentos.
- **Apps de celular.** O app atual roda no navegador do celular. Para publicar nas lojas, ele pode ser empacotado com Capacitor ou refeito em React Native, usando esta mesma API.
