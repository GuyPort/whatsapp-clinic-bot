"""
Configuração do Celery para processamento assíncrono de mensagens.
"""
from celery import Celery
from celery.exceptions import Retry as CeleryRetry
from app.simple_config import settings
from app.conversation_state import EnqueueResult
import logging

logger = logging.getLogger(__name__)

# Criar instância do Celery
celery_app = Celery(
    'clinic_bot',
    broker=settings.redis_url,
    backend=settings.redis_url
)

# Configurações do Celery
celery_app.conf.update(
    # Serialização JSON
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='America/Sao_Paulo',
    enable_utc=True,
    
    # Configurações de retry
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    
    # Timeouts
    task_time_limit=300,  # 5 minutos máximo por task
    task_soft_time_limit=240,  # 4 minutos soft limit
    
    # Resultado expira após 1 hora
    result_expires=3600,
    broker_connection_timeout=2,
    broker_transport_options={"socket_connect_timeout": 2, "socket_timeout": 2,
                              "retry_on_timeout": False},
    task_publish_retry=False,
    beat_schedule={"conversation-recovery": {
        "task": "app.main.recover_conversations_task",
        "schedule": settings.batch_recovery_interval_seconds or 20,
        "options": {"expires": settings.batch_recovery_interval_seconds or 20},
    }},
    
    # Configurações de worker
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
    
    # Roteamento de tasks para filas separadas
    task_routes={
        'app.main.send_message_task': {'queue': 'send_queue'},
        'app.main.process_message_task': {'queue': 'celery'},  # Fila padrão
    },
)

logger.info("conversation_celery_configured")


def probe_broker(app):
    """Connection only, one bounded attempt; never publish or consume work."""
    try:
        with app.connection_for_read(connect_timeout=2, transport_options={
                "socket_connect_timeout": 2, "socket_timeout": 2,
                "retry_on_timeout": False}) as connection:
            connection.ensure_connection(max_retries=0)
            return connection.connected is True
    except Exception:
        return False


class CeleryProcessingBroker:
    """Injected task/probe: construction never opens a broker connection."""
    def __init__(self, task, *, probe, debounce_seconds=10):
        self.task, self._probe, self.debounce_seconds = task, probe, debounce_seconds

    def probe(self):
        return self._probe() is True

    def enqueue_processing(self, command):
        payload = command.to_payload()
        try:
            self.task.apply_async(args=[payload], countdown=self.debounce_seconds,
                argsrepr="(<conversation_command>,)", kwargsrepr="{}")
        except CeleryRetry:
            raise
        except Exception:
            return EnqueueResult.AMBIGUOUS
        return EnqueueResult.CONFIRMED


class CeleryOutboundBroker:
    def __init__(self, task):
        self.task = task

    def enqueue_outbound(self, outbound):
        payload = outbound.to_payload()
        try:
            self.task.apply_async(args=[payload], argsrepr="(<conversation_outbound>,)", kwargsrepr="{}")
        except CeleryRetry:
            raise
        except Exception:
            return EnqueueResult.AMBIGUOUS
        return EnqueueResult.CONFIRMED

# Importar módulo onde a task está definida para registro automático
import app.main  # noqa: F401

