# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WhatsApp clinic bot for automated appointment scheduling using Claude AI, Evolution API (WhatsApp), FastAPI, PostgreSQL, and Celery task queues. The bot acts as "Beatriz" (secretary) for Consultório Dra. Rose.

## Common Commands

### Development
```bash
# Install dependencies
pip install -r requirements.txt

# Run development server (checks required env vars first)
python run.py

# Run with uvicorn directly
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run tests
pytest

# Run Celery worker locally
celery -A app.celery_app worker --loglevel=info --concurrency=10 -Q celery,send_queue
```

### Database Operations
```bash
# Initialize database (creates tables)
POST /admin/init-db

# Reload clinic configuration without restart
POST /admin/reload-config
```

### Production Deployment
The application runs two processes on Railway:
- `web`: FastAPI server (python run.py)
- `worker`: Celery worker for async message processing

## Architecture Overview

### Message Processing Pipeline
1. **Webhook Reception** ([main.py:webhook_whatsapp](app/main.py)): Evolution API posts incoming WhatsApp messages
2. **Celery Task Queue**: `process_message_task()` enqueued with phone/message/ID
3. **Redis Locking**: Per-contact lock ensures serial processing (prevents race conditions)
4. **AI Agent** ([ai_agent.py](app/ai_agent.py)): Claude SDK processes message with 15+ tools
5. **Response Queue**: `send_message_task()` enqueued for rate-limited sending
6. **WhatsApp Delivery**: Evolution API sends response (5s min interval between messages)

### Key Architectural Patterns

**Tool-Based AI Agent** ([ai_agent.py:5764 LOC](app/ai_agent.py))
- Claude receives system prompt defining "Beatriz" personality and business rules
- 15+ tools available: `create_appointment`, `cancel_appointment`, `find_next_available_slot`, etc.
- Tools execute within SQLAlchemy session context
- Claude returns JSON with tool_use blocks → agent parses and executes locally

**Conversation State Management**
- `ConversationContext` model stores per-contact message history and flow state
- Auto-expires after 1 hour of inactivity (scheduler job)
- Flow data tracks: patient name, birth date, consultation type, insurance plan

**Appointment Business Rules** ([appointment_rules.py](app/appointment_rules.py))
- 48-hour minimum advance booking (calculated from current time + 48h)
- Insurance-specific rules:
  - **IPE**: Max 3 appointments/day
  - **Particular**: Only option on Mondays
  - **CABERGS**: No restrictions
- Clinic hours: Mon 14-16h, Tue-Fri 14-19h (closed weekends)
- Appointments must be on whole hours (:00 minutes only)

**Rate Limiting & Concurrency**
- Redis distributed locks prevent concurrent processing of same contact
- 5-second minimum interval between WhatsApp messages (prevents API throttling)
- Celery task routing: `send_queue` for sending, `celery` for processing

### Critical Implementation Details

**Date/Time Formats** (INCONSISTENT - be careful!)
- Storage: `appointment_date` is **YYYYMMDD string** (e.g., "20251122")
- Display: Converted to **DD/MM/YYYY** (e.g., "22/11/2025") for user-facing text
- Birth dates: Always DD/MM/YYYY
- Time: HH:MM string, must be whole hours (e.g., "14:00", "15:00")

**Phone Normalization**
- Stored with country code: `5511999999999` (Brazil +55)
- WhatsApp format: `5511999999999@s.whatsapp.net`
- Use `normalize_phone()` from [utils.py](app/utils.py) for consistency

**Timezone Handling**
- Database stores UTC timestamps
- All business logic uses `America/Sao_Paulo` timezone
- Use `now_brazil()` from [utils.py](app/utils.py) for Brazil-aware current time

**Secretary /pause Command**
- Secretary sends `/pause` to patient contact via WhatsApp
- Webhook detects `is_from_me: true` flag
- Creates `PausedContact` record (2-hour expiry)
- Bot ignores messages from paused contacts until expiry

## Configuration Files

**[data/clinic_info.json](data/clinic_info.json)** - Editable clinic configuration
- Clinic hours, closed dates, consultation types, insurance plans
- Modified dynamically via `POST /admin/reload-config`
- Changes take effect immediately without restart

**Environment Variables** (`.env` not in repo)
Required for operation:
- `ANTHROPIC_API_KEY` - Claude API access
- `WASENDER_API_KEY`, `WASENDER_URL`, `WASENDER_PROJECT_NAME` - WaSender (Evolution API fork)
- `DATABASE_URL` - PostgreSQL connection string
- `REDIS_URL` - Redis for Celery broker and distributed locks

## Database Schema

**Three Core Models** ([models.py](app/models.py)):

1. **Appointment** - Scheduled consultations
   - Indexed by: `patient_phone`, `appointment_date`, `status`, `reminder_sent_at`
   - Status enum: `AGENDADA`, `CANCELADA`, `REALIZADA`
   - Validation hooks ensure: name present, proper date/time formats, birth date valid

2. **ConversationContext** - Per-contact chat state
   - Primary key: `phone` (WhatsApp number)
   - `messages` (JSON array): Full conversation history
   - `flow_data` (JSON dict): Collected data during current flow

3. **PausedContact** - Temporary bot silence
   - Used when secretary needs to take over conversation
   - Auto-expires after 2 hours

## Common Modification Tasks

| Task | Files to Modify |
|------|----------------|
| Change clinic hours | [data/clinic_info.json](data/clinic_info.json) |
| Add consultation type | [data/clinic_info.json](data/clinic_info.json) + [ai_agent.py](app/ai_agent.py) system prompt |
| Modify appointment duration | [data/clinic_info.json](data/clinic_info.json) + [appointment_rules.py](app/appointment_rules.py) |
| Change insurance rules | [appointment_rules.py](app/appointment_rules.py) + [ai_agent.py](app/ai_agent.py) system prompt |
| Add/remove holidays | [data/clinic_info.json](data/clinic_info.json) `dias_fechados` array |
| Modify conversation flow | [ai_agent.py](app/ai_agent.py) system prompt + tool definitions |
| Change rate limiting | [whatsapp_service.py](app/whatsapp_service.py) `send_message()` Redis lock timeout |
| Adjust reminder timing | [scheduler.py](app/scheduler.py) `send_appointment_reminders()` window |

## Background Jobs

**APScheduler Tasks** ([scheduler.py](app/scheduler.py)):
- **check_inactive_contexts()**: Every 20 min - expires conversations inactive >1h
- **send_appointment_reminders()**: Every 1h - sends 24h prior reminder (20-26h window)

## API Endpoints

**Public**:
- `GET /` - HTML homepage
- `GET /health` - Health check
- `GET /status` - System status (WhatsApp, DB, Calendar)
- `GET /dashboard` - Modern appointment dashboard

**Webhooks**:
- `POST /webhook/whatsapp` - Main webhook for Evolution API

**Admin**:
- `POST /admin/reload-config` - Hot-reload [clinic_info.json](data/clinic_info.json)
- `GET/POST /admin/init-db` - Initialize database tables
- `GET /admin/patients` - List all patients
- `GET /admin/appointments` - List all appointments
- `GET /api/appointments/scheduled` - JSON API for dashboard

## Testing Notes

- Test framework: pytest
- For local testing, SQLite fallback is available (`data/appointments.db`)
- Production uses PostgreSQL via Railway
- Test environment should set `DATABASE_URL` to SQLite or test PostgreSQL instance

## Important Constraints

1. **48-Hour Rule**: Appointments must be booked ≥48 hours in advance (enforced in `find_next_available_slot()`)
2. **Identity Verification**: Requires name + birth date match for cancellation/rescheduling
3. **Holiday Closure**: Currently closed Nov 14 - Dec 30, 2025 and Jan 13, 2026 (Thanksgiving through New Year)
4. **Insurance Limits**: IPE max 3 appointments/day, Particular only on Mondays
5. **Time Slots**: Only whole hours available (14:00, 15:00, etc. - no 14:30)
