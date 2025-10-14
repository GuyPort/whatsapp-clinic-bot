# 📑 Índice de Navegação - WhatsApp Clinic Bot

Guia rápido para encontrar o que você precisa!

---

## 🚀 Começando

**Nunca usou o projeto?** → [CHECKLIST_CLINICA.md](CHECKLIST_CLINICA.md)  
**Quer fazer setup?** → [SETUP_GUIDE.md](SETUP_GUIDE.md)  
**Documentação geral?** → [README.md](README.md)  

---

## 📚 Documentação

| Documento | Descrição | Quando Usar |
|-----------|-----------|-------------|
| [README.md](README.md) | Visão geral completa | Entender o projeto |
| [SETUP_GUIDE.md](SETUP_GUIDE.md) | Setup passo a passo | Configurar pela primeira vez |
| [CHECKLIST_CLINICA.md](CHECKLIST_CLINICA.md) | Checklist visual | Garantir que fez tudo certo |
| [FAQ.md](FAQ.md) | Perguntas frequentes | Tirar dúvidas comuns |
| [EXAMPLES.md](EXAMPLES.md) | Exemplos práticos | Ver como usar na prática |
| [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) | Resumo técnico | Entender arquitetura |
| [CHANGELOG.md](CHANGELOG.md) | Histórico de mudanças | Ver o que mudou |
| [DOCKER_DEPLOY.md](DOCKER_DEPLOY.md) | Deploy com Docker | Usar containers |
| [INDEX.md](INDEX.md) | Este arquivo | Navegar na documentação |

---

## 🔧 Scripts e Ferramentas

| Script | Comando | Descrição |
|--------|---------|-----------|
| **Rodar bot** | `python run.py` | Inicia o servidor localmente |
| **Quick start** | `quickstart.bat` | Setup automático (Windows) |
| **Testar bot** | `python test_bot.py` | Simula conversas sem WhatsApp |
| **Gerenciar DB** | `python manage_db.py` | Backups, estatísticas, export |
| **Monitorar** | `python monitor.py` | Verifica status de tudo |

---

## 📁 Estrutura de Arquivos

```
📦 whatsapp-clinic-bot/
├── 📂 app/                      ← Código principal
│   ├── main.py                  ← FastAPI + webhooks
│   ├── ai_agent.py              ← Agente IA (Claude)
│   ├── whatsapp_service.py      ← Cliente WhatsApp
│   ├── calendar_service.py      ← Google Calendar
│   ├── appointment_rules.py     ← Regras de agendamento
│   ├── models.py                ← Modelos do banco
│   ├── database.py              ← Setup SQLite
│   ├── config.py                ← Configurações
│   └── utils.py                 ← Funções auxiliares
│
├── 📂 data/                     ← Dados
│   ├── clinic_info.json         ← Info da clínica (EDITAR!)
│   └── appointments.db          ← Banco de dados (gerado)
│
├── 📄 .env                      ← Credenciais (CRIAR!)
├── 📄 env.example               ← Exemplo de .env
├── 📄 requirements.txt          ← Dependências Python
├── 📄 railway.json              ← Config Railway
├── 📄 Dockerfile                ← Imagem Docker
├── 📄 docker-compose.yml        ← Docker Compose
│
├── 🛠️ run.py                    ← Rodar servidor
├── 🛠️ test_bot.py               ← Testes
├── 🛠️ manage_db.py              ← Gerenciar banco
├── 🛠️ monitor.py                ← Monitoramento
├── 🛠️ quickstart.bat            ← Setup rápido
│
└── 📚 (Documentação)            ← Todos os .md
```

---

## 🎯 Por Onde Começar?

### Sou da Clínica (Não-Técnico)
1. ✅ [CHECKLIST_CLINICA.md](CHECKLIST_CLINICA.md) - Passo a passo visual
2. ✅ [SETUP_GUIDE.md](SETUP_GUIDE.md) - Guia detalhado
3. ✅ [FAQ.md](FAQ.md) - Dúvidas comuns

### Sou Desenvolvedor
1. ✅ [README.md](README.md) - Visão geral técnica
2. ✅ [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) - Arquitetura
3. ✅ [EXAMPLES.md](EXAMPLES.md) - Código de exemplo

### Quero Fazer Deploy
1. ✅ **Railway**: [README.md](README.md#-deploy-railway)
2. ✅ **Docker**: [DOCKER_DEPLOY.md](DOCKER_DEPLOY.md)
3. ✅ **VPS**: [DOCKER_DEPLOY.md](DOCKER_DEPLOY.md#-deploy-em-servidor-vps)

### Preciso de Ajuda
1. ❓ [FAQ.md](FAQ.md) - Perguntas frequentes
2. ❓ [README.md](README.md#-troubleshooting) - Troubleshooting
3. ❓ Entre em contato com suporte

---

## 🔍 Busca Rápida

### Configuração
- **API Keys**: [SETUP_GUIDE.md](SETUP_GUIDE.md#2️⃣-configurar-anthropic-api-2-min)
- **WhatsApp**: [SETUP_GUIDE.md](SETUP_GUIDE.md#3️⃣-configurar-evolution-api-5-min)
- **Google Calendar**: [SETUP_GUIDE.md](SETUP_GUIDE.md#4️⃣-configurar-google-calendar-10-min)
- **Editar info clínica**: [CHECKLIST_CLINICA.md](CHECKLIST_CLINICA.md#24-editar-informações-da-clínica)

### Problemas
- **Bot não responde**: [FAQ.md](FAQ.md#bot-não-está-respondendo-no-whatsapp)
- **Erro Calendar**: [FAQ.md](FAQ.md#erro-google-calendar-permission-denied)
- **Erro API**: [FAQ.md](FAQ.md#erro-anthropic_api_key-not-found)
- **WhatsApp desconecta**: [FAQ.md](FAQ.md#whatsapp-desconecta-constantemente)

### Uso
- **Exemplos de conversa**: [EXAMPLES.md](EXAMPLES.md#-exemplos-de-conversas)
- **Comandos API**: [EXAMPLES.md](EXAMPLES.md#-exemplos-de-api)
- **Scripts Python**: [EXAMPLES.md](EXAMPLES.md#-exemplos-de-scripts-python)

### Deploy
- **Railway**: [README.md](README.md#-deploy-railway)
- **Docker local**: [DOCKER_DEPLOY.md](DOCKER_DEPLOY.md#-deploy-local-com-docker)
- **Docker VPS**: [DOCKER_DEPLOY.md](DOCKER_DEPLOY.md#-deploy-em-servidor-vps)

### Manutenção
- **Backup**: [FAQ.md](FAQ.md#como-fazer-backup)
- **Ver consultas**: [FAQ.md](FAQ.md#como-ver-as-consultas-agendadas)
- **Atualizar info**: [FAQ.md](FAQ.md#como-adicionar-mais-tipos-de-consulta)
- **Monitorar**: `python monitor.py`

---

## 📊 Fluxogramas

### Fluxo de Agendamento
```
Paciente pergunta
    ↓
Bot responde (Claude)
    ↓
Detecta intenção de agendar
    ↓
Pede nome + data nascimento
    ↓
Salva no banco (Patient)
    ↓
Pede tipo de consulta
    ↓
Pede dia preferido
    ↓
Busca horários (DB + Calendar)
    ↓
Mostra 3 opções
    ↓
Paciente escolhe
    ↓
Confirma
    ↓
Cria no banco + Calendar
    ↓
Envia confirmação
```

### Fluxo de Webhook
```
WhatsApp → Evolution API → Webhook
    ↓
FastAPI recebe
    ↓
Valida (não é do bot)
    ↓
Extrai telefone + mensagem
    ↓
Background task
    ↓
Processa com IA
    ↓
Gera resposta
    ↓
Envia via Evolution
    ↓
Marca como lida
```

---

## 🆘 Ajuda Rápida

| Problema | Solução Rápida |
|----------|----------------|
| 🚫 Bot não responde | Verificar webhook + logs Railway |
| ❌ Erro API | Verificar .env e variáveis Railway |
| 📅 Calendar não funciona | Verificar compartilhamento + credentials |
| 📱 WhatsApp desconecta | Escanear QR code novamente |
| 🗄️ Erro de banco | Fazer backup e resetar |
| 💰 Custos altos | Verificar uso da Claude API |

---

## 📱 Contatos

- **Email Suporte**: (definir)
- **Desenvolvedor**: Daniel Nobrega Medeiros
- **Empresa**: Nobrega Medtech

---

## 🎓 Recursos Adicionais

- [Documentação FastAPI](https://fastapi.tiangolo.com/)
- [Anthropic Claude](https://docs.anthropic.com/)
- [Evolution API](https://github.com/EvolutionAPI/evolution-api)
- [Google Calendar API](https://developers.google.com/calendar)
- [Railway Docs](https://docs.railway.app/)

---

**💡 Dica**: Adicione este arquivo aos favoritos para acesso rápido!

