"""
Scheduler para verificação automática de contextos inativos.
Encerra proativamente conversas que ficaram sem resposta por 30+ minutos.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from app.database import get_db
from app.models import ConversationContext, Appointment, AppointmentStatus
from app.whatsapp_service import whatsapp_service
from app.utils import (
    now_brazil,
    parse_appointment_datetime,
    format_pre_appointment_reminder,
)
import asyncio
import logging

logger = logging.getLogger(__name__)

async def check_inactive_contexts():
    """Verifica e encerra contextos inativos"""
    try:
        with get_db() as db:
            # Buscar contextos inativos há mais de 1 minuto (para teste)
            cutoff_time = datetime.utcnow() - timedelta(hours=1)
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


async def send_appointment_reminders():
    """Envia lembretes automáticos 24h antes das consultas agendadas."""
    try:
        now = now_brazil()
        window_start = now + timedelta(hours=24) - timedelta(hours=2)
        window_end = now + timedelta(hours=24) + timedelta(minutes=5)
        
        candidate_dates = {
            window_start.strftime("%Y%m%d"),
            window_end.strftime("%Y%m%d"),
        }
        
        sent_count = 0
        with get_db() as db:
            appointments = db.query(Appointment).filter(
                Appointment.status == AppointmentStatus.AGENDADA,
                Appointment.reminder_sent_at.is_(None),
                Appointment.appointment_date.in_(candidate_dates)
            ).all()
            
            logger.info(f"🔔 Verificando lembretes: {len(appointments)} candidatos encontrados")
            
            for appointment in appointments:
                appointment_dt = parse_appointment_datetime(
                    appointment.appointment_date,
                    appointment.appointment_time
                )
                
                if not appointment_dt:
                    logger.warning(
                        "⚠️ Não foi possível converter data/hora do agendamento",
                        extra={"appointment_id": appointment.id}
                    )
                    continue
                
                if not (window_start <= appointment_dt <= window_end):
                    continue
                
                message = format_pre_appointment_reminder(
                    appointment.patient_name,
                    appointment_dt
                )
                
                success = await whatsapp_service.send_message(
                    appointment.patient_phone,
                    message
                )
                
                if success:
                    appointment.reminder_sent_at = datetime.utcnow()
                    db.add(appointment)
                    db.commit()
                    sent_count += 1
                    logger.info(
                        "✅ Lembrete enviado",
                        extra={
                            "appointment_id": appointment.id,
                            "patient_phone": appointment.patient_phone,
                            "appointment_datetime": appointment_dt.isoformat()
                        }
                    )
                else:
                    logger.error(
                        "❌ Falha ao enviar lembrete",
                        extra={
                            "appointment_id": appointment.id,
                            "patient_phone": appointment.patient_phone
                        }
                    )
        if sent_count:
            logger.info(f"📬 Lembretes enviados: {sent_count}")
    except Exception as e:
        logger.error(f"❌ Erro ao enviar lembretes: {str(e)}", exc_info=True)


def run_send_reminders():
    """Wrapper síncrono para envio dos lembretes."""
    asyncio.run(send_appointment_reminders())

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
    scheduler.add_job(
        run_send_reminders,
        'interval',
        minutes=1,
        id='send_appointment_reminders'
    )
    scheduler.start()
    logger.info("✅ Scheduler iniciado: timeout (20 min) e lembretes (1 min)")

def stop_scheduler():
    """Para o scheduler"""
    scheduler.shutdown()
    logger.info("🛑 Scheduler de timeout parado")