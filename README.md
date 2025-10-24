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
- **Banco de Dados**: SQLite com SQLAlchemy
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
│   └── appointments.db      # Banco SQLite (gerado automaticamente)
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

# Database
DATABASE_URL=sqlite:///./data/appointments.db

# Configurações gerais
ENVIRONMENT=development
LOG_LEVEL=INFO
```

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
- **Logs completos**: Todas interações são logadas para auditoria
- **LGPD**: Apenas dados essenciais são coletados (nome, telefone, data nascimento)

## 🤝 Contribuindo

Sinta-se à vontade para abrir issues ou pull requests com melhorias!

## 📝 Licença

Este projeto é de uso privado para a clínica.

## 👨‍💻 Desenvolvedor

Desenvolvido por Daniel Nobrega Medeiros - Nobrega Medtech

---

**Precisa de ajuda?** Entre em contato com o desenvolvedor.

