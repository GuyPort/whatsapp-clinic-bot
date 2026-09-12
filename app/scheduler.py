"""
Scheduler para verificação automática de contextos inativos.
Lembretes de consulta agora são enviados pelo MedSystem (Django).
"""
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import timedelta, timezone
from uuid import uuid4
from app.conversation_tasks import _require_ready
from app.models import ConversationContext
import asyncio
import logging

logger = logging.getLogger(__name__)


async def check_inactive_contexts(runtime=None):
    """Scan phone IDs, then recheck/delete each context under its own lease."""
    try:
        _require_ready(runtime)
        cutoff = runtime.clock.now() - timedelta(hours=1)
        with runtime.session_factory() as db:
            phones = db.query(ConversationContext.phone).filter(
                ConversationContext.last_activity < cutoff.astimezone(timezone.utc).replace(tzinfo=None)
            ).all()
    except Exception:
        logger.warning("conversation_cleanup_unavailable")
        return

    for (phone,) in phones:
        try:
            _require_ready(runtime)
        except Exception:
            logger.warning("conversation_cleanup_unavailable")
            return
        try:
            with runtime.store.contact_lease(phone) as lease, runtime.session_factory() as db:
                runtime.coordinator.close_inactive_context(db, phone, cutoff,
                    runtime.clock.now(), lease, str(uuid4()))
        except Exception:
            logger.warning("conversation_cleanup_contact_unavailable")


def run_check(runtime=None):
    """Wrapper síncrono para executar tarefa assíncrona"""
    asyncio.run(check_inactive_contexts(runtime))


# Criar scheduler
scheduler = BackgroundScheduler()


def start_scheduler(runtime=None):
    """Task 9 supplies the runtime; missing composition remains closed."""
    scheduler.add_job(
        run_check,
        'interval',
        minutes=20,
        id='check_inactive_contexts',
        kwargs={"runtime": runtime}
    )
    scheduler.start()
    logger.info("conversation_cleanup_started")


def stop_scheduler():
    """Para o scheduler"""
    scheduler.shutdown()
    logger.info("conversation_cleanup_stopped")
