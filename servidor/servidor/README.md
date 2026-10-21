# Motora: servidor em Python

App de corridas para passageiras mulheres, com motoristas homens ou mulheres e frota só elétrica ou híbrida. O servidor guarda cadastros e corridas, calcula a tarifa e avisa o contato de emergência. Ele também serve o próprio app, então tudo fica num endereço só.

## O que o servidor faz

- **Cadastro com validação.** Passageira precisa declarar que é mulher. A pessoa motorista informa o sexo, e o carro precisa ser elétrico ou híbrido da lista. Todos precisam ter 18 anos ou mais, CPF e contato de emergência.
- **Corridas de até 100 km**, com tarifa calculada no servidor: bandeirada, km, minuto, espera no embarque após 2 minutos grátis, paradas após 1 minuto grátis em cada, +20% das 22h às 6h e dinâmica por região em 8 níveis (1x a 2,4x).
- **Compras no porta-malas.** A passageira informa quantas sacolas ou caixas leva (bagagem de mão não conta). Até 2 não pagam; de 3 a 4 volumes +3%, 5 a 6 +5%, 7 a 8 +7% e 9 ou mais +10% sobre o valor da corrida.
- **Compartilhamento automático.** Quando a corrida começa (código de embarque confirmado), o contato de emergência da passageira recebe por SMS ou WhatsApp um link para acompanhar a rota ao vivo. Na chegada, recebe outra mensagem avisando.
- **Página de acompanhamento** em `/acompanhar/<código>`, sem login. Mostra o mapa, o carro, a placa e os alertas, e expira 1 hora depois do fim da corrida.
- **Monitoramento de rota.** Quando o carro passa de 1 km fora do trajeto, a central é alertada.
- **Rota alternativa no trânsito.** A pessoa motorista propõe a rota e a passageira só aceita depois de confirmar que viu o engarrafamento. O valor extra é cobrado antes.
- **Pagamentos com Stripe Connect.** A passageira salva um cartão e paga no fim da corrida. A Stripe divide cada cobrança na hora: 85% para a conta da pessoa motorista e 15% para a Motora. O valor extra da rota alternativa é cobrado no momento do aceite.
- **Motora Fiel (fidelidade).** A passageira entra no programa com um toque, de graça, e ganha 5% de desconto em toda corrida. O desconto vale só sobre km e tempo, incluindo a dinâmica deles. A bandeirada, a espera e a rota alternativa ficam de fora. O desconto sai da parte da Motora: quem dirige continua recebendo 85% do valor cheio.
- **Alerta de sinal fraco.** Cada aparelho mede a própria internet, usando a rede informada pelo navegador e o tempo de resposta do servidor, e envia o resultado para `/api/corridas/<id>/sinal`. Quando o sinal de um lado fica fraco, os dois lados veem um aviso de que a localização e o horário de chegada ao embarque podem estar imprecisos. O servidor também marca o sinal de quem dirige como fraco quando passa mais de 30 segundos sem receber uma posição nova. A página do contato de emergência mostra o mesmo aviso.
- **Mapa de zonas de sinal fraco.** Cada aviso de sinal fraco vira um relato no quadrado de 10 km onde aconteceu. Um quadrado vira zona automática quando junta relatos de corridas diferentes nos últimos 7 dias: 3 relatos em produção e 1 no modo demo, valor que pode ser ajustado em `MOTORA_ZONA_MIN_RELATOS`. Na aba Central, a equipe pode marcar zonas à mão (túnel, área rural), com raio e observação, e dispensar zonas automáticas. Uma zona dispensada volta se chegarem relatos novos. Quando o embarque, o destino ou o trajeto cai numa zona, o pedido, a chamada da motorista, a corrida e a página do contato mostram o alerta. Rotas: `GET/POST /api/zonas-sinal`, `DELETE /api/zonas-sinal/<id>`, `GET /api/zonas-sinal/verificar?x=&y=`.
- **Avaliação mútua.** Ao fim da corrida, passageira e quem dirige dão de 1 a 5 estrelas uma à outra em cada critério. A passageira avalia conduta, tratamento, respeito mútuo, condução do veículo e zelo pelo veículo. Quem dirige avalia conduta, tratamento, respeito mútuo e zelo pelo veículo. A nota geral é a média dos critérios.
- **Dicas de bom convívio.** Aparecem para as duas pessoas no app e precisam ser aceitas no cadastro: volume baixo, sem conversas obscenas ou gritaria, sem bater a porta, vidros fechados com ar ligado, sem sujeira e sem bebidas.
- **Taxa de limpeza.** Quem dirige pode pedir até R$ 250 em até 48 horas após a corrida, anexando fotos do veículo e a nota fiscal do serviço de limpeza. A passageira pode contestar, e só é cobrada se a Central aprovar. O valor vai 100% para quem dirige. Rotas: `POST /api/corridas/<id>/taxa-limpeza`, `.../taxa-limpeza/contestar`, `.../taxa-limpeza/decisao`, `GET /api/taxas-limpeza`.
- **Socorro Motora.** Durante a corrida, as duas pessoas podem pedir apoio da Central (recusa a sair, discussão, problema no veículo, reparo no pneu, socorro mecânico, socorro de bateria, objeto esquecido etc.); para pneu e bateria, sugere o parceiro credenciado mais perto, sem ser emergência policial. A Central atende e encerra. Rotas: `POST /api/corridas/<id>/apoio`, `GET /api/apoios`, `POST /api/corridas/<id>/apoio/<id>/atender|encerrar`.
- **Motora Verde (rede credenciada).** Postos de recarga elétrica e de etanol, borracharias e serviços de bateria dão desconto a quem dirige com nota 4 ou 5 estrelas: Verde 5%, Prata 10% (60 corridas em 30 dias) e Ouro 15% (120 corridas), até R$ 300 por mês. Desconto em combustível só para o combustível limpo do veículo. Quem dirige gera um cupom no app e o parceiro valida com o valor. A Central credencia e descredencia parceiros. Rotas: `GET/POST /api/postos`, `DELETE /api/postos/<id>`, `GET /api/motoristas/<id>/motora-verde`, `POST /api/motoristas/<id>/motora-verde/cupom`, `POST /api/postos/cupom`.
- **Jornada.** No máximo 12 horas de trabalho (tempo disponível no app). Ao chegar a 12 h, a corrida em andamento termina e a conta fica 6 horas em descanso obrigatório. Pausas menores que 6 h não zeram a jornada. Rotas: `POST /api/motoristas/<id>/disponibilidade`, `GET /api/motoristas/<id>/jornada`.
- **Vencimento da CNH e do CRLV.** As validades entram no cadastro, que recusa documento vencido. O app avisa 30 dias antes e, vencido, quem dirige não fica disponível nem aceita corridas até enviar o documento renovado. Rotas: `GET/PUT /api/motoristas/<id>/documentos`, `GET /api/documentos/vencimentos`.
- **Prova de vida.** Quem dirige tira uma selfie, comparada com a foto do cadastro, a cada volta ao app. Ela vale 60 minutos; cada nova saída do app nas últimas 24 horas encurta a próxima para 45, 30 e 15 minutos (mínimo). Sem prova válida, não fica disponível nem aceita corridas. No protótipo o reconhecimento facial é simulado. Rotas: `GET/POST /api/motoristas/<id>/prova-de-vida`.
- **Parada longa e Modo assalto.** Depois de 10 minutos parada(o) na espera ou numa parada (30 minutos disponível sem chamada), o app pergunta se está tudo bem: estou bem, preciso de ajuda, ligar 190 ou Modo assalto. Sem resposta em 60 segundos, a central é avisada. O Modo assalto (também no botão SOS) manda um alerta silencioso à central e ao contato de emergência e deixa a tela com cara de app comum; para sair, digitam-se os 4 últimos dígitos do CPF. Rotas: `POST /api/motoristas/<id>/bem-estar`, `POST /api/corridas/<id>/sos` com `modo: "assalto"`.
- **Recomendações e Bluetooth do carro.** O app de quem dirige traz recomendações: avisar a passageira sobre o cinto e os itens de segurança, vidros fechados com ar ligado, celular carregado e conectado ao Bluetooth do carro. No cadastro, o Bluetooth do veículo entra como item de segurança (multimídia do veículo, adaptador ou nenhum, com o nome do aparelho). Se o celular desconectar durante o trabalho, o app pergunta se está tudo bem e, sem resposta em 60 segundos, avisa a central. Bateria abaixo de 20% gera aviso. No navegador a conexão é simulada; o app nativo lê o Bluetooth de verdade. Rota: `POST /api/motoristas/<id>/conexao`.
- **Cancelamento.** A passageira cancela grátis até 3 minutos depois do aceite. Depois, paga a taxa de deslocamento (R$ 5 + R$ 2,20 por km já rodado por quem dirige, até R$ 20); se quem dirige já chegou (desistência), soma a espera. A taxa vai 100% para quem dirige. Com sinal de internet fraco em qualquer lado, a taxa não é cobrada na hora e vai para análise da central. Quem dirige que cancela perde 2% da nota a cada 10 cancelamentos nas últimas 100 corridas aceitas, até 10%; a passageira volta para a fila. Rotas: `GET /api/corridas/<id>/cancelamento`, `POST /api/corridas/<id>/cancelar` (`por`), `POST /api/corridas/<id>/taxa-cancelamento/decisao`, `GET /api/motoristas/<id>/cancelamentos`.
- **Chegada automática pelo GPS.** Quando o carro chega a 50 m do embarque, a chegada é marcada sozinha, a espera começa e a passageira é avisada. Quem dirige pode desligar e marcar "Cheguei" à mão.
- **Mensagens.** Chat entre passageira e motorista da aceitação até o fim da corrida, com respostas rápidas. Depois disso o chat fecha (o histórico fica guardado) e o assunto vai para a central por reclamação. Números de telefone são ocultados. Rotas: `GET/POST /api/corridas/<id>/mensagens`.
- **Compartilhar a rota pelo WhatsApp.** A passageira toca em "Enviar rota pelo WhatsApp" e o WhatsApp abre com a mensagem pronta para o contato de emergência (ou outro contato). O link leva à página de acompanhamento, que tem o botão "Abrir no meu app de mapas": no Android abre o app de mapas padrão do celular (link `geo:`), no iPhone abre o Mapas, e há atalhos para Google Maps, Waze e Apple Mapas. O mapa simulado é convertido para coordenadas reais a partir de `MOTORA_MAPA_LAT`/`MOTORA_MAPA_LNG` (padrão: Praça da Sé, São Paulo); pontos de GPS que já trazem `lat`/`lng` são usados como estão. O link vale só durante a corrida: quando ela termina ou é cancelada, a página mostra "Corrida encerrada". Rota: `POST /api/corridas/<id>/compartilhar`.
- **Reclamações e objetos esquecidos.** Passageira e motorista abrem reclamações para a central sobre descumprimento das regras ou objeto esquecido, com protocolo e acompanhamento. A Motora não se responsabiliza por objetos esquecidos. Se o objeto for encontrado, a entrega custa R$ 30 até 15 km e R$ 1 por km a mais, até R$ 50 (medido do destino da corrida até o endereço de entrega); retirar combinando com a central não tem custo. A taxa é cobrada só na entrega e vai 100% para quem dirige. Rotas: `GET /api/reclamacoes/regras`, `POST /api/reclamacoes`, `GET /api/reclamacoes?autor_id=`, `POST /api/reclamacoes/<id>/andamento`.
- **Taxa fixa de deslocamento.** Toda corrida soma R$ 3,00 ao total (`regras.TAXA_DESLOCAMENTO`). Esse valor vai inteiro para quem dirige, fora da divisão 85/15.
- **Melhor rota e alertas de trânsito.** Quem dirige avisa a central sobre acidente, obra, engarrafamento, policiamento ou blitz no ponto onde está. A distância e o tempo de toda corrida usam a melhor rota: acidente soma 15 min, obra 10 min e engarrafamento 20 min para quem passa a até 1 km, e a rota desvia pelo menor tempo. Policiamento e blitz aparecem como aviso. Rotas: `GET/POST /api/ocorrencias`, `DELETE /api/ocorrencias/<id>`, `POST /api/rota/melhor`.
- **Motoristas favoritas.** A passageira favorita quem dirigiu, desde que tenha dado 5 estrelas em conduta numa corrida. Os dois lados podem excluir. Com "Chamar minhas favoritas primeiro", a chamada fica 1 minuto só com as favoritas. Rotas: `POST/GET /api/passageiras/<id>/favoritos`, `DELETE /api/passageiras/<id>/favoritos/<motorista>`, `GET /api/motoristas/<id>/favoritada-por`, `DELETE /api/motoristas/<id>/favoritada-por/<passageira>`.
- **Corridas agendadas.** De 30 minutos a 7 dias antes, com qualquer motorista ou uma favorita. Quem aceita recebe o alerta de prazo: o horário de sair, contando o deslocamento pela melhor rota e o trânsito, para chegar 10 minutos antes. Se não for chegar a tempo, a central e a passageira são avisadas. Cancelar é grátis até a pessoa motorista sair. Rotas: `POST /api/corridas` com `agendada_para`, `GET /api/agendamentos`, `POST /api/corridas/<id>/agendamento/aceitar`, `GET /api/corridas/<id>/prazo`, `POST /api/corridas/<id>/agendamento/iniciar`.
- **Área de atuação.** Quem dirige escolhe um bairro ou endereço e só recebe embarques a até 15 km em linha reta dele (sem contar o caminho pelas ruas). Com GPS real, troque a lista `despacho.BAIRROS` por geocodificação. Rotas: `GET /api/bairros`, `PUT/DELETE /api/motoristas/<id>/area`.
- **Despacho das chamadas.** `GET /api/corridas/disponiveis?motorista_id=&x=&y=` informa a posição e devolve a chamada oferecida a essa pessoa. Cada oferta dura 20 s; se não aceitar (ou recusar em `POST /api/corridas/<id>/recusar`), passa para a próxima, e cada pessoa recebe até 3 vezes. O embarque pode estar a até 6 km em linha reta; com menos de 2 pessoas nesse raio, a regra é ignorada e vale a mais perto. Com uma só pessoa disponível, ou quando todas já receberam 3 vezes, a chamada se concentra na mais perto até chegar outra.
- **Resumo das regras.** `GET /api/regras/resumo` devolve todas as regras em poucas palavras, o mesmo resumo que aparece no app.
- **Denúncias e bloqueio.** Na mesma tela dá para marcar assédio, desrespeito, imoralidade, humilhação, violência física ou violência verbal, contar o que aconteceu e bloquear a outra pessoa, que não será mais pareada com quem bloqueou. Assédio, imoralidade e violência física suspendem a conta denunciada na hora. A Central analisa cada denúncia e decide entre reativar a conta ou manter a suspensão. Rotas: `POST /api/corridas/<id>/avaliacao`, `GET /api/denuncias`, `POST /api/denuncias/<id>/decisao`, `GET /api/usuarios/<id>/reputacao`.
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

## Pagamentos: ligar a Stripe Connect

Sem chaves configuradas, os pagamentos rodam em **modo simulado**: o app mostra um cartão de teste, e as cobranças aparecem como pagas sem mover dinheiro.

Para usar a Stripe de verdade:

1. **Crie a conta da Motora** em dashboard.stripe.com, com o CNPJ da empresa e a conta bancária que vai receber os 15%.
2. **Ative o Connect** no painel (menu Connect). Escolha contas **Express** e preencha o perfil da plataforma.
3. **Copie as chaves de teste** em Desenvolvedores > Chaves de API e configure no servidor:
   - `STRIPE_SECRET_KEY=sk_test_...`
   - `STRIPE_PUBLISHABLE_KEY=pk_test_...`
4. **Crie o webhook** em Desenvolvedores > Webhooks, apontando para `https://SEU-ENDERECO/api/stripe/webhook`, com os eventos `account.updated`, `payment_intent.succeeded` e `payment_intent.payment_failed`. Copie o segredo para `STRIPE_WEBHOOK_SECRET=whsec_...`.
5. **Teste.** No app, a pessoa motorista toca em "Conectar conta de recebimento" e preenche o cadastro na página da Stripe. Em modo de teste, a Stripe aceita dados fictícios. A passageira adiciona o cartão `4242 4242 4242 4242`, com qualquer validade futura e qualquer CVC.
6. **Produção.** Troque as chaves `sk_test`/`pk_test` pelas `sk_live`/`pk_live` depois que a Stripe aprovar a conta.

Como o dinheiro anda:

- A passageira salva o cartão uma vez (SetupIntent). Nem o servidor nem o app guardam o número do cartão.
- Ao fim da corrida, o servidor cria a cobrança (PaymentIntent) com `transfer_data.destination` apontando para a conta da pessoa motorista e `application_fee_amount` de 15%.
- A Stripe repassa o dinheiro para a conta bancária da pessoa motorista conforme o calendário de saques da conta. As tarifas da Stripe saem da parte da Motora.
- Uma cobrança recusada fica registrada na corrida com o status `falhou`, e a central é avisada.

Pix: a Stripe aceita Pix no Brasil, mas cada Pix precisa ser pago pela passageira na hora, lendo um QR code. Ele não fica salvo como o cartão. Dá para acrescentar o Pix como opção de pagamento no fim da corrida.

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
| `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY` | Pagamentos reais (ou de teste) pela Stripe | modo simulado |
| `STRIPE_WEBHOOK_SECRET` | Confere os avisos enviados pela Stripe | — |

## Arquivos

```
servidor/
  app/main.py           rotas da API e fluxo da corrida
  app/regras.py         tarifa, limite de 100 km, regiões, frota permitida
  app/db.py             banco SQLite
  app/notificacoes.py   SMS/WhatsApp para o contato de emergência
  app/avaliacoes.py     avaliação por critério, denúncias, bloqueio e suspensão
  app/postos.py         Motora Verde: rede credenciada, níveis e cupons
  app/jornada.py        limite de 12 h de trabalho e 6 h de descanso
  app/documentos.py     vencimento da CNH e do CRLV
  app/vida.py           prova de vida e checagem de parada longa
  app/zonas.py          mapa de zonas de sinal fraco (relatos, zonas automáticas e manuais)
  app/pagamentos.py     Stripe Connect: contas das motoristas, cartão e divisão 85/15
  static/index.html     o app (passageira, motorista e central)
  static/acompanhar.html  página que o contato de emergência abre
  gerar_index.py        recria static/index.html a partir do motora.html
  tests/test_api.py     testes automáticos
```

## Antes de lançar de verdade

Este servidor é a base do produto, mas ainda faltam algumas peças:

- **Login e senha** (por exemplo, código por SMS). Hoje qualquer pessoa com o ID de uma corrida consegue agir sobre ela.
- **GPS e mapa reais.** O app usa um mapa simulado em km. Para ruas de verdade, troque as funções de `app/regras.py` por latitude/longitude e uma API de rotas (Google Maps, Mapbox ou OSRM).
- **Verificação de identidade e de antecedentes**, com um serviço de biometria facial e consulta de documentos.
- **Apps de celular.** O app atual roda no navegador do celular. Para publicar nas lojas, ele pode ser empacotado com Capacitor ou refeito em React Native, usando esta mesma API.
