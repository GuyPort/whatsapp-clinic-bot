"""
Aplicação FastAPI principal com webhooks do WhatsApp.
"""
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from contextlib import asynccontextmanager
import logging
import secrets
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

# Sistema de autenticação HTTP Basic Auth
security = HTTPBasic()

def verify_admin_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    """
    Verifica as credenciais de admin usando HTTP Basic Auth.
    Username pode ser qualquer coisa, apenas a senha é verificada.
    """
    correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"),
        settings.admin_password.encode("utf8")
    )

    if not correct_password:
        raise HTTPException(
            status_code=401,
            detail="Credenciais inválidas",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


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
        remote_jid = key.get('remoteJid', '')
        
        # Extrair texto da mensagem (antes de tratar fromMe)
        message_text = None
        if 'conversation' in message_data:
            message_text = message_data['conversation']
        elif 'extendedTextMessage' in message_data:
            message_text = message_data['extendedTextMessage'].get('text', '')
        elif 'imageMessage' in message_data:
            message_text = message_data['imageMessage'].get('caption', '')
        
        is_from_me = key.get('fromMe', False)
        
        # Tratar comando /pause da secretária (mensagens enviadas pelo número da clínica)
        if is_from_me:
            lowered = (message_text or '').strip().lower()
            if lowered in {"/pausar", "/pause"} and remote_jid and '@newsletter' not in remote_jid and '@g.us' not in remote_jid:
                patient_phone = remote_jid.replace('@s.whatsapp.net', '')
                if patient_phone:
                    logger.info(f"⏸️ Comando /pause recebido da secretária para {patient_phone}")
                    with get_db() as db:
                        ai_agent._handle_secretary_pause(db, patient_phone)
                    return {"status": "processed", "action": "secretary_pause", "patient": patient_phone}
            # Outras mensagens enviadas por nós devem ser ignoradas
            return {"status": "ignored", "reason": "message from bot"}
        
        # Extrair informações
        phone = remote_jid
        
        # Ignorar mensagens de newsletter e grupos
        if '@newsletter' in phone or '@g.us' in phone:
            logger.info(f"Ignorando mensagem de newsletter/grupo: {phone}")
            return {"status": "ignored", "reason": "newsletter or group message"}
        
        phone = phone.replace('@s.whatsapp.net', '')
        
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
        
        # Verificar comandos administrativos (/pausar)
        lowered = message_text.strip().lower()

        if lowered in {"/pausar", "/pause"}:
            with get_db() as db:
                logger.info(f"⏸️ Comando /pausar recebido para {phone}")
                response = ai_agent._handle_request_human_assistance({}, db, phone)
                if response:
                    send_message_task.delay(phone, response)
                return

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
async def reload_config(admin: str = Depends(verify_admin_credentials)):
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
async def get_patients(admin: str = Depends(verify_admin_credentials)):
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
async def get_appointments(admin: str = Depends(verify_admin_credentials)):
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
async def init_database(admin: str = Depends(verify_admin_credentials)):
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
async def clean_database(admin: str = Depends(verify_admin_credentials)):
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
async def migrate_add_consultation_type(admin: str = Depends(verify_admin_credentials)):
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
async def migrate_add_insurance_plan(admin: str = Depends(verify_admin_credentials)):
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
async def get_dashboard(admin: str = Depends(verify_admin_credentials)):
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


# ==================== ENDPOINTS DE MANIPULAÇÃO DE CONSULTAS ====================

@app.delete("/admin/appointments/{appointment_id}")
async def delete_appointment_admin(
    appointment_id: int,
    admin: str = Depends(verify_admin_credentials)
):
    """Deleta uma consulta permanentemente do banco de dados"""
    try:
        with get_db() as db:
            appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()

            if not appointment:
                raise HTTPException(status_code=404, detail="Consulta não encontrada")

            # Log antes de deletar
            logger.info(f"Admin {admin} deletou consulta #{appointment_id}: {appointment.patient_name} - {appointment.appointment_date} {appointment.appointment_time}")

            # Deletar do banco
            db.delete(appointment)
            db.commit()

            return {
                "success": True,
                "message": "Consulta deletada com sucesso"
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao deletar consulta: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/appointments/{appointment_id}/mark-attended")
async def mark_attended_appointment_admin(
    appointment_id: int,
    admin: str = Depends(verify_admin_credentials)
):
    """Marca que o paciente compareceu à consulta"""
    try:
        with get_db() as db:
            appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()

            if not appointment:
                raise HTTPException(status_code=404, detail="Consulta não encontrada")

            if appointment.status == AppointmentStatus.COMPARECEU:
                raise HTTPException(status_code=400, detail="Esta consulta já foi marcada como compareceu")

            # Marcar como compareceu
            appointment.status = AppointmentStatus.COMPARECEU

            db.commit()

            logger.info(f"Admin {admin} marcou consulta #{appointment_id} como compareceu: {appointment.patient_name}")

            return {
                "success": True,
                "message": "Paciente marcado como compareceu",
                "appointment": {
                    "id": appointment.id,
                    "patient_name": appointment.patient_name,
                    "status": appointment.status.value
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao marcar presença: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/appointments/{appointment_id}/mark-missed")
async def mark_missed_appointment_admin(
    appointment_id: int,
    admin: str = Depends(verify_admin_credentials)
):
    """Marca que o paciente não compareceu à consulta (faltou)"""
    try:
        with get_db() as db:
            appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()

            if not appointment:
                raise HTTPException(status_code=404, detail="Consulta não encontrada")

            if appointment.status == AppointmentStatus.NAO_COMPARECEU:
                raise HTTPException(status_code=400, detail="Esta consulta já foi marcada como não compareceu")

            # Marcar como não compareceu
            appointment.status = AppointmentStatus.NAO_COMPARECEU

            db.commit()

            logger.info(f"Admin {admin} marcou consulta #{appointment_id} como não compareceu (falta): {appointment.patient_name}")

            return {
                "success": True,
                "message": "Paciente marcado como faltou",
                "appointment": {
                    "id": appointment.id,
                    "patient_name": appointment.patient_name,
                    "status": appointment.status.value
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao marcar falta: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/appointments/{appointment_id}/reschedule")
async def reschedule_appointment_admin(
    appointment_id: int,
    request: Request,
    admin: str = Depends(verify_admin_credentials)
):
    """Remarca uma consulta para nova data/hora"""
    try:
        body = await request.json()
        new_date = body.get("new_date")  # Formato: YYYYMMDD
        new_time = body.get("new_time")  # Formato: HH:MM

        if not new_date or not new_time:
            raise HTTPException(status_code=400, detail="Nova data e hora são obrigatórias")

        with get_db() as db:
            appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()

            if not appointment:
                raise HTTPException(status_code=404, detail="Consulta não encontrada")

            # Validar disponibilidade do novo horário
            from app.appointment_rules import AppointmentRules
            from app.utils import load_clinic_info, parse_appointment_datetime

            clinic_info = load_clinic_info()
            rules = AppointmentRules(clinic_info)

            # Verificar se o horário é válido
            new_datetime = parse_appointment_datetime(new_date, new_time)

            if not rules.is_valid_appointment_date(new_datetime):
                raise HTTPException(status_code=400, detail="Data/hora inválida (fora do horário de funcionamento ou no passado)")

            # Verificar disponibilidade (excluindo a própria consulta sendo remarcada)
            duration = appointment.duration_minutes or 60
            if not rules.check_slot_availability(new_datetime, duration, db, exclude_appointment_id=appointment_id):
                raise HTTPException(status_code=400, detail="Horário já está ocupado")

            # Verificar regras de convênio
            insurance_plan = appointment.insurance_plan or "particular"
            if not rules.is_plan_allowed_on_date(new_datetime, insurance_plan):
                raise HTTPException(status_code=400, detail=f"Convênio {insurance_plan} não permitido nesta data")

            if not rules.has_capacity_for_insurance(new_datetime, insurance_plan, db, exclude_appointment_id=appointment_id):
                raise HTTPException(status_code=400, detail=f"Capacidade diária para {insurance_plan} atingida")

            # Atualizar consulta
            appointment.appointment_date = new_date
            appointment.appointment_time = new_time
            appointment.status = AppointmentStatus.AGENDADA  # Reset status se estava como realizada

            db.commit()

            logger.info(f"Admin {admin} remarcou consulta #{appointment_id} para {new_date} {new_time}")

            return {
                "success": True,
                "message": "Consulta remarcada com sucesso",
                "appointment": {
                    "id": appointment.id,
                    "patient_name": appointment.patient_name,
                    "appointment_date": new_date,
                    "appointment_time": new_time,
                    "status": appointment.status.value
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao remarcar consulta: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/admin/appointments/{appointment_id}")
async def update_appointment_admin(
    appointment_id: int,
    request: Request,
    admin: str = Depends(verify_admin_credentials)
):
    """Atualiza detalhes de uma consulta (nome, telefone, tipo, etc)"""
    try:
        body = await request.json()

        with get_db() as db:
            appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()

            if not appointment:
                raise HTTPException(status_code=404, detail="Consulta não encontrada")

            # Atualizar campos permitidos
            if "patient_name" in body:
                appointment.patient_name = body["patient_name"]

            if "patient_phone" in body:
                appointment.patient_phone = body["patient_phone"]

            if "patient_birth_date" in body:
                appointment.patient_birth_date = body["patient_birth_date"]

            if "consultation_type" in body:
                appointment.consultation_type = body["consultation_type"]

            if "insurance_plan" in body:
                appointment.insurance_plan = body["insurance_plan"]

            db.commit()

            logger.info(f"Admin {admin} atualizou consulta #{appointment_id}")

            return {
                "success": True,
                "message": "Consulta atualizada com sucesso",
                "appointment": {
                    "id": appointment.id,
                    "patient_name": appointment.patient_name,
                    "patient_phone": appointment.patient_phone,
                    "consultation_type": appointment.consultation_type,
                    "insurance_plan": appointment.insurance_plan
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao atualizar consulta: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/appointments")
async def create_appointment_admin(
    request: Request,
    admin: str = Depends(verify_admin_credentials)
):
    """Cria uma nova consulta via painel admin"""
    try:
        body = await request.json()

        # Validar campos obrigatórios
        required_fields = ["patient_name", "patient_phone", "patient_birth_date",
                          "appointment_date", "appointment_time", "consultation_type", "insurance_plan"]

        for field in required_fields:
            if not body.get(field):
                raise HTTPException(status_code=400, detail=f"Campo obrigatório: {field}")

        # Validar e normalizar dados
        from app.utils import normalize_phone, load_clinic_info, parse_appointment_datetime
        from app.appointment_rules import AppointmentRules

        patient_phone = normalize_phone(body["patient_phone"])
        appointment_date = body["appointment_date"]  # YYYYMMDD
        appointment_time = body["appointment_time"]  # HH:MM
        duration_minutes = body.get("duration_minutes", 60)

        with get_db() as db:
            # Validar disponibilidade
            clinic_info = load_clinic_info()
            rules = AppointmentRules(clinic_info)

            appointment_datetime = parse_appointment_datetime(appointment_date, appointment_time)

            if not rules.is_valid_appointment_date(appointment_datetime):
                raise HTTPException(status_code=400, detail="Data/hora inválida (fora do horário de funcionamento ou no passado)")

            if not rules.check_slot_availability(appointment_datetime, duration_minutes, db):
                raise HTTPException(status_code=400, detail="Horário já está ocupado")

            insurance_plan = body["insurance_plan"]
            if not rules.is_plan_allowed_on_date(appointment_datetime, insurance_plan):
                raise HTTPException(status_code=400, detail=f"Convênio {insurance_plan} não permitido nesta data")

            if not rules.has_capacity_for_insurance(appointment_datetime, insurance_plan, db):
                raise HTTPException(status_code=400, detail=f"Capacidade diária para {insurance_plan} atingida")

            # Criar consulta
            new_appointment = Appointment(
                patient_name=body["patient_name"],
                patient_phone=patient_phone,
                patient_birth_date=body["patient_birth_date"],
                appointment_date=appointment_date,
                appointment_time=appointment_time,
                duration_minutes=duration_minutes,
                consultation_type=body["consultation_type"],
                insurance_plan=insurance_plan,
                status=AppointmentStatus.AGENDADA
            )

            db.add(new_appointment)
            db.commit()
            db.refresh(new_appointment)

            logger.info(f"Admin {admin} criou nova consulta #{new_appointment.id}: {new_appointment.patient_name} - {appointment_date} {appointment_time}")

            return {
                "success": True,
                "message": "Consulta criada com sucesso",
                "appointment": {
                    "id": new_appointment.id,
                    "patient_name": new_appointment.patient_name,
                    "patient_phone": new_appointment.patient_phone,
                    "appointment_date": new_appointment.appointment_date,
                    "appointment_time": new_appointment.appointment_time,
                    "consultation_type": new_appointment.consultation_type,
                    "insurance_plan": new_appointment.insurance_plan,
                    "status": new_appointment.status.value
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao criar consulta: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/migrate-status")
async def migrate_appointment_status(admin: str = Depends(verify_admin_credentials)):
    """
    Migração única para atualizar status antigos:
    - Deleta consultas com status 'cancelada'
    - Converte 'realizada' para 'compareceu'
    """
    try:
        from sqlalchemy import text
        with get_db() as db:
            # Contar registros antes
            total_count = db.query(Appointment).count()
            canceled_count = db.query(Appointment).filter(
                Appointment.status == 'cancelada'
            ).count()
            realizada_count = db.query(Appointment).filter(
                Appointment.status == 'realizada'
            ).count()

            # Deletar consultas canceladas
            db.query(Appointment).filter(
                Appointment.status == 'cancelada'
            ).delete()

            # Converter realizada para compareceu
            # Precisamos usar raw SQL porque o enum mudou
            db.execute(
                text("UPDATE appointments SET status = 'compareceu' WHERE status = 'realizada'")
            )

            db.commit()

            logger.info(f"Migração concluída: {canceled_count} canceladas deletadas, {realizada_count} convertidas para compareceu")

            return {
                "success": True,
                "message": "Migração concluída com sucesso",
                "stats": {
                    "total_before": total_count,
                    "canceled_deleted": canceled_count,
                    "realizada_converted": realizada_count,
                    "total_after": total_count - canceled_count
                }
            }

    except Exception as e:
        logger.error(f"Erro na migração: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/dashboard")
async def dashboard(admin: str = Depends(verify_admin_credentials)):
    """Dashboard moderno para visualizar consultas agendadas"""
    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dashboard - Consultas Agendadas</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
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
                grid-template-columns: 80px 1fr auto auto;
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

            .appointment-actions {
                display: flex;
                gap: 0.5rem;
                flex-direction: column;
            }

            .appointment-actions .btn {
                padding: 0.4rem 0.8rem;
                font-size: 0.85rem;
                white-space: nowrap;
            }

            .appointment-actions .btn i {
                margin-right: 0.3rem;
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
            <div class="header" style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h1><i class="fas fa-calendar-check"></i> Dashboard - Consultas Agendadas</h1>
                    <p class="text-muted mb-0">Consultório Dra. Rose</p>
                </div>
                <button class="btn btn-primary" onclick="openCreateModal()" style="height: fit-content;">
                    <i class="fas fa-plus"></i> Criar Consulta
                </button>
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
                const actionButtons = getActionButtons(appointment);
                return `
                    <div class="appointment-card" data-id="${appointment.id}">
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
                        <div class="appointment-actions">
                            ${actionButtons}
                        </div>
                    </div>
                `;
            }

            function getActionButtons(appointment) {
                const buttons = [];

                if (appointment.status === 'agendada') {
                    buttons.push(`
                        <button class="btn btn-sm btn-success" onclick="markAttended(${appointment.id})">
                            <i class="fas fa-check"></i> Paciente Compareceu
                        </button>
                    `);
                    buttons.push(`
                        <button class="btn btn-sm btn-warning" onclick="markMissed(${appointment.id})">
                            <i class="fas fa-user-times"></i> Paciente Faltou
                        </button>
                    `);
                    buttons.push(`
                        <button class="btn btn-sm btn-primary" onclick="openRescheduleModal(${appointment.id}, '${appointment.appointment_date}', '${appointment.appointment_time}')">
                            <i class="fas fa-calendar-alt"></i> Remarcar
                        </button>
                    `);
                    buttons.push(`
                        <button class="btn btn-sm btn-danger" onclick="deleteAppointment(${appointment.id}, '${appointment.patient_name}')">
                            <i class="fas fa-trash"></i> Deletar
                        </button>
                    `);
                }

                return buttons.join('');
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
                    'compareceu': 'Compareceu',
                    'nao_compareceu': 'Não Compareceu'
                };
                return statusMap[status] || status;
            }

            // ========== FUNÇÕES DE AÇÃO ==========

            async function deleteAppointment(id, patientName) {
                const result = await Swal.fire({
                    title: 'Deletar Consulta?',
                    html: `<p>Tem certeza que deseja <strong>deletar permanentemente</strong> a consulta de <strong>${patientName}</strong>?</p><p class="text-danger">Esta ação não pode ser desfeita!</p>`,
                    icon: 'warning',
                    showCancelButton: true,
                    confirmButtonColor: '#EF4444',
                    cancelButtonColor: '#6B7280',
                    confirmButtonText: 'Sim, deletar',
                    cancelButtonText: 'Não'
                });

                if (result.isConfirmed) {
                    try {
                        const response = await fetch(`/admin/appointments/${id}`, {
                            method: 'DELETE',
                            headers: { 'Content-Type': 'application/json' }
                        });

                        const data = await response.json();

                        if (response.ok) {
                            Swal.fire('Deletada!', 'Consulta deletada com sucesso.', 'success');
                            loadAppointments();
                        } else {
                            throw new Error(data.detail || 'Erro ao deletar consulta');
                        }
                    } catch (error) {
                        Swal.fire('Erro!', error.message, 'error');
                    }
                }
            }

            async function markAttended(id) {
                const result = await Swal.fire({
                    title: 'Paciente Compareceu?',
                    text: 'Marcar que o paciente compareceu à consulta.',
                    icon: 'question',
                    showCancelButton: true,
                    confirmButtonColor: '#10B981',
                    cancelButtonColor: '#6B7280',
                    confirmButtonText: 'Sim, compareceu',
                    cancelButtonText: 'Cancelar'
                });

                if (result.isConfirmed) {
                    try {
                        const response = await fetch(`/admin/appointments/${id}/mark-attended`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' }
                        });

                        const data = await response.json();

                        if (response.ok) {
                            Swal.fire('Concluída!', 'Consulta marcada como realizada.', 'success');
                            loadAppointments(); // Recarregar lista
                        } else {
                            throw new Error(data.detail || 'Erro ao concluir consulta');
                        }
                    } catch (error) {
                        Swal.fire('Erro!', error.message, 'error');
                    }
                }
            }

            async function markMissed(id) {
                const result = await Swal.fire({
                    title: 'Paciente Faltou?',
                    text: 'Marcar que o paciente NÃO compareceu à consulta.',
                    icon: 'warning',
                    showCancelButton: true,
                    confirmButtonColor: '#F59E0B',
                    cancelButtonColor: '#6B7280',
                    confirmButtonText: 'Sim, faltou',
                    cancelButtonText: 'Cancelar'
                });

                if (result.isConfirmed) {
                    try {
                        const response = await fetch(`/admin/appointments/${id}/mark-missed`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' }
                        });

                        const data = await response.json();

                        if (response.ok) {
                            Swal.fire('Registrado!', 'Falta do paciente registrada.', 'success');
                            loadAppointments();
                        } else {
                            throw new Error(data.detail || 'Erro ao marcar falta');
                        }
                    } catch (error) {
                        Swal.fire('Erro!', error.message, 'error');
                    }
                }
            }

            async function openRescheduleModal(id, currentDate, currentTime) {
                const { value: formValues } = await Swal.fire({
                    title: 'Remarcar Consulta',
                    html: `
                        <div style="text-align: left;">
                            <label style="display: block; margin-bottom: 5px; font-weight: 500;">Nova Data</label>
                            <input type="date" id="new-date" class="swal2-input" style="margin-top: 0;">
                            <label style="display: block; margin-top: 15px; margin-bottom: 5px; font-weight: 500;">Novo Horário</label>
                            <input type="time" id="new-time" class="swal2-input" style="margin-top: 0;" step="3600">
                        </div>
                    `,
                    focusConfirm: false,
                    showCancelButton: true,
                    confirmButtonColor: '#4F46E5',
                    cancelButtonColor: '#6B7280',
                    confirmButtonText: 'Remarcar',
                    cancelButtonText: 'Cancelar',
                    preConfirm: () => {
                        const newDate = document.getElementById('new-date').value;
                        const newTime = document.getElementById('new-time').value;

                        if (!newDate || !newTime) {
                            Swal.showValidationMessage('Por favor, preencha data e horário');
                            return false;
                        }

                        return { newDate, newTime };
                    }
                });

                if (formValues) {
                    await rescheduleAppointment(id, formValues.newDate, formValues.newTime);
                }
            }

            async function rescheduleAppointment(id, newDate, newTime) {
                try {
                    // Converter data de YYYY-MM-DD para YYYYMMDD
                    const dateFormatted = newDate.replace(/-/g, '');

                    const response = await fetch(`/admin/appointments/${id}/reschedule`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            new_date: dateFormatted,
                            new_time: newTime
                        })
                    });

                    const data = await response.json();

                    if (response.ok) {
                        Swal.fire('Remarcada!', 'Consulta remarcada com sucesso.', 'success');
                        loadAppointments(); // Recarregar lista
                    } else {
                        throw new Error(data.detail || 'Erro ao remarcar consulta');
                    }
                } catch (error) {
                    Swal.fire('Erro!', error.message, 'error');
                }
            }

            async function openCreateModal() {
                const { value: formValues } = await Swal.fire({
                    title: 'Criar Nova Consulta',
                    html: `
                        <div style="text-align: left;">
                            <label style="display: block; margin-bottom: 5px; font-weight: 500;">Nome do Paciente</label>
                            <input type="text" id="patient-name" class="swal2-input" placeholder="Nome completo" style="margin-top: 0;">

                            <label style="display: block; margin-top: 15px; margin-bottom: 5px; font-weight: 500;">Telefone</label>
                            <input type="tel" id="patient-phone" class="swal2-input" placeholder="+55 (51) 99999-9999" style="margin-top: 0;" value="+55 ">

                            <label style="display: block; margin-top: 15px; margin-bottom: 5px; font-weight: 500;">Data de Nascimento</label>
                            <input type="text" id="patient-birth" class="swal2-input" placeholder="DD/MM/AAAA" style="margin-top: 0;" maxlength="10">

                            <label style="display: block; margin-top: 15px; margin-bottom: 5px; font-weight: 500;">Convênio</label>
                            <select id="insurance-plan" class="swal2-input" style="margin-top: 0;">
                                <option value="">Selecione...</option>
                                <option value="CABERGS">CABERGS</option>
                                <option value="IPE">IPE</option>
                                <option value="particular">Particular</option>
                            </select>

                            <label style="display: block; margin-top: 15px; margin-bottom: 5px; font-weight: 500;">Tipo de Consulta</label>
                            <select id="consultation-type" class="swal2-input" style="margin-top: 0;">
                                <option value="">Selecione...</option>
                                <option value="clinica_geral">Clínica Geral</option>
                                <option value="geriatria">Geriatria</option>
                                <option value="domiciliar">Atendimento Domiciliar</option>
                            </select>

                            <label style="display: block; margin-top: 15px; margin-bottom: 5px; font-weight: 500;">Data da Consulta</label>
                            <input type="date" id="appointment-date" class="swal2-input" style="margin-top: 0;">

                            <label style="display: block; margin-top: 15px; margin-bottom: 5px; font-weight: 500;">Horário</label>
                            <input type="time" id="appointment-time" class="swal2-input" style="margin-top: 0;" step="3600">
                        </div>
                    `,
                    focusConfirm: false,
                    showCancelButton: true,
                    confirmButtonColor: '#4F46E5',
                    cancelButtonColor: '#6B7280',
                    confirmButtonText: 'Criar Consulta',
                    cancelButtonText: 'Cancelar',
                    width: '600px',
                    didOpen: () => {
                        // Formatar telefone automaticamente
                        const phoneInput = document.getElementById('patient-phone');
                        phoneInput.addEventListener('input', (e) => {
                            let value = e.target.value;

                            // Remove tudo exceto números
                            let numbers = value.replace(/\D/g, '');

                            // Se o usuário tentar apagar o +55, restaura
                            if (!value.startsWith('+55')) {
                                value = '+55 ' + numbers;
                                numbers = value.replace(/\D/g, '');
                            }

                            // Remove o código do país (55) dos números para formatar o resto
                            if (numbers.startsWith('55')) {
                                numbers = numbers.substring(2);
                            }

                            // Formata como +55 (XX) XXXXX-XXXX
                            let formatted = '+55 ';
                            if (numbers.length > 0) {
                                formatted += '(' + numbers.substring(0, 2);
                            }
                            if (numbers.length >= 2) {
                                formatted += ') ' + numbers.substring(2, 7);
                            }
                            if (numbers.length >= 7) {
                                formatted += '-' + numbers.substring(7, 11);
                            }

                            e.target.value = formatted;
                        });

                        // Impedir que o usuário delete o +55
                        phoneInput.addEventListener('keydown', (e) => {
                            if ((e.key === 'Backspace' || e.key === 'Delete') && e.target.selectionStart <= 4) {
                                e.preventDefault();
                            }
                        });

                        // Formatar data de nascimento automaticamente
                        const birthInput = document.getElementById('patient-birth');
                        birthInput.addEventListener('input', (e) => {
                            let value = e.target.value;

                            // Remove tudo exceto números
                            let numbers = value.replace(/\D/g, '');

                            // Limita a 8 dígitos (DDMMAAAA)
                            numbers = numbers.substring(0, 8);

                            // Formata como DD/MM/AAAA
                            let formatted = '';
                            if (numbers.length > 0) {
                                formatted = numbers.substring(0, 2);
                            }
                            if (numbers.length >= 3) {
                                formatted += '/' + numbers.substring(2, 4);
                            }
                            if (numbers.length >= 5) {
                                formatted += '/' + numbers.substring(4, 8);
                            }

                            e.target.value = formatted;
                        });
                    },
                    preConfirm: () => {
                        const name = document.getElementById('patient-name').value;
                        const phone = document.getElementById('patient-phone').value;
                        const birth = document.getElementById('patient-birth').value;
                        const insurance = document.getElementById('insurance-plan').value;
                        const type = document.getElementById('consultation-type').value;
                        const date = document.getElementById('appointment-date').value;
                        const time = document.getElementById('appointment-time').value;

                        if (!name || !phone || !birth || !insurance || !type || !date || !time) {
                            Swal.showValidationMessage('Por favor, preencha todos os campos');
                            return false;
                        }

                        // Validar formato da data de nascimento
                        if (birth.length !== 10 || !birth.match(/^\d{2}\/\d{2}\/\d{4}$/)) {
                            Swal.showValidationMessage('Data de nascimento deve estar no formato DD/MM/AAAA');
                            return false;
                        }

                        // Validar telefone completo
                        if (phone.length < 19) {  // +55 (XX) XXXXX-XXXX = 19 caracteres
                            Swal.showValidationMessage('Telefone incompleto. Use o formato +55 (XX) XXXXX-XXXX');
                            return false;
                        }

                        return { name, phone, birth, insurance, type, date, time };
                    }
                });

                if (formValues) {
                    await createAppointment(formValues);
                }
            }

            async function createAppointment(data) {
                try {
                    // Converter data de YYYY-MM-DD para YYYYMMDD
                    const dateFormatted = data.date.replace(/-/g, '');

                    const response = await fetch('/admin/appointments', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            patient_name: data.name,
                            patient_phone: data.phone,
                            patient_birth_date: data.birth,
                            insurance_plan: data.insurance,
                            consultation_type: data.type,
                            appointment_date: dateFormatted,
                            appointment_time: data.time
                        })
                    });

                    const result = await response.json();

                    if (response.ok) {
                        Swal.fire('Criada!', 'Consulta criada com sucesso.', 'success');
                        loadAppointments();
                    } else {
                        throw new Error(result.detail || 'Erro ao criar consulta');
                    }
                } catch (error) {
                    Swal.fire('Erro!', error.message, 'error');
                }
            }
        </script>
    </body>
    </html>
    """

    return HTMLResponse(
        content=html_content,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)