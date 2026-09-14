"""
Scheduler para verificação automática de contextos inativos.
Lembretes de consulta agora são enviados pelo MedSystem (Django).
"""
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import timedelta, timezone
from uuid import uuid4
from app.conversation_tasks import _require_ready
from app.models import ConversationContext
from app.utils import AuditEvent, ConversationAuditLogger, new_audit_correlation_id
import asyncio
import logging

logger = logging.getLogger(__name__)
conversation_audit = ConversationAuditLogger(logger)


def _emit_audit(event: AuditEvent, **fields: object) -> None:
    conversation_audit.emit(event, correlation_id=new_audit_correlation_id(), **fields)


async def check_inactive_contexts(runtime=None):
    """Scan phone IDs, then recheck/delete each context under its own lease."""
    failure_outcome = "dependency_unavailable"
    try:
        _require_ready(runtime)
        failure_outcome = "persistence_failed"
        cutoff = runtime.clock.now() - timedelta(hours=1)
        with runtime.session_factory() as db:
            phones = db.query(ConversationContext.phone).filter(
                ConversationContext.last_activity < cutoff.astimezone(timezone.utc).replace(tzinfo=None)
            ).all()
    except Exception:
        _emit_audit(AuditEvent.RECOVERY, outcome=failure_outcome, attempt_state="closed")
        return

    for (phone,) in phones:
        try:
            _require_ready(runtime)
        except Exception:
            _emit_audit(AuditEvent.RECOVERY, outcome="dependency_unavailable", attempt_state="closed")
            return
        try:
            with runtime.store.contact_lease(phone) as lease, runtime.session_factory() as db:
                _require_ready(runtime)
                runtime.coordinator.close_inactive_context(db, phone, cutoff,
                    runtime.clock.now(), lease, str(uuid4()))
        except Exception:
            _emit_audit(AuditEvent.RECOVERY, outcome="coordination_failed", attempt_state="failed")
    _emit_audit(AuditEvent.RECOVERY, outcome="recovered", attempt_state="completed",
                count=min(len(phones), 1_000_000))


def run_check(runtime=None):
    """Wrapper síncrono para executar tarefa assíncrona"""
    asyncio.run(check_inactive_contexts(runtime))


# Criar scheduler
scheduler = BackgroundScheduler()


def start_scheduler(runtime=None):
    """Use the composed runtime and remain inert while dependencies are closed."""
    try:
        _require_ready(runtime)
    except Exception:
        _emit_audit(AuditEvent.READINESS, outcome="dependency_unavailable")
        return False
    scheduler.add_job(
        run_check,
        'interval',
        minutes=20,
        id='check_inactive_contexts',
        kwargs={"runtime": runtime}
    )
    scheduler.start()
    _emit_audit(AuditEvent.RECOVERY, outcome="started", attempt_state="scheduled")
    return True


def stop_scheduler():
    """Para o scheduler"""
    scheduler.shutdown()
    _emit_audit(AuditEvent.RECOVERY, outcome="stopped", attempt_state="terminal")
