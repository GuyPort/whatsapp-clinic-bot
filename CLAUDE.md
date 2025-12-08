# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WhatsApp Clinic Bot - An AI-powered appointment scheduling chatbot for medical clinics (currently configured for "Consultório Dra. Rose"). Built with FastAPI, Claude 3.5 Sonnet, and Evolution API/WAsender for WhatsApp integration.

## Commands

### Development
```bash
# Activate virtual environment (Windows)
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run locally with hot reload
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run via entry point (validates environment first)
python run.py

# Run Celery worker for async tasks
celery -A app.celery_app worker --loglevel=info --concurrency=10 -Q celery,send_queue
```

### Testing
```bash
pytest
```

## Architecture

### Core Flow
1. WhatsApp messages arrive at `POST /webhook/whatsapp` (main.py)
2. Message extracted, phone normalized, conversation context loaded/created
3. ClaudeToolAgent (ai_agent.py) processes message with tool-use loop
4. Response sent via WhatsAppService (whatsapp_service.py) with Redis-based rate limiting
5. Context and appointments persisted to PostgreSQL

### Key Components

- **app/main.py**: FastAPI app with webhook handlers, health endpoints, and admin routes (HTTP Basic Auth)
- **app/ai_agent.py**: ClaudeToolAgent - core AI logic with tool definitions for appointment CRUD, availability checking
- **app/models.py**: SQLAlchemy models (Appointment, ConversationContext, PausedContact) with status enum
- **app/appointment_rules.py**: Business logic for appointment validation (insurance restrictions, quotas, time slots)
- **app/whatsapp_service.py**: Evolution API client with Redis locks and rate limiting (1 msg/5s)
- **app/scheduler.py**: APScheduler jobs for inactive context cleanup (1h) and 24h appointment reminders
- **app/celery_app.py**: Celery config with separate queues for message sending and processing
- **data/clinic_info.json**: Clinic configuration (hours, consultation types, insurance plans, pricing)

### State Management
- Conversation flows tracked via `current_flow` field: `agendamento`, `cancelamento`, `duvidas`
- Message history stored as JSON in ConversationContext
- Appointment statuses: `AGENDADA`, `COMPARECEU`, `NAO_COMPARECEU`, `CANCELADA`

### External Services
- **Anthropic API**: Claude 3.5 Sonnet for conversational AI
- **WAsender/Evolution API**: WhatsApp messaging (configured at wasenderapi.com)
- **PostgreSQL**: Production database (Railway), SQLite fallback locally
- **Redis**: Message broker for Celery, rate limiting locks

## Configuration

### Required Environment Variables
```
ANTHROPIC_API_KEY
WASENDER_API_KEY
WASENDER_PROJECT_NAME=clinica-bot
WASENDER_URL=https://wasenderapi.com
DATABASE_URL
REDIS_URL
ADMIN_PASSWORD
```

### Clinic Configuration
Edit `data/clinic_info.json` to modify:
- Operating hours (`horario_funcionamento`, `horario_atendimento`)
- Consultation types and pricing (`tipos_consulta`)
- Insurance plans and restrictions (`convenios_aceitos`)
- Closed dates and manual scheduling periods (`dias_fechados`, `periodo_agendamento_manual`)
- Scheduling rules (intervals, durations)

Reload config at runtime: `POST /admin/reload-config` (requires admin auth)

## Business Rules

- Insurance-based restrictions: Mondays = Particular only
- IPE daily quota: max 3 appointments
- Cancellation policy: 24 hours notice required
- Appointment slots: hourly increments, configurable duration (default 60min)
- Timezone: America/Sao_Paulo (all date/time operations)
