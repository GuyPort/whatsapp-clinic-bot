# 🤖 WhatsApp Clinic Bot

Bot inteligente de WhatsApp para automatização de agendamentos e atendimento de clínicas médicas.

## 🎯 Funcionalidades

- ✅ **Responder dúvidas** sobre valores, horários, endereço e convênios
- ✅ **Agendar consultas** automaticamente com validação de horários
- ✅ **Cancelar e remarcar**  com verificação de identidade
- ✅ **Integração com Google Calendar** para sincronização de agendamentos
- ✅ **Operação 24/7** com respostas instantâneas
- ✅ **Escalação inteligente** para atendimento humano quando necesconsultassário
- ✅ **Regras de agendamento** configuráveis (antecedência mínima, bloqueio de horários, etc)
- ✅ **Tom cordial e profissional** em português brasileiro

## 🛠️ Tecnologias

- **IA**: Claude 3.5 Sonnet (Anthropic)
- **WhatsApp**: Evolution API
- **Backend**: Python 3.11+ com FastAPI
- **Banco de Dados**: PostgreSQL (Railway) com SQLAlchemy
- **Calendário**: Google Calendar API
- **Deploy**: Railway / Render

## 📁 Estrutura do Projeto

```
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + webhooks
│   ├── config.py            # Configurações
│   ├── database.py          # Setup do banco
│   ├── models.py            # Modelos SQLAlchemy
│   ├── ai_agent.py          # Lógica do agente IA
│   ├── whatsapp_service.py  # Cliente Evolution API
│   ├── calendar_service.py  # Cliente Google Calendar
│   ├── appointment_rules.py # Regras de agendamento
│   └── utils.py             # Funções auxiliares
├── data/
│   ├── clinic_info.json     # Dados da clínica (EDITÁVEL)
│   └── appointments.db      # (Opcional para testes locais simples; produção usa PostgreSQL)
├── requirements.txt
├── .env                     # Variáveis de ambiente (criar baseado em .env.example)
├── .gitignore
├── railway.json
└── README.md
```

## 🚀 Setup Local

### 1. Pré-requisitos

- Python 3.11 ou superior
- Conta na Anthropic (para API do Claude)
- Evolution API configurada
- Google Cloud Project com Calendar API habilitada
- Instância PostgreSQL acessível (ex.: Railway Postgres)
- Instância Redis acessível (ex.: Railway Redis)

### 2. Instalação

```bash
# Clone o repositório
git clone <seu-repositorio>
cd <nome-do-projeto>

# Crie e ative ambiente virtual
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate

# Instale dependências
pip install -r requirements.txt
```

### 3. Configuração

#### 3.1 Variáveis de Ambiente

Crie um arquivo `.env` na raiz do projeto:

```env
# Anthropic API (Claude)
ANTHROPIC_API_KEY=sk-ant-sua-chave-aqui

# Evolution API (WhatsApp)
EVOLUTION_API_URL=https://sua-evolution-api.com
EVOLUTION_API_KEY=sua-api-key
EVOLUTION_INSTANCE_NAME=clinica-bot

# Google Calendar
GOOGLE_CALENDAR_ID=seu-calendario@group.calendar.google.com
GOOGLE_SERVICE_ACCOUNT_FILE=google-credentials.json

# Database (use URL do seu PostgreSQL)
DATABASE_URL=postgresql://usuario:senha@host:5432/nome-do-banco

# Configurações gerais
ENVIRONMENT=development
LOG_LEVEL=INFO
```

Após definir as variáveis, inicialize o banco executando `python run.py` (ou chamando `POST /admin/init-db`) para criar as tabelas no PostgreSQL.

#### 3.2 Google Calendar

1. Acesse o [Google Cloud Console](https://console.cloud.google.com/)
2. Crie um novo projeto ou use um existente
3. Ative a **Google Calendar API**
4. Crie uma **Service Account**:
   - IAM & Admin → Service Accounts → Create Service Account
   - Gere uma chave JSON e salve como `google-credentials.json` na raiz
5. Compartilhe seu Google Calendar com o email da Service Account (com permissão de "Make changes to events")

#### 3.3 Evolution API

1. Configure sua instância do Evolution API (você pode usar uma instância hospedada ou self-hosted)
2. Crie uma instância chamada `clinica-bot` (ou o nome definido no .env)
3. Escaneie o QR code para conectar o WhatsApp
4. Configure o webhook:
   - URL: `https://seu-dominio.com/webhook/whatsapp`
   - Events: `messages.upsert`

#### 3.4 Informações da Clínica

Edite o arquivo `data/clinic_info.json` com os dados da sua clínica:

```json
{
  "nome_clinica": "Clínica Exemplo",
  "medica_responsavel": "Dra. Maria Silva",
  "especialidade": "Dermatologia",
  "endereco": "Rua Exemplo, 123 - Centro",
  "telefone_contato": "(11) 91234-5678",
  "horario_funcionamento": {
    "segunda": "08:00-18:00",
    "terca": "08:00-18:00",
    "quarta": "08:00-18:00",
    "quinta": "08:00-18:00",
    "sexta": "08:00-18:00",
    "sabado": "08:00-12:00",
    "domingo": "FECHADO"
  },
  "tipos_consulta": [
    {
      "tipo": "Consulta Dermatológica",
      "duracao_minutos": 30,
      "valor_particular": 350.00,
      "convenios_aceitos": ["particular", "unimed"]
    }
  ],
  "regras_agendamento": {
    "dias_minimos_antecedencia": 2,
    "intervalo_entre_consultas_minutos": 15
  }
}
```

### 4. Executar Localmente

```bash
# Ativar ambiente virtual (se não estiver ativo)
venv\Scripts\activate  # Windows
source venv/bin/activate  # Linux/Mac

# Rodar servidor
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Acesse: `http://localhost:8000`

### 5. Atualização de banco – lembretes 24h

Ao atualizar para a versão que envia lembretes automáticos, adicione a coluna `reminder_sent_at` na tabela `appointments`:

```sql
ALTER TABLE appointments ADD COLUMN reminder_sent_at TIMESTAMP NULL;
CREATE INDEX ix_appointments_reminder_sent_at ON appointments(reminder_sent_at);
```

- **PostgreSQL**: execute os comandos acima antes de reiniciar o serviço.
- **SQLite (ambiental local)**: abra `sqlite3 data/appointments.db` e rode as mesmas instruções.

Somente após aplicar a migração reinicie o bot para evitar falhas ao persistir novos agendamentos.

## 🌐 Deploy (Railway)

### 1. Preparação

1. Crie conta no [Railway](https://railway.app/)
2. Instale o Railway CLI (opcional)
3. Faça commit do código no GitHub

### 2. Deploy

1. No Railway, clique em **New Project**
2. Selecione **Deploy from GitHub repo**
3. Conecte seu repositório
4. Configure as variáveis de ambiente:
   - Vá em **Variables**
   - Adicione todas as variáveis do `.env`
   - Para `GOOGLE_SERVICE_ACCOUNT_FILE`, você pode:
     - Fazer upload do arquivo JSON e referenciar o path, ou
     - Converter o conteúdo do JSON em string e criar uma variável `GOOGLE_SERVICE_ACCOUNT_JSON`

5. O Railway detectará automaticamente o `railway.json` e fará o deploy

### 3. Configurar Webhook

1. Copie a URL do seu app no Railway (ex: `https://seu-app.up.railway.app`)
2. Configure no Evolution API:
   - Webhook URL: `https://seu-app.up.railway.app/webhook/whatsapp`
   - Events: `messages.upsert`

## 📱 Como Usar

### Fluxo de Agendamento

**Paciente:**
```
Olá, quanto custa uma consulta?
```

**Bot:**
```
Olá! A consulta dermatológica custa R$ 350,00. Posso ajudar com mais alguma informação?
```

**Paciente:**
```
Quero agendar
```

**Bot:**
```
Ótimo! Para agendar, preciso de algumas informações. Qual é seu nome completo?
```

**Paciente:**
```
João Silva
```

**Bot:**
```
Obrigado, João! Agora preciso da sua data de nascimento (formato DD/MM/AAAA).
```

**Paciente:**
```
15/03/1990
```

**Bot:**
```
Perfeito! Que tipo de consulta você deseja agendar?
```

**Paciente:**
```
Dermatológica
```

**Bot:**
```
Ótimo! Consulta Dermatológica - R$ 350,00

Que dia seria melhor para você?
```

**Paciente:**
```
Quinta-feira
```

**Bot:**
```
Horários disponíveis:

1. 09:00
2. 10:30
3. 14:00

Por favor, escolha o número do horário desejado.
```

**Paciente:**
```
2
```

**Bot:**
```
Perfeito! Vou agendar sua Consulta Dermatológica para 25/10/2025 às 10:30.

Confirma o agendamento? (Sim/Não)
```

**Paciente:**
```
Sim
```

**Bot:**
```
✅ Consulta agendada com sucesso!

📅 Data: 25/10/2025 às 10:30
⏱️ Duração: 30 minutos
💰 Valor: R$ 350,00
📍 Endereço: Rua Exemplo, 123 - Centro

Lembramos que cancelamentos devem ser feitos com 24h de antecedência.
Até lá! 😊
```

### Cancelamento/Remarcação

**Paciente:**
```
Preciso remarcar minha consulta
```

**Bot:**
```
Para isso, preciso confirmar sua identidade. Qual é seu nome completo?
```

_(Bot valida identidade e mostra consultas agendadas)_

## 🔧 Manutenção

### Atualizar Informações da Clínica

1. Edite o arquivo `data/clinic_info.json`
2. Se o bot estiver rodando, chame o endpoint de reload:

```bash
curl -X POST https://seu-app.com/admin/reload-config
```

### Ver Logs

**Railway:**
- Acesse o painel do Railway
- Clique no seu projeto
- Vá em **Deployments** → **View Logs**

**Local:**
- Os logs aparecem no terminal onde você rodou `uvicorn`

### Backup do Banco de Dados

O banco SQLite fica em `data/appointments.db`. Faça backup regularmente:

```bash
# Copiar arquivo
cp data/appointments.db data/appointments.db.backup

# Ou comprimir
zip backup-$(date +%Y%m%d).zip data/appointments.db
```

## Readiness e recuperação das conversas

### Configuração da Fase 1

Forneça a configuração pelo ambiente operacional. A lista registra nomes e
semântica; credenciais e valores de produção não pertencem ao runbook.

| Variável | Semântica |
|---|---|
| `WASENDER_WEBHOOK_SECRET` | Segredo obrigatório, validado antes do payload do webhook. |
| `ADMIN_PASSWORD` | Autenticação administrativa do dashboard e das três rotas do simulador. |
| `DATABASE_URL`, `REDIS_URL` | Dependências SQL e Redis/broker; URLs não devem aparecer em logs. |
| `CONVERSATION_COORDINATION_EPOCH` | UUID externo compartilhado pelas instâncias e conferido contra a âncora Redis. |
| `CONVERSATION_REDIS_EXPECTED_RUN_ID` | Identidade esperada do processo Redis. |
| `CONVERSATION_REDIS_ATTEST_NOEVICTION`, `CONVERSATION_REDIS_ATTEST_PERSISTENCE` | Atestações operacionais obrigatórias, acompanhadas das verificações do servidor. |
| `CONTACT_LEASE_TTL_SECONDS`, `CONTACT_LEASE_HEARTBEAT_SECONDS` | Prazo da lease e intervalo de renovação; o heartbeat não pode exceder um terço do TTL. |
| `CLAIM_TTL_SECONDS` | Prazo de propriedade do processamento, inferior ao horizonte de processamento. |
| `DISPATCH_RETRY_SECONDS` | Horizonte do batch antes de staging. |
| `PROCESSING_RETRY_SECONDS` | Horizonte criado no primeiro claim; governa resultado e fronteira de commit após staging. |
| `ENQUEUE_VISIBILITY_SECONDS`, `ENQUEUE_BACKOFF_SECONDS` | Próxima tentativa após confirmação/ambiguidade ou falha definitiva do broker; ambos inferiores ao horizonte de despacho. |
| `REPLAY_WINDOW_SECONDS`, `TTL_MARGIN_SECONDS` | Retenção de deduplicação e margem; a janela geral nunca é menor que sete dias, e descarte durante pausa cobre também seu vencimento. |
| `BATCH_RECOVERY_INTERVAL_SECONDS` | Intervalo das passagens periódicas de recuperação. |
| `BATCH_RECOVERY_PAGE_SIZE`, `BATCH_RECOVERY_MAX_PAGES` | Limites de metadados visitados por passagem. |
| `APP_SKIP_DOTENV` | Impede carregamento de dotenv no ambiente de testes e nas rotinas que exigem configuração externa. |

### Testes locais descartáveis

Execute na raiz do checkout, com as dependências já disponíveis:

```powershell
python -m pytest tests/test_conversation_state.py -q
python -m pytest tests/test_conversation_flow.py -q
python -m pytest tests/test_conversation_security.py -q
python -m pytest tests/test_conversation_concurrency.py -q
python -m pytest -q
```

O harness fornece configuração sintética e SQLite estritamente em memória.
Após o bootstrap dos plugins do pytest e antes da coleta dos testes, ele bloqueia
rede, DNS, subprocessos, dotenv, leitura de `data/`, SQL persistente/ATTACH e
escritas dos testes no repositório. Cada caso verifica transações, proprietários,
heartbeats e chamadas fake em andamento antes de qualquer descarte. Resíduos de
lease/claim só são aceitos em cenários explícitos de interrupção, com token,
identidade e prazo exatos declarados pelo caso e conferidos antes da limpeza.
O teardown não avança o relógio nem expira owners: primeiro valida os resíduos,
depois elimina o estado privado dos fakes e seu conteúdo. A finalização do
próprio pytest pode atualizar seu cache.

Os IDs `state-01` a `state-48` e `flow-01` a `flow-77` identificam itens
executáveis da spec aprovada; eles podem ser selecionados com `pytest -k` ou
pelo node ID completo. Os testes usam barreiras e snapshots SQL sintéticos;
não comprovam isolamento ou entrega em serviços hospedados.

Verificação local da Task 11 após a revisão: estado **166**, fluxo **908**, segurança **274**,
concorrência **212** e suíte completa **1560 testes aprovados**. Os cinco comandos
acima terminaram com exit `0`. A suíte completa registrou 414 avisos do escape
preexistente em `app/main.py:2318`; nenhum serviço externo foi acessado.

### Sinais de disponibilidade e proprietários das falhas

`GET /health` é apenas liveness: responde 200 quando o processo HTTP está vivo,
sem sondar SQL, Redis ou broker. `GET /ready` responde 200 somente quando todas
as dependências obrigatórias estão disponíveis; caso contrário, responde 503.
O corpo contém somente `status` (`ready`/`not_ready`) e `dependencies`, com as
chaves `secret`, `sql`, `redis`, `epoch` e `broker`, cada uma com
`ready`/`not_ready`. O segredo é verificado por presença; nenhum valor de
configuração ou detalhe de exceção é publicado.

A coordenação exige `CONVERSATION_COORDINATION_EPOCH` válido e já existente no
Redis, `CONVERSATION_REDIS_EXPECTED_RUN_ID` igual ao servidor e as atestações
`CONVERSATION_REDIS_ATTEST_NOEVICTION=true` e
`CONVERSATION_REDIS_ATTEST_PERSISTENCE=true`. O servidor também precisa reportar
`noeviction`, AOF habilitado, última escrita AOF `ok` e `loading=0`.
Readiness nunca cria ou corrige o epoch. Os clientes de sondagem usam limites
de conexão e socket de 2 segundos; SQL PostgreSQL também limita cada statement
e espera por lock a 2 segundos. Falhas mantêm os fluxos da conversa fechados.
O cliente Redis de coordenação é construído pelo mesmo helper no runtime e na
CLI. Na query da URL aceita-se somente um `db` numérico; opções de timeout,
retry ou outras opções são rejeitadas antes da construção, incluindo variantes
duplicadas, codificadas ou com caixa diferente. A configuração efetiva do pool
é conferida depois da construção: timeout de até 2 segundos e zero retries.
O startup preserva a inicialização SQL e o cleanup de 20 minutos somente depois
de readiness passar. Se o processo iniciou indisponível, reinicie-o após corrigir
as dependências para iniciar também esse cleanup.

HTTP, workers e cleanup usam o mesmo contrato `ConversationRuntime`. O Celery
beat registra `app.main.recover_conversations_task`, no intervalo
`BATCH_RECOVERY_INTERVAL_SECONDS`. Execute uma única instância de beat na
topologia operacional existente. Cada passagem lê páginas de metadados e
retorna somente contagens: `scanned`, `rescheduled`, `completed`, `aborted`,
`quarantined`, `exhausted`, `skipped` e `failed`. Os limites são
`BATCH_RECOVERY_PAGE_SIZE` (padrão 100; máximo 1000) e
`BATCH_RECOVERY_MAX_PAGES` (padrão 2; máximo 100). A continuação fica em um
checkpoint Redis compartilhado, protegido por CAS e pelo epoch; reinícios ou
workers alternados retomam esse progresso. Índices e cursores não contêm
telefone, mensagem ou credenciais. Entradas danificadas são contadas e não
impedem a visita de outras entradas; metadados incertos não são apagados para
simular uma recuperação bem-sucedida.

A recuperação readquire a lease, relê SQL e Redis e pode republicar apenas
comandos internos canônicos. Nunca chama Claude ou o transporte do provedor.
Páginas antigas são apenas candidatos: tentativas criadas depois da leitura
da página são reavaliadas com o relógio atual sob lease e preservadas enquanto
saudáveis. Readiness é rechecado junto às fronteiras seguintes de DML, agente,
broker, transporte e checkpoint, inclusive depois de operações demoradas.
Essas observações não são uma transação atômica com as dependências externas.
Resultados, commits e enqueues já reconhecidos mantêm seu registro canônico
mesmo se readiness fechar; o próximo efeito externo é bloqueado.
Uma confirmação local de enqueue já persistida permite concluir o lote sem
reenviar; `DONE` não é reproduzido. Sem confirmação, resultados compatíveis já
confirmados no SQL ou com reserva válida permanecem recuperáveis. Preparações
provadamente anteriores ao commit podem ser abortadas após o prazo; um
`COMMITTING` incerto entra em quarentena. O conteúdo SQL atual não comprova
qual operação foi confirmada. Resolver essa ambiguidade exige evidência externa
e o procedimento operacional de resolução em quiescência; não há replay de DML.
O esgotamento de trabalho não confirmado respeita os horizontes configurados
de dispatch/processamento e as cercas existentes.

O contato sintético do simulador permanece em captura local durante recovery.
Essa captura não entrega uma resposta ao navegador nem ao provedor. Tentativas
interrompidas do simulador podem exigir o reset autenticado de seu estado.
Confirmação local de enqueue não comprova entrega e não oferece exactly-once.
Redis/Lua/ACL, broker, SQL hospedado, serviços externos e os procedimentos reais
de recovery/rotação continuam **NÃO VERIFICADOS** por testes locais simulados.

| Fronteira | Quem retoma ou resolve |
|---|---|
| Webhook | Retorna `503` em indisponibilidade; a nova entrega pertence ao fornecedor. Assinatura inválida permanece `401`. |
| Dashboard e simulador | Retornam `503` sem efeito parcial; nova tentativa pertence ao operador/chamador. Duração inválida retorna `400`. |
| Processamento e sender | A task converte a falha tipada em retry, preservando o comando interno e seus IDs. |
| Broker de processamento | Replay e recuperador respeitam a reserva e `next_enqueue_at`, sem novo append da mensagem. |
| Scheduler e recuperador | A próxima passagem reavalia o estado; readiness fechada não autoriza efeitos. |
| `COMMITTING` incerto / `QUARANTINED` | Operador autorizado, com quiescência e prova externa; ausência momentânea de uma linha SQL não autoriza repetir ou abortar o commit. |

Os eventos usam classes fixas como `dependency_unavailable`, `coordination_failed`,
`persistence_failed` e `transport_failed`, sem detalhes sensíveis da exceção.
O resumo final do scheduler conta candidatos examinados e ainda pode registrar
`recovered/completed` mesmo quando houve falhas por contato; consulte também os
eventos individuais. Esse limite conhecido não é uma prova de limpeza concluída.

Entrega exatamente uma vez sem outbox permanece **NÃO VERIFICADA**. Persistência
e failover de Redis, ambiguidade do broker, Wasender e Claude reais permanecem
**NÃO VERIFICADOS**. Quiescência e rotação de epoch em produção permanecem
**NÃO VERIFICADAS** até execução por operador autorizado. A Fase 1 não executa
migração nem alteração de estado em produção.

### Rotação do epoch com quiescência obrigatória

1. Pare ingresso HTTP, workers, scheduler/beat e sender em todas as instâncias.
2. Aguarde as leases ativas e as tentativas drenarem. Resolva qualquer tentativa
   incerta com evidência operacional; não use a rotação para ignorar pendências.
3. Registre com segurança o UUID atual e um UUID novo e distinto. Atualize a
   configuração externa `CONVERSATION_COORDINATION_EPOCH` para o novo UUID,
   mantendo todos os processos parados e a atestação do Redis válida.
4. No ambiente operacional autorizado, execute **uma única vez**:

   ```bash
   python scripts/rotate_conversation_epoch.py --expected-current-epoch UUID_ATUAL --new-epoch UUID_NOVO --confirm-quiescent
   ```

   Substitua os marcadores pelos UUIDs operacionais aprovados. O comando consome a configuração externa,
   não carrega `.env`, exige que o novo UUID seja o configurado e verifica
   SQL/Redis/broker e atestação antes do CAS atômico do anchor antigo.
5. Reinicie os processos e confirme `GET /ready` com 200 em todas as instâncias.

O comando imprime somente `epoch_rotation_succeeded` (exit 0),
`epoch_rotation_rejected` (exit 2: argumentos/configuração/confirmação) ou
`epoch_rotation_failed` (exit 3: dependência/CAS). Não imprime UUIDs, URLs,
payloads Redis ou exceções. Uma repetição com o UUID antigo falha por CAS.
Uma falha de conexão após o CAS pode deixar o resultado incerto; mantenha a
quiescência e confira o anchor por um canal operacional protegido antes de
decidir o próximo passo. A rotação invalida owners e comandos do epoch anterior;
não publica nem apaga dados de pacientes e não substitui a drenagem.

## 🐛 Troubleshooting

### Bot não responde mensagens

1. Verifique se o webhook está configurado corretamente no Evolution API
2. Verifique os logs para erros
3. Teste o endpoint: `GET https://seu-app.com/health`
4. Verifique se a instância do Evolution API está conectada

### Erro ao acessar Google Calendar

1. Verifique se o arquivo `google-credentials.json` está presente
2. Verifique se o calendário foi compartilhado com a Service Account
3. Verifique se a Google Calendar API está habilitada no projeto

### Erro "ANTHROPIC_API_KEY not found"

1. Certifique-se de que o arquivo `.env` existe
2. Verifique se a chave está correta
3. Reinicie o servidor após adicionar a chave

### WhatsApp desconecta constantemente

- Evolution API pode desconectar se usar o WhatsApp em outro dispositivo
- Escaneie o QR code novamente
- Use um número dedicado para o bot

## 📊 Endpoints da API

- `GET /` - Página inicial com informações do bot
- `GET /health` - Health check
- `GET /status` - Status detalhado (WhatsApp, Calendar, DB)
- `POST /webhook/whatsapp` - Webhook para receber mensagens
- `POST /admin/reload-config` - Recarregar configurações da clínica

## 🔒 Segurança

- **Validação de identidade**: Nome + data de nascimento para modificar consultas
- **Sem orientação médica**: Bot recusa dar diagnósticos ou orientações médicas
- **Rate limiting**: Evita spam (implementar se necessário)
- **Auditoria conversacional**: eventos, transições e resultados operacionais sanitizados; conteúdo e identificadores de pacientes não entram nos logs da Fase 1.
- **LGPD**: Apenas dados essenciais são coletados (nome, telefone, data nascimento)

## 🤝 Contribuindo

Sinta-se à vontade para abrir issues ou pull requests com melhorias!

## 📝 Licença

Este projeto é de uso privado para a clínica.

## 👨‍💻 Desenvolvedor

Desenvolvido por Daniel Nobrega Medeiros - Nobrega Medtech

---

**Precisa de ajuda?** Entre em contato com o desenvolvedor.
