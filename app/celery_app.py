"""
Configuração do Celery para processamento assíncrono de mensagens.
"""
from celery import Celery
from app.simple_config import settings
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
    
    # Configurações de worker
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
    
    # Roteamento de tasks para filas separadas
    task_routes={
        'app.main.send_message_task': {'queue': 'send_queue'},
        'app.main.process_message_task': {'queue': 'celery'},  # Fila padrão
    },
)

logger.info(f"✅ Celery configurado com broker: {settings.redis_url[:20]}...")

# Importar módulo onde a task está definida para registro automático
import app.main  # noqa: F401

