"""
Aplicação FastAPI principal com webhooks do WhatsApp.
"""
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from contextlib import asynccontextmanager
import logging
from typing import Dict, Any, List
from datetime import datetime, date

from app.simple_config import settings

from app.database import init_db, get_db
from app.ai_agent import ai_agent
from app.whatsapp_service import whatsapp_service
from app.utils import normalize_phone
from app.models import Appointment, ConversationContext, PausedContact, AppointmentStatus
from app.scheduler import start_scheduler, stop_scheduler
from app.celery_app import celery_app
import asyncio

# Configurar logging
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle da aplicação"""
    # Startup
    logger.info("🚀 Iniciando bot da clínica...")
    init_db()
    start_scheduler()  # Iniciar scheduler de timeout proativo
    logger.info("✅ Bot iniciado com sucesso!")
    
    yield
    
    # Shutdown
    stop_scheduler()  # Parar scheduler
    logger.info("👋 Encerrando bot da clínica...")


# Criar aplicação FastAPI
app = FastAPI(
    title="WhatsApp Clinic Bot",
    description="Bot de WhatsApp para agendamento de consultas em clínica",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/", response_class=HTMLResponse)
async def root():
    """Página inicial"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>WhatsApp Clinic Bot</title>
        <meta charset="utf-8">
        <style>
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                max-width: 800px;
                margin: 50px auto;
                padding: 20px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
            }
            .container {
                background: rgba(255, 255, 255, 0.95);
                color: #333;
                padding: 40px;
                border-radius: 15px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            }
            h1 {
                color: #667eea;
                margin-bottom: 10px;
            }
            .status {
                display: inline-block;
                padding: 5px 15px;
                background: #10b981;
                color: white;
                border-radius: 20px;
                font-size: 14px;
                margin: 20px 0;
            }
            .info {
                background: #f3f4f6;
                padding: 20px;
                border-radius: 10px;
                margin: 20px 0;
            }
            .info h3 {
                margin-top: 0;
                color: #667eea;
            }
            ul {
                line-height: 1.8;
            }
            .footer {
                text-align: center;
                margin-top: 30px;
                font-size: 14px;
                color: #666;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🤖 WhatsApp Clinic Bot</h1>
            <div class="status">✅ Online</div>
            
            <div class="info">
                <h3>📋 Funcionalidades</h3>
                <ul>
                    <li>✅ Responder dúvidas sobre a clínica</li>
                    <li>✅ Agendar consultas automaticamente</li>
                    <li>✅ Cancelar e remarcar consultas</li>
                    <li>✅ Integração com Google Calendar</li>
                    <li>✅ Operação 24/7</li>
                    <li>✅ Escalação inteligente para atendimento humano</li>
                </ul>
            </div>
            
            <div class="info">
                <h3>🔧 Tecnologias</h3>
                <ul>
                    <li><strong>IA:</strong> Claude 3.5 Sonnet (Anthropic)</li>
                    <li><strong>WhatsApp:</strong> Evolution API</li>
                    <li><strong>Backend:</strong> FastAPI + Python</li>
                    <li><strong>Banco:</strong> SQLite + SQLAlchemy</li>
                    <li><strong>Calendário:</strong> Google Calendar API</li>
                </ul>
            </div>
            
            <div class="info">
                <h3>📊 Endpoints</h3>
                <ul>
                    <li><code>GET /</code> - Esta página</li>
                    <li><code>GET /dashboard</code> - Dashboard de consultas</li>
                    <li><code>GET /health</code> - Health check</li>
                    <li><code>POST /webhook/whatsapp</code> - Webhook do WhatsApp</li>
                </ul>
            </div>
            
            <div class="info">
                <h3>🎛️ Painel de Controle</h3>
                <p>Visualize todas as consultas agendadas em tempo real:</p>
                <a href="/dashboard" class="btn btn-primary btn-lg">
                    <i class="fas fa-chart-line"></i> Abrir Dashboard
                </a>
            </div>
            
            <div class="footer">
                <p>Desenvolvido com ❤️ para automatização de clínicas</p>
            </div>
        </div>
    </body>
    </html>
    """


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "whatsapp-clinic-bot",
        "version": "1.0.0"
    }


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    """
    Webhook para receber mensagens do Evolution API.
    
    Evolution API envia payloads no formato:
    {
        "event": "messages.upsert",
        "instance": "instance_name",
        "data": {
            "key": {
                "remoteJid": "5511999999999@s.whatsapp.net",
                "fromMe": false,
                "id": "message_id"
            },
            "message": {
                "conversation": "texto da mensagem",
                "extendedTextMessage": {
                    "text": "texto"
                }
            },
            "messageTimestamp": "1234567890",
            "pushName": "Nome do Usuário"
        }
    }
    """
    try:
        payload = await request.json()
        logger.info(f"Webhook recebido: {payload.get('event')}")
        logger.info(f"Payload completo: {payload}")  # DEBUG: Ver payload completo
        
        # Verificar se é mensagem recebida (não enviada por nós)
        event = payload.get('event', '')
        if event not in ['messages.upsert', 'messages.received']:
            return {"status": "ignored", "reason": "not a message event"}
        
        data = payload.get('data', {})
        messages = data.get('messages', {})
        key = messages.get('key', {})
        message_data = messages.get('message', {})
        
        # Ignorar mensagens enviadas por nós
        if key.get('fromMe', False):
            return {"status": "ignored", "reason": "message from bot"}
        
        # Extrair informações
        phone = key.get('remoteJid', '')
        
        # Ignorar mensagens de newsletter e grupos
        if '@newsletter' in phone or '@g.us' in phone:
            logger.info(f"Ignorando mensagem de newsletter/grupo: {phone}")
            return {"status": "ignored", "reason": "newsletter or group message"}
        
        phone = phone.replace('@s.whatsapp.net', '')
        
        # Extrair texto da mensagem
        message_text = None
        if 'conversation' in message_data:
            message_text = message_data['conversation']
        elif 'extendedTextMessage' in message_data:
            message_text = message_data['extendedTextMessage'].get('text', '')
        elif 'imageMessage' in message_data:
            message_text = message_data['imageMessage'].get('caption', '')
        
        if not message_text or not phone:
            logger.warning("Mensagem sem texto ou telefone")
            return {"status": "ignored", "reason": "no text or phone"}
        
        logger.info(f"Mensagem de {phone}: {message_text[:50]}...")
        
        # Enfileirar task no Celery
        task = process_message_task.delay(phone, message_text, key.get('id'))
        logger.info(f"📨 Task enfileirada: {task.id} para {phone}")
        
        return {"status": "processing", "task_id": task.id}
        
    except Exception as e:
        logger.error(f"Erro no webhook: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _send_message_sync(phone: str, message: str) -> bool:
    """
    Wrapper síncrono para whatsapp_service.send_message (async).
    Usado dentro de tasks Celery que são síncronas.
    """
    try:
        return asyncio.run(whatsapp_service.send_message(phone, message))
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem via wrapper síncrono: {str(e)}")
        return False


def _mark_message_as_read_sync(phone: str, message_id: str) -> bool:
    """
    Wrapper síncrono para whatsapp_service.mark_message_as_read (async).
    Usado dentro de tasks Celery que são síncronas.
    """
    try:
        return asyncio.run(whatsapp_service.mark_message_as_read(phone, message_id))
    except Exception as e:
        logger.error(f"Erro ao marcar mensagem como lida via wrapper síncrono: {str(e)}")
        return False


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_message_task(self, phone: str, message: str):
    """
    Task Celery dedicada para envio de mensagens para WhatsApp API.
    Esta task é roteada para a fila 'send_queue' e usa rate limiting de 5 segundos.
    
    Args:
        phone: Número do telefone
        message: Texto da mensagem a ser enviada
    """
    task_id = self.request.id
    logger.info(f"📤 Task de envio {task_id} iniciada para {phone}")
    
    try:
        # Normalizar telefone
        phone = normalize_phone(phone)
        
        # Enviar mensagem usando wrapper síncrono (já tem rate limiting)
        success = _send_message_sync(phone, message)
        
        if success:
            logger.info(f"✅ Task de envio {task_id} concluída - Mensagem enviada para {phone}")
        else:
            logger.error(f"❌ Task de envio {task_id} - Falha ao enviar mensagem para {phone}")
            # Retry automático se falhou
            raise Exception("Falha ao enviar mensagem")
            
    except Exception as e:
        logger.error(f"❌ Task de envio {task_id} - Erro: {str(e)}", exc_info=True)
        # Retry automático do Celery
        raise self.retry(exc=e)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_message_task(self, phone: str, message_text: str, message_id: str = None):
    """
    Processa mensagem em background usando Celery.
    
    Args:
        phone: Número do telefone
        message_text: Texto da mensagem
        message_id: ID da mensagem (para marcar como lida)
    """
    task_id = self.request.id
    logger.info(f"🔄 Task {task_id} iniciada para {phone}: {message_text[:50]}...")
    
    lock = None
    lock_acquired = False
    
    try:
        # Normalizar telefone
        phone = normalize_phone(phone)
        
        # Garantir processamento serializado por contato
        lock = whatsapp_service.acquire_chat_lock(phone)
        if lock:
            try:
                lock_acquired = lock.acquire(blocking=True)
            except Exception as lock_error:
                logger.warning(f"⚠️ Não foi possível adquirir lock para {phone}: {lock_error}")
                raise self.retry(exc=lock_error, countdown=2)
            
            if not lock_acquired:
                logger.warning(f"⚠️ Lock ocupado para {phone}, reagendando task")
                raise self.retry(exc=Exception("chat_lock_busy"), countdown=2)
        else:
            logger.warning(f"⚠️ Processando {phone} sem lock - Redis indisponível")
        
        # Marcar como lida
        if message_id:
            _mark_message_as_read_sync(phone, message_id)
        
        # Verificar se bot está pausado para este telefone
        with get_db() as db:
            paused_contact = db.query(PausedContact).filter_by(phone=phone).first()
            
            if paused_contact:
                if datetime.utcnow() < paused_contact.paused_until:
                    # Ainda pausado - bot ignora mensagem
                    logger.info(f"Bot pausado para {phone} até {paused_contact.paused_until}")
                    return
                else:
                    # Passou 2 horas - reativar silenciosamente
                    logger.info(f"Bot reativado automaticamente para {phone}")
                    db.delete(paused_contact)
                    db.commit()
            
        # Processar com IA
        response = ai_agent.process_message(message_text, phone, db)
        
        # Enfileirar mensagem para envio na fila separada
        if response:
            send_task = send_message_task.delay(phone, response)
            logger.info(f"✅ Task {task_id} concluída - Resposta enfileirada para envio (task: {send_task.id})")
        else:
            logger.warning(f"⚠️ Task {task_id} - Nenhuma resposta gerada para {phone}")
        
    except CeleryRetry:
        raise
    except Exception as e:
        try:
            from celery.exceptions import Retry as CeleryRetry  # type: ignore
        except ImportError:
            CeleryRetry = None
        
        if CeleryRetry and isinstance(e, CeleryRetry):
            raise e
        
        logger.error(f"❌ Task {task_id} - Erro ao processar mensagem: {str(e)}", exc_info=True)
        
        error_text = str(e).lower()
        concurrency_issue = any(
            issue in error_text
            for issue in ["database is locked", "chat_lock_busy", "deadlock", "could not obtain lock"]
        )
        
        if concurrency_issue:
            logger.warning(f"⚠️ Erro de concorrência detectado para {phone}; retry silencioso.")
        else:
            # Tentar enfileirar mensagem de erro ao usuário
            try:
                send_message_task.delay(
                    phone,
                    "Desculpe, ocorreu um erro ao processar sua mensagem. Por favor, tente novamente em instantes."
                )
                logger.info(f"📤 Mensagem de erro enfileirada para {phone}")
            except Exception as send_error:
                logger.error(f"❌ Task {task_id} - Erro ao enfileirar mensagem de erro: {str(send_error)}")
        
        # Retry automático do Celery se necessário
        raise self.retry(exc=e, countdown=2 if concurrency_issue else 60)
    finally:
        if lock and lock_acquired:
            try:
                lock.release()
            except Exception as release_error:
                logger.warning(f"⚠️ Erro ao liberar lock de {phone}: {release_error}")


@app.get("/status")
async def status():
    """Retorna status detalhado do sistema"""
    try:
        # Verificar WhatsApp
        whatsapp_status = await whatsapp_service.get_instance_status()
        
        return {
            "status": "operational",
            "whatsapp": whatsapp_status,
            "database": "connected"
        }
    except Exception as e:
        logger.error(f"Erro ao verificar status: {str(e)}")
        return {
            "status": "degraded",
            "error": str(e)
        }


@app.post("/admin/reload-config")
async def reload_config():
    """
    Recarrega configurações da clínica sem reiniciar o servidor.
    Útil para atualizar valores, horários, etc.
    """
    try:
        ai_agent.reload_clinic_info()
        return {"status": "success", "message": "Configurações recarregadas"}
    except Exception as e:
        logger.error(f"Erro ao recarregar config: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== ENDPOINTS DO BANCO DE DADOS ====================

@app.get("/admin/patients")
async def get_patients():
    """Lista todos os pacientes únicos baseado nas consultas"""
    try:
        with get_db() as db:
            appointments = db.query(Appointment).order_by(Appointment.created_at.desc()).all()
            patients = []
            seen_patients = set()
            
            for apt in appointments:
                patient_key = f"{apt.patient_name}_{apt.patient_birth_date}"
                if patient_key not in seen_patients:
                    patients.append({
                        "id": apt.id,
                        "name": apt.patient_name,
                        "phone": "N/A",
                        "birth_date": apt.patient_birth_date,
                        "created_at": apt.created_at.isoformat(),
                        "appointments_count": 1  # Contagem simplificada
                    })
                    seen_patients.add(patient_key)
                else:
                    # Incrementar contador se já existe
                    for p in patients:
                        if f"{p['name']}_{p['birth_date']}" == patient_key:
                            p['appointments_count'] += 1
                            break
            
            return {
                "total": len(patients),
                "patients": patients
            }
    except Exception as e:
        logger.error(f"Erro ao buscar pacientes: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


def _format_appointment_date(date_value):
    """Converte qualquer formato de data para DD/MM/YYYY"""
    if isinstance(date_value, str):
        # Se for string YYYYMMDD (ex: "20251022")
        if len(date_value) == 8 and date_value.isdigit():
            return f"{date_value[6:8]}/{date_value[4:6]}/{date_value[0:4]}"
        # Se for string DD-MM-YYYY (ex: "22-10-2025")
        elif '-' in date_value:
            return date_value.replace('-', '/')
        # Se for string DD/MM/YYYY (ex: "22/10/2025")
        elif '/' in date_value:
            return date_value
    elif hasattr(date_value, 'strftime'):
        # Se for datetime.date ou datetime.datetime
        return date_value.strftime('%d/%m/%Y')
    
    return str(date_value)

@app.get("/admin/appointments")
async def get_appointments():
    """Lista todas as consultas agendadas"""
    try:
        with get_db() as db:
            appointments = db.query(Appointment).order_by(Appointment.appointment_date.desc()).all()
            return {
                "total": len(appointments),
                "appointments": [
                    {
                        "id": a.id,
                        "patient_name": a.patient_name,
                        "patient_phone": "N/A",
                        "appointment_date": _format_appointment_date(a.appointment_date),  # ← FORMATARZ AQUI
                        "appointment_time": a.appointment_time,
                        "patient_birth_date": a.patient_birth_date,
                        "created_at": a.created_at.isoformat()
                    }
                    for a in appointments
                ]
            }
    except Exception as e:
        logger.error(f"Erro ao buscar consultas: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/appointments/scheduled")
async def get_scheduled_appointments():
    """API para o dashboard - consultas agendadas com estatísticas"""
    try:
        with get_db() as db:
            from datetime import datetime, timedelta
            
            # Buscar todas as consultas ORDENADAS POR DATA DA CONSULTA (crescente)
            appointments = db.query(Appointment).order_by(
                Appointment.appointment_date.asc(),  # Data crescente
                Appointment.appointment_time.asc()   # Horário crescente
            ).all()
            
            # Calcular estatísticas
            today = datetime.now().date()
            week_start = today - timedelta(days=today.weekday())  # Início da semana
            week_end = week_start + timedelta(days=6)  # Fim da semana
            
            # Contar pacientes únicos
            unique_patients = set()
            for apt in appointments:
                unique_patients.add(f"{apt.patient_name}_{apt.patient_birth_date}")
            
            # Calcular estatísticas com formato com hífen
            today_str = today.strftime('%Y%m%d')
            week_start_str = week_start.strftime('%Y%m%d')
            week_end_str = week_end.strftime('%Y%m%d')
            
            stats = {
                "scheduled": len(appointments),
                "total_patients": len(unique_patients),
                "today": db.query(Appointment).filter(
                    Appointment.appointment_date == today_str
                ).count(),
                "this_week": db.query(Appointment).filter(
                    Appointment.appointment_date >= week_start_str,
                    Appointment.appointment_date <= week_end_str
                ).count()
            }
            
            # Formatar consultas - CONVERTER HÍFEN PARA BARRA NA EXIBIÇÃO
            formatted_appointments = []
            for apt in appointments:
                formatted_appointments.append({
                    "id": apt.id,
                    "patient_name": apt.patient_name,
                    "patient_phone": apt.patient_phone,
                    "patient_birth_date": apt.patient_birth_date,
                    "appointment_date": _format_appointment_date(apt.appointment_date),  # DD/MM/YYYY
                    "appointment_date_sortable": apt.appointment_date.replace('/', ''),  # DDMMYYYY para sort
                    "appointment_time": apt.appointment_time,  # String HH:MM
                    "consultation_type": apt.consultation_type,
                    "insurance_plan": apt.insurance_plan,
                    "status": apt.status.value,
                    "duration_minutes": apt.duration_minutes,
                    "notes": apt.notes,
                    "cancelled_at": apt.cancelled_at.isoformat() if apt.cancelled_at else None,
                    "cancelled_reason": apt.cancelled_reason,
                    "created_at": apt.created_at.isoformat(),
                    "updated_at": apt.updated_at.isoformat()
                })
            
            return {
                "stats": stats,
                "appointments": formatted_appointments
            }
            
    except Exception as e:
        logger.error(f"Erro ao buscar consultas agendadas: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/init-db")
@app.post("/admin/init-db")
async def init_database():
    """Força a criação das tabelas no banco de dados"""
    try:
        from app.database import init_db
        init_db()
        return {"message": "✅ Banco de dados inicializado com sucesso!", "status": "success"}
    except Exception as e:
        logger.error(f"Erro ao inicializar banco: {str(e)}")
        return {"message": f"❌ Erro ao inicializar banco: {str(e)}", "status": "error"}


@app.get("/admin/clean-db")
@app.post("/admin/clean-db")
async def clean_database():
    """Remove tabelas antigas e mantém apenas appointments"""
    try:
        from app.database import engine
        from sqlalchemy import text
        
        with engine.connect() as conn:
            # Remover tabelas antigas se existirem
            conn.execute(text("DROP TABLE IF EXISTS conversation_contexts CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS patients CASCADE"))
            conn.commit()
            
        return {
            "message": "✅ Banco limpo com sucesso! Apenas a tabela 'appointments' foi mantida.", 
            "status": "success"
        }
    except Exception as e:
        logger.error(f"Erro ao limpar banco: {str(e)}")
        return {"message": f"❌ Erro ao limpar banco: {str(e)}", "status": "error"}


@app.post("/admin/migrate-add-consultation-type")
async def migrate_add_consultation_type():
    """Endpoint para executar migração que adiciona coluna consultation_type"""
    try:
        from migrate_add_consultation_type import migrate_add_consultation_type
        
        result = migrate_add_consultation_type()
        
        if result.get("success"):
            return {"success": True, "message": result.get("message", "Migração executada com sucesso")}
        else:
            return {"success": False, "error": result.get("error", "Erro desconhecido")}
            
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/admin/migrate-add-insurance-plan")
async def migrate_add_insurance_plan():
    """Endpoint para executar migração que adiciona coluna insurance_plan"""
    try:
        from migrate_add_insurance_plan import migrate_add_insurance_plan
        
        result = migrate_add_insurance_plan()
        
        if result.get("success"):
            return {"success": True, "message": result.get("message", "Migração executada com sucesso")}
        else:
            return {"success": False, "error": result.get("error", "Erro desconhecido")}
            
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/admin/dashboard")
async def get_dashboard():
    """Dashboard com estatísticas gerais"""
    try:
        with get_db() as db:
            # Contadores
            total_appointments = db.query(Appointment).count()
            # Contar pacientes únicos baseado nas consultas
            unique_patients = set()
            for apt in db.query(Appointment).all():
                unique_patients.add(f"{apt.patient_name}_{apt.patient_birth_date}")
            total_patients = len(unique_patients)
            
            # Consultas por status
            appointments_by_status = {}
            for status in AppointmentStatus:
                count = db.query(Appointment).filter(Appointment.status == status).count()
                appointments_by_status[status.value] = count
            
            # Consultas recentes (últimos 7 dias)
            from datetime import datetime, timedelta
            week_ago = datetime.utcnow() - timedelta(days=7)
            recent_appointments = db.query(Appointment).filter(
                Appointment.created_at >= week_ago
            ).count()
            
            return {
                "summary": {
                    "total_patients": total_patients,
                    "total_appointments": total_appointments,
                    "recent_appointments": recent_appointments
                },
                "appointments_by_status": appointments_by_status
            }
    except Exception as e:
        logger.error(f"Erro ao buscar dashboard: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/dashboard")
async def dashboard():
    """Dashboard moderno para visualizar consultas agendadas"""
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dashboard - Consultas Agendadas</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        <style>
            /* ESTILOS MODERNOS E LIMPOS */
            
            :root {
                --primary: #4F46E5;
                --success: #10B981;
                --warning: #F59E0B;
                --danger: #EF4444;
                --bg: #F9FAFB;
                --card-bg: #FFFFFF;
                --text: #1F2937;
                --text-muted: #6B7280;
                --border: #E5E7EB;
            }
            
            body {
                background: var(--bg);
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                color: var(--text);
            }
            
            .dashboard-container {
                max-width: 1400px;
                margin: 0 auto;
                padding: 2rem;
            }
            
            /* Header */
            .header {
                background: var(--card-bg);
                border-radius: 16px;
                padding: 2rem;
                margin-bottom: 2rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            }
            
            .header h1 {
                font-size: 2rem;
                font-weight: 700;
                color: var(--primary);
                margin: 0;
            }
            
            /* Stats Cards */
            .stats-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 1.5rem;
                margin-bottom: 2rem;
            }
            
            .stat-card {
                background: var(--card-bg);
                border-radius: 12px;
                padding: 1.5rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
                display: flex;
                align-items: center;
                gap: 1rem;
                transition: transform 0.2s, box-shadow 0.2s;
            }
            
            .stat-card:hover {
                transform: translateY(-2px);
                box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            }
            
            .stat-icon {
                width: 48px;
                height: 48px;
                border-radius: 12px;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 1.5rem;
            }
            
            .stat-icon.primary { background: rgba(79, 70, 229, 0.1); color: var(--primary); }
            .stat-icon.success { background: rgba(16, 185, 129, 0.1); color: var(--success); }
            .stat-icon.warning { background: rgba(245, 158, 11, 0.1); color: var(--warning); }
            .stat-icon.danger { background: rgba(239, 68, 68, 0.1); color: var(--danger); }
            
            .stat-content h3 {
                font-size: 1.75rem;
                font-weight: 700;
                margin: 0;
                color: var(--text);
            }
            
            .stat-content p {
                margin: 0;
                color: var(--text-muted);
                font-size: 0.875rem;
            }
            
            /* Filters */
            .filters-bar {
                background: var(--card-bg);
                border-radius: 12px;
                padding: 1.5rem;
                margin-bottom: 2rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            }
            
            .search-input {
                border: 1px solid var(--border);
                border-radius: 8px;
                padding: 0.75rem 1rem;
                font-size: 0.95rem;
                transition: all 0.2s;
            }
            
            .search-input:focus {
                border-color: var(--primary);
                box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1);
                outline: none;
            }
            
            .btn-filter {
                padding: 0.75rem 1.25rem;
                border-radius: 8px;
                border: 1px solid var(--border);
                background: white;
                color: var(--text);
                transition: all 0.2s;
            }
            
            .btn-filter:hover, .btn-filter.active {
                background: var(--primary);
                color: white;
                border-color: var(--primary);
            }
            
            /* Date Group Header */
            .date-group {
                margin-bottom: 2rem;
            }
            
            .date-header {
                background: linear-gradient(135deg, var(--primary) 0%, #6366F1 100%);
                color: white;
                border-radius: 12px;
                padding: 1rem 1.5rem;
                margin-bottom: 1rem;
                display: flex;
                justify-content: space-between;
                align-items: center;
                box-shadow: 0 2px 8px rgba(79, 70, 229, 0.3);
            }
            
            .date-header h3 {
                margin: 0;
                font-size: 1.25rem;
                font-weight: 600;
            }
            
            .date-count {
                background: rgba(255,255,255,0.2);
                padding: 0.25rem 0.75rem;
                border-radius: 20px;
                font-size: 0.875rem;
            }
            
            /* Appointment Card */
            .appointment-card {
                background: var(--card-bg);
                border: 1px solid var(--border);
                border-radius: 12px;
                padding: 1.5rem;
                margin-bottom: 1rem;
                display: grid;
                grid-template-columns: 80px 1fr auto;
                gap: 1.5rem;
                align-items: center;
                transition: all 0.2s;
                box-shadow: 0 1px 3px rgba(0,0,0,0.05);
            }
            
            .appointment-card:hover {
                transform: translateX(4px);
                box-shadow: 0 4px 12px rgba(0,0,0,0.1);
                border-color: var(--primary);
            }
            
            .appointment-time {
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                text-align: center;
                padding: 1rem;
                background: linear-gradient(135deg, var(--primary) 0%, #6366F1 100%);
                border-radius: 10px;
                color: white;
            }
            
            .appointment-time .time {
                font-size: 1.5rem;
                font-weight: 700;
                line-height: 1;
            }
            
            .appointment-time .duration {
                font-size: 0.75rem;
                opacity: 0.9;
                margin-top: 0.25rem;
            }
            
            .appointment-info {
                display: flex;
                flex-direction: column;
                gap: 0.5rem;
            }
            
            .patient-name {
                font-size: 1.125rem;
                font-weight: 600;
                color: var(--text);
                margin: 0;
            }
            
            .patient-details {
                display: flex;
                gap: 1.5rem;
                flex-wrap: wrap;
                color: var(--text-muted);
                font-size: 0.875rem;
            }
            
            .patient-details i {
                margin-right: 0.25rem;
            }
            
            .appointment-badges {
                display: flex;
                flex-direction: column;
                gap: 0.5rem;
                align-items: flex-end;
            }
            
            .badge-custom {
                padding: 0.5rem 1rem;
                border-radius: 8px;
                font-size: 0.8125rem;
                font-weight: 600;
                white-space: nowrap;
            }
            
            .badge-type {
                background: rgba(79, 70, 229, 0.1);
                color: var(--primary);
            }
            
            .badge-insurance {
                background: rgba(16, 185, 129, 0.1);
                color: var(--success);
            }
            
            .badge-status-agendada {
                background: rgba(16, 185, 129, 0.1);
                color: var(--success);
            }
            
            /* No Appointments */
            .no-appointments {
                text-align: center;
                padding: 4rem 2rem;
                background: var(--card-bg);
                border-radius: 12px;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            }
            
            .no-appointments i {
                font-size: 4rem;
                color: var(--text-muted);
                margin-bottom: 1rem;
            }
            
            /* Loading */
            .loading {
                text-align: center;
                padding: 4rem 2rem;
                color: var(--text-muted);
            }
            
            /* Responsive */
            @media (max-width: 768px) {
                .appointment-card {
                    grid-template-columns: 1fr;
                    text-align: center;
                }
                
                .appointment-badges {
                    align-items: center;
                }
                
                .patient-details {
                    justify-content: center;
                }
            }
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            <!-- Header -->
            <div class="header">
                <h1><i class="fas fa-calendar-check"></i> Dashboard - Consultas Agendadas</h1>
                <p class="text-muted mb-0">Consultório Dra. Rose</p>
            </div>

            <!-- Estatísticas -->
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-icon primary">
                        <i class="fas fa-calendar-alt"></i>
                    </div>
                    <div class="stat-content">
                        <h3 id="total-scheduled">-</h3>
                        <p>Consultas Agendadas</p>
                    </div>
                </div>
                <div class="stat-card">
                    <div class="stat-icon success">
                        <i class="fas fa-users"></i>
                    </div>
                    <div class="stat-content">
                        <h3 id="total-patients">-</h3>
                        <p>Total de Pacientes</p>
                    </div>
                </div>
                <div class="stat-card">
                    <div class="stat-icon warning">
                        <i class="fas fa-calendar-day"></i>
                    </div>
                    <div class="stat-content">
                        <h3 id="today-appointments">-</h3>
                        <p>Consultas Hoje</p>
                    </div>
                </div>
                <div class="stat-card">
                    <div class="stat-icon danger">
                        <i class="fas fa-calendar-week"></i>
                    </div>
                    <div class="stat-content">
                        <h3 id="week-appointments">-</h3>
                        <p>Esta Semana</p>
                    </div>
                </div>
            </div>

            <!-- Filtros -->
            <div class="filters-bar">
                <div class="row align-items-center">
                    <div class="col-md-4">
                        <input type="text" class="form-control search-input" id="searchInput" placeholder="🔍 Buscar por nome do paciente...">
                    </div>
                    <div class="col-md-2">
                        <select class="form-select" id="typeFilter">
                            <option value="">Todos os tipos</option>
                            <option value="clinica_geral">Clínica Geral</option>
                            <option value="geriatria">Geriatria</option>
                            <option value="domiciliar">Domiciliar</option>
                        </select>
                    </div>
                    <div class="col-md-2">
                        <select class="form-select" id="insuranceFilter">
                            <option value="">Todos os convênios</option>
                            <option value="IPE">IPE</option>
                            <option value="CABERGS">CABERGS</option>
                            <option value="particular">Particular</option>
                        </select>
                    </div>
                    <div class="col-md-2">
                        <select class="form-select" id="statusFilter">
                            <option value="">Todos os status</option>
                            <option value="agendada">Agendada</option>
                            <option value="realizada">Realizada</option>
                            <option value="cancelada">Cancelada</option>
                        </select>
                    </div>
                    <div class="col-md-2">
                        <button class="btn btn-primary w-100" onclick="loadAppointments()">
                            <i class="fas fa-sync-alt"></i> Atualizar
                        </button>
                    </div>
                </div>
                <div class="mt-2">
                    <small class="text-muted">
                        Última atualização: <span id="last-update">-</span>
                    </small>
                </div>
            </div>

            <!-- Lista de Consultas -->
            <div id="appointments-container">
                <div class="loading">
                    <i class="fas fa-spinner fa-spin fa-2x"></i>
                    <p>Carregando consultas...</p>
                </div>
            </div>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
        <script>
            let allAppointments = [];
            let autoRefreshInterval;

            // Carregar dados ao abrir a página
            document.addEventListener('DOMContentLoaded', function() {
                loadAppointments();
                setupFilters();
                startAutoRefresh();
            });

            function setupFilters() {
                document.getElementById('searchInput').addEventListener('input', filterAppointments);
                document.getElementById('typeFilter').addEventListener('change', filterAppointments);
                document.getElementById('insuranceFilter').addEventListener('change', filterAppointments);
                document.getElementById('statusFilter').addEventListener('change', filterAppointments);
            }

            function startAutoRefresh() {
                autoRefreshInterval = setInterval(loadAppointments, 30000); // 30 segundos
            }

            async function loadAppointments() {
                try {
                    // Mostrar loading
                    document.getElementById('appointments-container').innerHTML = `
                        <div class="loading">
                            <i class="fas fa-spinner fa-spin fa-2x"></i>
                            <p>Carregando consultas...</p>
                        </div>
                    `;

                    // Buscar consultas
                    const response = await fetch('/api/appointments/scheduled');
                    const data = await response.json();

                    // Armazenar todas as consultas
                    allAppointments = data.appointments || [];

                    // Atualizar estatísticas
                    updateStats(data.stats);

                    // Atualizar lista de consultas
                    filterAppointments();

                    // Atualizar timestamp
                    document.getElementById('last-update').textContent = new Date().toLocaleString('pt-BR');

                } catch (error) {
                    console.error('Erro ao carregar consultas:', error);
                    document.getElementById('appointments-container').innerHTML = `
                        <div class="alert alert-danger">
                            <i class="fas fa-exclamation-triangle"></i>
                            Erro ao carregar consultas. Tente novamente.
                        </div>
                    `;
                }
            }

            function updateStats(stats) {
                document.getElementById('total-scheduled').textContent = stats.scheduled || 0;
                document.getElementById('total-patients').textContent = stats.total_patients || 0;
                document.getElementById('today-appointments').textContent = stats.today || 0;
                document.getElementById('week-appointments').textContent = stats.this_week || 0;
            }

            function filterAppointments() {
                const searchTerm = document.getElementById('searchInput').value.toLowerCase();
                const typeFilter = document.getElementById('typeFilter').value;
                const insuranceFilter = document.getElementById('insuranceFilter').value;
                const statusFilter = document.getElementById('statusFilter').value;

                let filteredAppointments = allAppointments.filter(appointment => {
                    const matchesSearch = !searchTerm || appointment.patient_name.toLowerCase().includes(searchTerm);
                    const matchesType = !typeFilter || appointment.consultation_type === typeFilter;
                    const matchesInsurance = !insuranceFilter || appointment.insurance_plan === insuranceFilter;
                    const matchesStatus = !statusFilter || appointment.status === statusFilter;

                    return matchesSearch && matchesType && matchesInsurance && matchesStatus;
                });

                displayAppointments(filteredAppointments);
            }

            function displayAppointments(appointments) {
                const container = document.getElementById('appointments-container');
                
                if (!appointments || appointments.length === 0) {
                    container.innerHTML = `
                        <div class="no-appointments">
                            <i class="fas fa-calendar-times fa-3x mb-3"></i>
                            <h4>Nenhuma consulta encontrada</h4>
                            <p>As consultas agendadas aparecerão aqui.</p>
                        </div>
                    `;
                    return;
                }

                // Agrupar por data
                const groupedAppointments = groupAppointmentsByDate(appointments);
                
                let html = '';
                for (const [date, appointmentsForDate] of Object.entries(groupedAppointments)) {
                    html += `
                        <div class="date-group">
                            <div class="date-header">
                                <h3>${formatDateHeader(date)}</h3>
                                <span class="date-count">${appointmentsForDate.length} consulta${appointmentsForDate.length !== 1 ? 's' : ''}</span>
                            </div>
                            ${appointmentsForDate.map(appointment => renderAppointmentCard(appointment)).join('')}
                        </div>
                    `;
                }

                container.innerHTML = html;
            }

            function groupAppointmentsByDate(appointments) {
                const groups = {};
                
                appointments.forEach(appointment => {
                    const date = appointment.appointment_date;
                    if (!groups[date]) {
                        groups[date] = [];
                    }
                    groups[date].push(appointment);
                });

                // Ordenar datas (mais próxima primeiro - crescente)
                const sortedDates = Object.keys(groups).sort((a, b) => {
                    // Converter DD/MM/YYYY para YYYYMMDD para ordenação correta
                    const dateA = a.split('/').reverse().join('');
                    const dateB = b.split('/').reverse().join('');
                    return dateA.localeCompare(dateB);
                });

                const sortedGroups = {};
                sortedDates.forEach(date => {
                    sortedGroups[date] = groups[date];
                });

                return sortedGroups;
            }

            function renderAppointmentCard(appointment) {
                return `
                    <div class="appointment-card">
                        <div class="appointment-time">
                            <div class="time">${formatTime(appointment.appointment_time)}</div>
                            <div class="duration">${appointment.duration_minutes}min</div>
                        </div>
                        <div class="appointment-info">
                            <h4 class="patient-name">${appointment.patient_name}</h4>
                            <div class="patient-details">
                                <span><i class="fas fa-phone"></i> ${appointment.patient_phone}</span>
                                <span><i class="fas fa-birthday-cake"></i> ${appointment.patient_birth_date}</span>
                            </div>
                        </div>
                        <div class="appointment-badges">
                            <span class="badge-custom badge-type">${getConsultationTypeText(appointment.consultation_type)}</span>
                            <span class="badge-custom badge-insurance">${getInsurancePlanText(appointment.insurance_plan)}</span>
                            <span class="badge-custom badge-status-${appointment.status}">${getStatusText(appointment.status)}</span>
                        </div>
                    </div>
                `;
            }

            function formatTime(timeStr) {
                return timeStr.substring(0, 5); // HH:MM
            }

            function formatDateHeader(dateStr) {
                const parts = dateStr.split('/');
                if (parts.length === 3) {
                    const [day, month, year] = parts;
                    const date = new Date(parseInt(year), parseInt(month) - 1, parseInt(day));
                    
                    if (!isNaN(date.getTime())) {
                        return date.toLocaleDateString('pt-BR', {
                            weekday: 'long',
                            day: '2-digit',
                            month: 'long',
                            year: 'numeric'
                        });
                    }
                }
                return dateStr;
            }

            function getConsultationTypeText(type) {
                const typeMap = {
                    'clinica_geral': 'Clínica Geral',
                    'geriatria': 'Geriatria',
                    'domiciliar': 'Domiciliar'
                };
                return typeMap[type] || 'Clínica Geral';
            }

            function getInsurancePlanText(plan) {
                const planMap = {
                    'CABERGS': 'CABERGS',
                    'IPE': 'IPE',
                    'particular': 'Particular'
                };
                return planMap[plan] || 'Particular';
            }

            function getStatusText(status) {
                const statusMap = {
                    'agendada': 'Agendada',
                    'realizada': 'Realizada',
                    'cancelada': 'Cancelada'
                };
                return statusMap[status] || status;
            }
        </script>
    </body>
    </html>
    """)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)