"""
Scheduler para verificação automática de contextos inativos.
Encerra proativamente conversas que ficaram sem resposta por 30+ minutos.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from app.database import get_db
from app.models import ConversationContext
from app.whatsapp_service import whatsapp_service
import asyncio
import logging

logger = logging.getLogger(__name__)

async def check_inactive_contexts():
    """Verifica e encerra contextos inativos"""
    try:
        with get_db() as db:
            # Buscar contextos inativos há mais de 30 minutos
            cutoff_time = datetime.utcnow() - timedelta(minutes=30)
            inactive_contexts = db.query(ConversationContext).filter(
                ConversationContext.last_activity < cutoff_time
            ).all()
            
            logger.info(f"🔍 Verificando contextos inativos. Encontrados: {len(inactive_contexts)}")
            
            for context in inactive_contexts:
                logger.info(f"🕒 Encerrando contexto inativo para {context.phone}")
                
                # Enviar mensagem de encerramento
                message = (
                    "Olá! Como você ficou um tempo sem responder, "
                    "vou encerrar essa sessão. 😊\n\n"
                    "Quando quiser conversar novamente, é só me chamar!"
                )
                
                try:
                    await whatsapp_service.send_message(context.phone, message)
                    logger.info(f"📤 Mensagem de encerramento enviada para {context.phone}")
                except Exception as e:
                    logger.error(f"❌ Erro ao enviar mensagem para {context.phone}: {str(e)}")
                
                # Deletar contexto
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
        minutes=5,  # Executar a cada 5 minutos
        id='check_inactive_contexts'
    )
    scheduler.start()
    logger.info("✅ Scheduler de timeout iniciado (verificação a cada 5 min)")

def stop_scheduler():
    """Para o scheduler"""
    scheduler.shutdown()
    logger.info("🛑 Scheduler de timeout parado")
