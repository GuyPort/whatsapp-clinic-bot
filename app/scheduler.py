"""
Scheduler para verificação automática de contextos inativos.
Lembretes de consulta agora são enviados pelo MedSystem (Django).
"""
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from app.database import get_db
from app.models import ConversationContext
import asyncio
import logging

logger = logging.getLogger(__name__)


async def check_inactive_contexts():
    """Verifica e encerra contextos inativos"""
    try:
        with get_db() as db:
            cutoff_time = datetime.utcnow() - timedelta(hours=1)
            inactive_contexts = db.query(ConversationContext).filter(
                ConversationContext.last_activity < cutoff_time
            ).all()

            logger.info(f"🔍 Verificando contextos inativos. Encontrados: {len(inactive_contexts)}")

            for context in inactive_contexts:
                logger.info(f"🕒 Encerrando contexto inativo para {context.phone}")
                db.delete(context)
                db.commit()
                logger.info(f"✅ Contexto encerrado e deletado para {context.phone}")

    except Exception as e:
        logger.error(f"❌ Erro ao verificar contextos inativos: {str(e)}")


def run_check():
    """Wrapper síncrono para executar tarefa assíncrona"""
    asyncio.run(check_inactive_contexts())


# Criar scheduler
scheduler = BackgroundScheduler()


def start_scheduler():
    """Inicia o scheduler"""
    scheduler.add_job(
        run_check,
        'interval',
        minutes=20,
        id='check_inactive_contexts'
    )
    scheduler.start()
    logger.info("✅ Scheduler iniciado: timeout de contextos inativos (20 min)")


def stop_scheduler():
    """Para o scheduler"""
    scheduler.shutdown()
    logger.info("🛑 Scheduler parado")
