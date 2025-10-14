# 🚀 Guia de Setup Rápido

Este guia vai te ajudar a colocar o bot no ar em menos de 30 minutos!

## ✅ Checklist de Pré-requisitos

- [ ] Python 3.11+ instalado
- [ ] Conta na Anthropic (Claude API)
- [ ] Evolution API configurada
- [ ] Conta no Google Cloud
- [ ] Conta no Railway (para deploy)

---

## 📋 Passo a Passo

### 1️⃣ Clonar e Instalar (5 min)

```bash
# Clonar repositório
git clone <seu-repo>
cd whatsapp-clinic-bot

# Criar ambiente virtual
python -m venv venv

# Ativar (Windows)
venv\Scripts\activate

# Ativar (Linux/Mac)
source venv/bin/activate

# Instalar dependências
pip install -r requirements.txt
```

### 2️⃣ Configurar Anthropic API (2 min)

1. Acesse: https://console.anthropic.com/
2. Crie uma API Key
3. Copie a chave (começa com `sk-ant-...`)

### 3️⃣ Configurar Evolution API (5 min)

**Opção A: Usar serviço hospedado**
- Contrate um serviço de Evolution API (vários disponíveis)
- Anote a URL e API Key

**Opção B: Self-hosted**
```bash
# Clone Evolution API
git clone https://github.com/EvolutionAPI/evolution-api.git
cd evolution-api

# Configure e rode (seguir docs do projeto)
docker-compose up -d
```

1. Acesse o painel da Evolution API
2. Crie uma instância chamada `clinica-bot`
3. Escaneie o QR code com o WhatsApp da clínica
4. Anote a URL e API Key

### 4️⃣ Configurar Google Calendar (10 min)

1. Acesse: https://console.cloud.google.com/

2. **Criar Projeto**
   - New Project → Nome: "Clinic Bot" → Create

3. **Habilitar Calendar API**
   - APIs & Services → Library
   - Busque "Google Calendar API"
   - Clique em Enable

4. **Criar Service Account**
   - IAM & Admin → Service Accounts
   - Create Service Account
   - Nome: "clinic-bot"
   - Grant this service account access to project (pode pular)
   - Done

5. **Gerar Chave JSON**
   - Clique na Service Account criada
   - Keys → Add Key → Create New Key
   - Tipo: JSON
   - Create
   - Baixe o arquivo JSON

6. **Renomear arquivo**
   - Renomeie o arquivo baixado para `google-credentials.json`
   - Mova para a raiz do projeto

7. **Compartilhar Calendário**
   - Abra Google Calendar
   - Crie um calendário novo ou use existente
   - Settings → Share with specific people
   - Adicione o email da Service Account (está no JSON)
   - Permissão: "Make changes to events"
   - Copie o Calendar ID (em Settings → Integrate Calendar)

### 5️⃣ Criar arquivo .env (3 min)

Crie um arquivo `.env` na raiz do projeto:

```env
ANTHROPIC_API_KEY=sk-ant-COLE-SUA-CHAVE-AQUI
EVOLUTION_API_URL=https://sua-evolution-api.com
EVOLUTION_API_KEY=sua-api-key-aqui
EVOLUTION_INSTANCE_NAME=clinica-bot
GOOGLE_CALENDAR_ID=seu-calendar-id@group.calendar.google.com
GOOGLE_SERVICE_ACCOUNT_FILE=google-credentials.json
DATABASE_URL=sqlite:///./data/appointments.db
ENVIRONMENT=development
LOG_LEVEL=INFO
```

### 6️⃣ Editar Informações da Clínica (5 min)

Edite `data/clinic_info.json` com os dados reais da clínica:

```json
{
  "nome_clinica": "Nome Real da Clínica",
  "medica_responsavel": "Dra. Nome Real",
  "especialidade": "Especialidade Real",
  "endereco": "Endereço Real",
  "telefone_contato": "(XX) XXXXX-XXXX",
  ...
}
```

### 7️⃣ Testar Localmente (2 min)

```bash
# Rodar servidor
uvicorn app.main:app --reload

# Em outro terminal, testar
curl http://localhost:8000/health
```

Se retornar `{"status":"healthy"}`, está funcionando! ✅

### 8️⃣ Deploy no Railway (5 min)

1. Acesse: https://railway.app/
2. Login com GitHub
3. New Project → Deploy from GitHub repo
4. Conecte seu repositório
5. Configure as variáveis de ambiente (copie do .env)
6. Deploy!
7. Copie a URL do app (ex: `https://seu-app.up.railway.app`)

### 9️⃣ Configurar Webhook (2 min)

1. Acesse o painel da Evolution API
2. Vá em Settings → Webhooks
3. Configure:
   - URL: `https://seu-app.up.railway.app/webhook/whatsapp`
   - Events: Marque `messages.upsert`
   - Save

### 🎉 Testar o Bot!

Envie uma mensagem para o WhatsApp conectado:

```
Olá!
```

O bot deve responder! 🎉

---

## 🔍 Verificações

### ✅ Bot está respondendo?

**SIM** → Parabéns! 🎉

**NÃO** → Verifique:

1. **Webhook configurado corretamente?**
   ```bash
   curl https://seu-app.up.railway.app/health
   ```
   Deve retornar status healthy

2. **Evolution API conectada?**
   - Verifique no painel se a instância está "connected"

3. **Logs no Railway**
   - Vá em Deployments → View Logs
   - Veja se há erros

4. **Variáveis de ambiente**
   - Verifique se todas foram configuradas corretamente no Railway

---

## 🆘 Problemas Comuns

### "Module not found" error
```bash
pip install -r requirements.txt
```

### "Database error"
```bash
# Deletar banco e recriar
rm data/appointments.db
# Reiniciar servidor
```

### "Google Calendar permission denied"
- Verifique se compartilhou o calendário com a Service Account
- Email deve ser exatamente o que está no JSON

### "Evolution API timeout"
- Verifique se a URL está correta
- Verifique se a API Key está correta
- Verifique se a instância existe

---

## 📞 Suporte

Se algo não funcionou, verifique:
1. README.md → Seção Troubleshooting
2. Logs da aplicação
3. Status da Evolution API

---

Pronto! Seu bot está no ar! 🚀

