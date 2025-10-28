# Análise Detalhada do Bot WhatsApp Clínica

## Visão Geral do Sistema

Sistema de agendamento automatizado de consultas médicas via WhatsApp, utilizando Claude AI como assistente virtual (persona "Beatriz") para interação com pacientes.

---

## 1. ARQUITETURA DO SISTEMA

### 1.1 Stack Tecnológico

```
Frontend: WhatsApp (Evolution API)
    ↓
Backend: FastAPI (Python)
    ↓
IA: Claude 3.5 Sonnet (Anthropic)
    ↓
Banco de Dados: SQLite (dev) / PostgreSQL (prod)
```

### 1.2 Componentes Principais

**Camada de Apresentação (WhatsApp)**
- Recebe mensagens via webhook da Evolution API
- Processa mensagens de texto
- Ignora mensagens de grupos e newsletters

**Camada de Aplicação (FastAPI)**
- Gerencia webhooks e endpoints administrativos
- Dashboard HTML para visualização de agendamentos
- Endpoints de API para consultas e estatísticas

**Camada de Lógica de Negócio (Claude IA)**
- Processa mensagens usando ferramentas (tools)
- Gerencia fluxos de conversa
- Mantém contexto de conversação
- Valida dados de agendamento

**Camada de Dados**
- SQLAlchemy ORM
- Modelos: Appointment, ConversationContext, PausedContact
- Regras de agendamento e validações

---

## 2. MODELOS DE DADOS

### 2.1 Appointment (Consultas)

```python
Tabela: appointments
Campos Principais:
- id: Integer (PK)
- patient_name: String(200) - Nome completo
- patient_phone: String(20) - Telefone normalizado (5511...)
- patient_birth_date: String(10) - DD/MM/YYYY
- appointment_date: String(10) - YYYYMMDD
- appointment_time: String(5) - HH:MM
- duration_minutes: Integer - Duração (60 min)
- consultation_type: String(50) - clinica_geral, geriatria, domiciliar
- insurance_plan: String(50) - CABERGS, IPE, particular
- status: Enum - AGENDADA, CANCELADA, REALIZADA
- notes: Text - Observações
```

**Pontos Críticos:**
- `appointment_date` usa formato string para evitar problemas de timezone
- Comparações de datas exigem conversões específicas
- Status rastreia ciclo de vida da consulta

### 2.2 ConversationContext (Contexto de Conversa)

```python
Tabela: conversation_contexts
Campos:
- phone: String(20) (PK)
- messages: JSON - Histórico completo de mensagens
- current_flow: String(50) - Estado atual (booking, cancelamento, etc)
- flow_data: JSON - Dados temporários coletados
- status: String(20) - active, expired
```

**flow_data Structure:**
```json
{
  "patient_name": "João Silva",
  "patient_birth_date": "15/03/1990",
  "consultation_type": "clinica_geral",
  "insurance_plan": "particular",
  "appointment_date": "25/11/2025",
  "appointment_time": "14:00",
  "pending_confirmation": true
}
```

**Persistência de Contexto:**
- Mensagens armazenadas como array JSON
- Cada mensagem tem: role, content, timestamp
- Contexto preservado entre mensagens do mesmo telefone
- Timeout proativo via scheduler (1 hora sem atividade)

### 2.3 PausedContact (Pausas para Atendimento Humano)

```python
Tabela: paused_contacts
Campos:
- phone: String(20) (PK)
- paused_until: DateTime - Quando a pausa expira
- reason: String(100) - Motivo da pausa
```

**Comportamento:**
- Pausa de 2 horas quando usuário solicita atendimento humano
- Bot ignora mensagens durante período de pausa
- Reativação automática após expiração

---

## Pty3. FLUXO DE PROCESSAMENTO DE MENSAGENS

### 3.1 Pipeline de Mensagens

```
1. WhatsApp → Webhook (/webhook/whatsapp)
   ├─ Extrai phone, message_text, message_id
   ├─ Filtra mensagens enviadas por bot
   ├─ Filtra grupos e newsletters
   └─ Chama process_message_task() em background

2. process_message_task()
   ├─ Normaliza telefone (5511...)
   ├─ Marca mensagem como lida
   ├─ Verifica se bot está pausado
   ├─ Chama ai_agent.process_message()
   └─ Envia resposta via WhatsApp

3. ai_agent.process_message()
   ├─ Carrega contexto do banco (ou cria novo)
   ├─ Verifica encerramento por resposta negativa
   ├─ Detecta confirmação pendente
   ├─ Adiciona mensagem ao histórico
   ├─ Envia para Claude com histórico completo
   ├─ Processa tools em loop (máx 5 iterações)
   ├─ Salva resposta no histórico
   ├─ Atualiza flow_data incrementalmente
   └─ Executa fallbacks quando necessário
```

### 3.2 Loop de Processamento de Tools

```python
Iteração 1: Claude retorna tool_use
  ↓
Executa tool → tool_result
  ↓
Iteração 2: Envia tool_result para Claude
  ↓
Claude pode:
  - Retornar tool_use novamente → Continua loop
  - Retornar text → Fim do loop
  ↓
Máximo 5 iterações (proteção contra loop infinito)
```

### 3.3 Fallback Mechanisms

**Fallback 1: confirm_time_slot**
- Detecta: temos appointment_date + appointment_time mas sem pending_confirmation
- Ação: Executa confirm_time_slot manualmente
- Motivo: Claude às vezes não chama a tool

**Fallback 2: Extração de dados**
- Se flow_data vazio → extrai do histórico de mensagens
- Regex patterns para nome e data de nascimento
- Validação de campos obrigatórios

---

## 4. FLUXO DE AGENDAMENTO DETALHADO

### 4.1 Etapas Sequenciais

```
Etapa 1: Menu Inicial
- Bot apresenta 3 opções sempre que usuário envia mensagem inicial
- Claude escolhe ação baseado em opção selecionada

Etapa 2: Coleta Nome + Data de Nascimento
- Bot solicita ambos de uma vez
- Aceita formatos:
  * "João Silva, 15/03/1990"
  * Nome primeiro, depois data
  * Linguagem natural
- Extração via _extract_name_and_birth_date()
  * Regex para datas (DD/MM/YYYY, DD/MM/AA, texto)
  * Regex para nomes (mínimo 2 palavras)
  * Validação de idade (max 120 anos)

Etapa 3: Tipo de Consulta
- Menu de 3 opções:
  1. Clínica Geral - R$ 300
  2. Geriatria Clínica e Preventiva - R$ 300
  3. Atendimento Domiciliar ao Paciente Idoso - R$ 500
- Salva no flow_data: consultation_type

Etapa 4: Convênio
- Pergunta sobre convênio
- Respostas possíveis:
  * NEGATIVA: "não", "particular" → insurance_plan = "particular"
  * POSITIVA ESPECÍFICA: "CABERGS", "IPE" → insurance_plan = nome
  * POSITIVA GENÉRICA: "sim" → pede especificação
  * AMBÍGUA → clarifica

Etapa 5: Data Desejada
- Solicita data no formato DD/MM/AAAA
- Claude chama tool: validate_date_and_show_slots()

Etapa 6: Tool validate_date_and_show_slots
- Validações:
  ✓ Dia da semana (domingo fechado)
  ✓ Dias especiais fechados (dias_fechados)
  ✓ Horário de funcionamento
- Busca consultas já agendadas
- Gera slots disponíveis (horários inteiros)
- Retorna mensagem completa com lista

Etapa 7: Escolha de Horário
- Usuário envia HH:MM (ex: "14:00")
- Claude detecta formato e chama confirm_time_slot()

Etapa 8: Tool confirm_time_slot
- Verifica se é horário inteiro (00:00, 01:00, etc)
- Valida disponibilidade final (verifica conflitos)
- Mostra resumo completo da consulta
- Define pending_confirmation = true

Etapa 9: Confirmação
- Usuário responde "sim", "confirma", etc
- Bot detecta intenção positiva
- Chama create_appointment()
- Limpa pending_confirmation

Etapa 10: Tool create_appointment
- Valida todos os dados
- Verifica disponibilidade final (dupla validação)
- Salva no banco com formato string (YYYYMMDD)
- Limpa flow_data (appointment_date, appointment_time, pending_confirmation)
- Retorna mensagem de sucesso

Etapa 11: Ciclo Continuo
- Bot pergunta: "Posso te ajudar com mais alguma coisa?"
- Se SIM → mantém contexto e processa nova solicitação
- Se NÃO → executa end_conversation e encerra
```

### 4.2 Validações Críticas

**Validação de Data de Nascimento:**
- Apenas Python valida a data (Claude apenas extrai)
- Se `erro_data` existe → rejeitar
- Se `erro_data` é null → aceitar
- Regra: Aceitar QUALQUER data válida (incluindo bebês)

**Validação de Horários:**
- Apenas horários inteiros (08:00, 09:00, etc)
- Slots gerados a cada 1 hora
- Verificação de conflitos com consultas existentes
- Duração fixa: 60 minutos

**Validação de Disponibilidade (Dupla):**
- 1ª verificação: ao listar slots disponíveis
- 2ª verificação: ao criar agendamento (previne race conditions)

---

## 5. EXTRAÇÃO DE DADOS

### 5.1 Padrões Regex

**Data de Nascimento:**
```python
# Padrão 1: DD/MM/YYYY ou DD/MM/AA
r'\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b'

# Padrão 2: Nome do mês
r'\b(\d{1,2})\s+de\s+(janeiro|fevereiro|...)\s+de\s+(\d{4})\b'
```

**Nome:**
```python
# Remove palavras comuns e data
# Verifica mínimo 2 palavras
# Aceita: letras, espaços, hífens, acentos
r"^[a-zA-ZÀ-ÿ\s\-']+$"
```

### 5.2 Classificação de Intenções

**Confirmação (positive):**
```python
keywords = ["sim", "ok", "confirmo", "quero", "pode", "confirma", "está ótimo"]
```

**Negativa (negative):**
```python
keywords = ["não", "nao", "n", "quero mudar", "tem como", "seria possível"]
```

**Encerramento:**
```python
triggers = ["só isso", "pode encerrarful", "não preciso de mais nada", "obrigado tchau"]
```

---

## 6. REGRAS DE AGENDAMENTO

### 6.1 AppointmentRules Class

**Métodos Principais:**

1. `is_valid_appointment_date()`
   - Datahora não pode ser no passado
   - Domingo sempre fechado
   - Verifica horário de funcionamento
   - Sábado: última consulta às 11:30

2. `get_available_slots()`
   - Gera slots a cada 5 minutos
   - Busca consultas existentes no banco
   - Verifica conflitos de horário
   - Retorna lista de datetime disponíveis

3. `check_slot_availability()`
   - Verifica um horário específico
   - Converte appointments do banco para datetime
   - Verifica sobreposição de horários
   - Suporte a múltiplos formatos de data

**Horários de Funcionamento:**
```
Segunda-Sexta: 08:00-18:00
Sábado: 08:00-12:00
Domingo: FECHADO
```

**Dias Fechados Especiais:**
- Lista em clinic_info.json → dias_fechados
- Exemplo: "14/11/2025" a "14/12/2025" (férias)

### 6.2 Conversão de Formatos de Data

**Problema Crítico:** Múltiplos formatos em uso

```
appointment_date (banco): "20251022" (YYYYMMDD string)
appointment_time (banco): "14:00" (HH:MM string)
patient_birth_date (banco): "15/03/1990" (DD/MM/YYYY string)

Conversões necessárias:
- YYYYMMDD → datetime para validação
- YYYYMMDD → DD/MM/YYYY para exibição
- DD/MM/YYYY → datetime para parse
- datetime → YYYYMMDD para salvar
```

---

## 7. INTEGRAÇÕES EXTERNAS

### 7.1 Evolution API (Wasender)

**Endpoint:** `https://wasenderapi.com/api/send-message`

**Autenticação:**
```python
headers = {
    "Authorization": f"Bearer {WASENDER_API_KEY}",
    "Content-Type": "application/json"
}
```

**Envio de Mensagem:**
```python
payload = {
    "to": "5511999999999@s.whatsapp.net",
    "text": "Mensagem",
    "delay": 1200  # 1.2s delay humano
}
```

**Status da Instância:**
- GET `/api/status`
- Verifica se WhatsApp está conectado

### 7.2 Anthropic Claude API

**Model:** `claude-sonnet-4-20250514`

**Parâmetros:**
```python
max_tokens = 2000
temperature = 0.1  # Baixa criatividade, alta consistência
```

**Tools (Ferramentas):**
- Claude recebe definição de 8 tools
- Pode chamar múltiplas tools em sequência
- Retorna tool_use ou text response

---

## 8. SCHEDULER (Timeout Proativo)

### 8.1.prv$Funcionamento

**Frequência:** A cada 20 minutos

**Processo:**
1. Busca contextos inativos (sem atividade há 1 hora)
2. Envia mensagem de encerramento proativa
3. Deleta contexto do banco
4. Log de todas as ações

**Mensagem:**
```
"Olá! Como você ficou um tempo sem responder, 
vou encerrar essa sessão. 😊

Quando quiser conversar novamente, é só me chamar!"
```

### 8.2 Vantagens

- Remove contextos órfãos
- Economiza tokens (não acumula histórico longo)
- Proatividade melhora experiência do usuário

---

## 9. DASHBOARD E ADMINISTRAÇÃO

### 9.1 Endpoints Administrativos

**Dashboard HTML:**
- `GET /dashboard` - Página HTML completa
- Bootstrap + Font Awesome
- Estatísticas em tempo real
- Tabela de consultas agendadas

**API Endpoints:**
```python
GET /api/appointments/scheduled
  - Retorna consultas com estatísticas
  - Formata datas para DD/MM/YYYY
  - Agrupa por status

GET /admin/appointments
  - Lista todas consultas

GET /admin/patients
  - Lista pacientes únicos
```

**Operações:**
```python
POST /admin/reload-config
  - Recarrega clinic_info.json sem restart

POST /admin/init-db
  - Cria tabelas do banco

POST /admin/clean-db
  - Remove tabelas antigas
```

### 9.2 Formatação de Datas no Dashboard

**Problema:** appointment_date no banco é string "20251022"

**Solução:**
```javascript
function formatDate(dateStr) {
    // Converter DD/MM/YYYY para Date object
    const parts = dateStr.split('/');
    const [day, month, year] = parts;
    const date = new Date(parseInt(year), parseInt(month) - 1, parseInt(day));
    return date.toLocaleDateString('pt-BR', {
        weekday: 'short',
        day: '2-digit',
        month: '2-digit',
        year: 'numeric'
    });
}
```

---

## 10. PONTOS CRÍTICOS E VULNERABILIDADES

### 10.1 Timezone Handling

**Problema:** Conversões de timezone podem causar bugs

**Código Atual:**
```python
# database.py
connect_args={
    "options": "-c timezone=America/Sao_Paulo"
}

# utils.py
def now_brazil():
    return datetime.now(get_brazil_timezone())
```

**Vulnerabilidade:**
- appointment_date salvo como string sem timezone
- Comparações diretas podem falhar
- Conversões múltiplas podem introduzir erros

### 10.2 Race Conditions

**Cenário:**
- 2 usuários escolhem mesmo horário simultaneamente
- Ambos passam na validação de slots
- Ambos criam agendamento

**Proteção Atual:**
- Validação dupla (listar + criar)
- Não há lock transacional no banco

**Recomendação:**
- Implementar constraint unique no banco
- Retry logic em caso de conflito

### 10.3 Extração de Dados

**Vulnerabilidade:**
- Regex pode falhar em casos edge
- Nomes não-latinos podem ser rejeitados
- Datas em formatos alternativos ignorados

**Código Atual:**
```python
if len(palavras_validas) >= 2:
    resultado["nome"] = nome_completo.title()
```

### 10.4 Loop Infinito de Tools

**Proteção:**
```python
max_iterations = 5
if iteration >= max_iterations:
    logger.error(f"❌ Limite de iterações atingido")
    bot_response = tool_result  # Fallback
```

**Vulnerabilidade:**
- Se Claude sempre retorna tool_use sem text
- Pode atingir limite e retornar resposta incompleta

---

## 11. PERFORMANCE E OTIMIZAÇÕES

### 11.1 Índices no Banco

```python
Index('idx_appointment_date_time_status', 'appointment_date', 'appointment_time', 'status')
Index('idx_patient_phone_status', 'patient_phone', 'status')
Index('idx_status_created', 'status', 'created_at')
```

### 11.2 Cache de Configurações

- clinic_info.json carregado uma vez no __init__
- Recarregável via endpoint admin sem restart
- Não há cache de contextos (sempre consulta banco)

### 11.3 Queries N+1

**Problema Potencial:**
- Buscar appointments e depois acessar propriedades
- Cada access pode trigger nova query

**Otimização Atual:**
- Query diretas no banco
- Uso de join explícito onde necessário

---

## 12. LOGGING E DEBUGGING

### 12.1 Níveis de Log

```python
logging.basicConfig(
    level=getattr(logging, settings.log_level),  # INFO por padrão
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

### 12.2 Pontos Críticos de Log

**Logs Importantes:**
- 🆕 Novo contexto criado
- 📱 Contexto carregado
- 🤖 Enviando mensagens para Claude
- 🔧 Tool executada
- ✅ Agendamento salvo
- ⚠️ Warnings de edge cases
- ❌ Erros críticos

### 12.3 Debug de Contextos

**Campo Útil:**
```python
logger.info(f"   flow_data atual: {data}")
logger.info(f"   Dados após extração: {data}")
```

---

## 13. CONFIGURAÇÃO E DEPLOY

### 13.1 Variáveis de Ambiente

```env
ANTHROPIC_API_KEY=sk-ant-...
WASENDER_API_KEY=...
WASENDER_PROJECT_NAME=clinica-bot
DATABASE_URL=postgresql://... (ou sqlite:///./data/appointments.db)
LOG_LEVEL=INFO
ENVIRONMENT=production
```

### 13.2 Railway Deployment

**railway.json:**
```json
{
  "build": {
    "builder": "NIXPACKS",
    "buildCommand": "pip install -r requirements.txt"
  },
  "deploy": {
    "startCommand": "python run.py",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

**Procfile:**
```
web: python run.py
```

### 13.3 Inicialização

```python
# lifespan no main.py
@asynccontextmanager
async def lifespan(app):
    # Startup
    init_db()
    start_scheduler()
    yield
    # Shutdown
    stop_scheduler()
```

---

## 14. CASOS DE USO E TESTES

### 14.1 Caso 1: Agendamento Normal

```
1. Usuário: "Olá"
2. Bot: Menu (3 opções)
3. Usuário: "1"
4. Bot: "Nome completo e data de nascimento"
5. Usuário: "João Silva, 15/03/1990"
6. Bot: "Tipo de consulta"
7. Usuário: "1"
8. Bot: "Convênio"
9. Usuário: "Particular"
10. Bot: "Data desejada"
11. Usuário: "25/11/2025"
12. Bot: Tool validate_date_and_show_slots → "Horários disponíveis: ..."
13. Usuário: "14:00"
14. Bot: Tool confirm_time_slot → "Resumo... Confirmar?"
15. Usuário: "Sim"
16. Bot: Tool create_appointment → "✅ Agendamento realizado!"
```

### 14.2 Caso 2: Atendimento Humano

```
1. Usuário: "Quero falar com alguém"
2. Bot: Tool request_human_assistance
3. Bot: "Transferindo para atendente..."
4. Banco: Cria PausedContact (2h)
5. Próxima mensagem: Ignorada
6. Após 2h: Bot reativa automaticamente
```

### 14.3 Caso 3: Encerramento Proativo

```
1. Usuário inicia conversa
2. Usuário para de responder
3. Após 1 hora: Scheduler detecta inatividade
4. Bot: "Encerrando sessão..."
5. Banco: Deleta contexto
```

---

## 15. RECOMENDAÇÕES DE MELHORIAS

### 15.1 Segurança

1. **Autenticação:** Adicionar token para endpoints admin
2. **Validação:** Rate limiting no webhook
3. **LGPD:** Adicionar endpoint de exclusão de dados

### 15.2 Performance

1. **Cache:** Implementar cache de contextos em memória (Redis)
2. **Pooling:** Aumentar pool_size do PostgreSQL em produção
3. **Async:** Migrar schedulers para async/await

### 15.3 Funcionalidades

1. **Lembretes:** Notificações 24h antes da consulta
2. **Remarcação:** Fluxo dedicado (não apenas cancelar + agendar)
3. **Múltiplos Agendamentos:** Permitir agendar 2+ consultas
4. **Upload de Arquivos:** Permitir envio de exames/docs

### 15.4 Robustez

1. **Retry Logic:** Implementar retry automático em falhas
2. **Circuit Breaker:** Proteger contra APIs externas down
3. **Idempotência:** IDs únicos para mensagens
4. **Monitoring:** Integrar com Sentry ou similar

---

## 16. DIAGRAMAS DE FLUXO

### 16.1 Fluxo de Agendamento (Simplificado)

```
[Início]
  ↓
[Menu Inicial]
  ↓
[Coletar Nome + Data Nasc]
  ↓
[Tipo Consulta]
  ↓
[Convênio]
  ↓
[Data Desejada]
  ↓
[validate_date_and_show_slots]
  ↓
[Escolher Horário]
  ↓
[confirm_time_slot]
  ↓
[Confirmação]
  ↓
[create_appointment]
  ↓
[Sucesso]
```

### 16.2 Gerenciamento de Contexto

```
[Mensagem Chega]
  ↓
[Contexto existe?]
  ├─ NÃO → Criar novo
  └─ SIM → Carregar
  ↓
[Deveria encerrar?]
  ├─ SIM → Deletar + Retornar mensagem
  └─ NÃO → Continuar
  ↓
[Adicionar mensagem ao histórico]
  ↓
[Enviar para Claude]
  ↓
[Processar Tools]
  ↓
[Salvar resposta]
  ↓
[Atualizar flow_data]
  ↓
[Commit no banco]
```

---

## 17. CONCLUSÃO

### 17.1 Pontos Fortes

✅ Arquitetura modular e separação de responsabilidades
✅ Uso eficiente de Claude AI com tools
✅ Contexto persistente entre mensagens
✅ Validações robustas de dados
✅ Dashboard administrativo funcional
✅ Deploy automatizado via Railway

### 17.2 Pontos de Atenção

⚠️ Conversões múltiplas de formato de data
⚠️ Possíveis race conditions em agendamentos simultâneos
⚠️ Falta de autenticação em endpoints admin
⚠️ Sem retry logic em falhas de API
⚠️ Extração de dados pode falhar em casos edge

### 17.3 Próximos Passos Sugeridos

1. Implementar testes automatizados
2. Adicionar monitoring e alerting
3. Melhorar tratamento de erros
4. Implementar retry logic
5. Adicionar documentação de API

---

**Documento gerado em:** $(date)
**Versão do código analisado:** 1.0.0
**Desenvolvedor:** Daniel Nóbrega Medeiros - Nobrega Medtech

