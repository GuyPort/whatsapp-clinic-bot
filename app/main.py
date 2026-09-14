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
from sqlalchemy.orm import Session
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
        media_type = None  # Tipo de mídia não suportada
        if 'conversation' in message_data:
            message_text = message_data['conversation']
        elif 'extendedTextMessage' in message_data:
            message_text = message_data['extendedTextMessage'].get('text', '')
        elif 'imageMessage' in message_data:
            message_text = message_data['imageMessage'].get('caption', '')
            if not message_text:
                media_type = 'imagem'
        elif 'audioMessage' in message_data:
            media_type = 'áudio'
        elif 'videoMessage' in message_data:
            media_type = 'vídeo'
        elif 'documentMessage' in message_data:
            media_type = 'documento'
        elif 'stickerMessage' in message_data:
            media_type = 'figurinha'
        
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

        # Tratar números @lid (Linked Device ID)
        if '@lid' in phone:
            # O número real vem no campo senderPn ou cleanedSenderPn do payload
            cleaned_sender = key.get('cleanedSenderPn')
            sender_pn = key.get('senderPn', '')

            if cleaned_sender:
                phone = cleaned_sender
                logger.info(f"✅ LID detectado, usando cleanedSenderPn: {phone}")
            elif sender_pn:
                phone = sender_pn.replace('@s.whatsapp.net', '').replace('@c.us', '')
                logger.info(f"✅ LID detectado, usando senderPn: {phone}")
            else:
                logger.warning(f"⚠️ LID detectado mas senderPn não disponível, ignorando")
                return {"status": "ignored", "reason": "LID without senderPn"}
        else:
            phone = phone.replace('@s.whatsapp.net', '').replace('@c.us', '')
        
        if not phone:
            logger.warning("Mensagem sem telefone")
            return {"status": "ignored", "reason": "no phone"}

        if not message_text:
            if media_type:
                # Responde que não processa mídia
                logger.info(f"Mídia recebida de {phone}: {media_type}")
                resposta = (
                    f"Desculpe, não consigo receber {media_type}. "
                    f"Se puder me explicar por texto, consigo te ajudar!\n\n"
                    f"Caso prefira, posso te transferir para nossa secretária Beatriz."
                )
                send_message_task.delay(phone, resposta)
                return {"status": "processed", "action": "media_response", "media_type": media_type}
            logger.warning("Mensagem sem texto")
            return {"status": "ignored", "reason": "no text"}

        logger.info(f"Mensagem de {phone}: {message_text[:50]}...")

        # Sistema de debounce: adicionar ao buffer e agendar task com delay
        # Isso permite agrupar múltiplas mensagens enviadas em sequência
        message_id = key.get('id')

        # Adicionar mensagem ao buffer Redis
        buffer_added = whatsapp_service.add_message_to_buffer(phone, message_text, message_id)

        if buffer_added:
            # Agendar task com delay de 7 segundos
            # Se outra mensagem chegar, essa task vai verificar e ignorar se não passou o tempo
            debounce_seconds = whatsapp_service.MESSAGE_DEBOUNCE_SECONDS
            task = process_message_task.apply_async(
                args=[phone, None, message_id],  # message_text=None pois vamos pegar do buffer
                countdown=debounce_seconds
            )
            logger.info(f"[DEBOUNCE] Task agendada para {phone} em {debounce_seconds}s (task: {task.id})")
            return {"status": "buffered", "task_id": task.id, "debounce_seconds": debounce_seconds}
        else:
            # Fallback: se Redis não disponível, processar imediatamente (comportamento antigo)
            task = process_message_task.delay(phone, message_text, message_id)
            logger.info(f"Task enfileirada (sem buffer): {task.id} para {phone}")
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
def process_message_task(self, phone: str, message_text: str = None, message_id: str = None):
    """
    Processa mensagem em background usando Celery.
    Suporta sistema de debounce: se message_text for None, busca do buffer Redis.

    Args:
        phone: Número do telefone
        message_text: Texto da mensagem (None se usando buffer)
        message_id: ID da mensagem (para marcar como lida)
    """
    task_id = self.request.id

    # Normalizar telefone primeiro
    phone = normalize_phone(phone)

    # ==========================================================================
    # SISTEMA DE DEBOUNCE: Verificar se deve processar agora
    # ==========================================================================
    if message_text is None:
        # Task foi agendada com delay - verificar se deve processar
        if not whatsapp_service.should_process_now(phone):
            # Ainda não passou tempo suficiente - outra mensagem chegou
            # Ignorar esta task, a próxima vai processar
            logger.info(f"[DEBOUNCE] Task {task_id} ignorada para {phone} - aguardando mais mensagens")
            return

        # Passou o tempo de debounce - pegar mensagens concatenadas do buffer
        message_text = whatsapp_service.get_concatenated_message(phone)

        if not message_text:
            logger.warning(f"[DEBOUNCE] Task {task_id} - Buffer vazio para {phone}")
            return

        logger.info(f"[DEBOUNCE] Task {task_id} processando {phone}: {message_text[:80]}...")
    else:
        # Modo antigo (fallback sem Redis) - processar diretamente
        logger.info(f"Task {task_id} iniciada para {phone}: {message_text[:50]}...")

    lock = None
    lock_acquired = False

    try:
        # Garantir processamento serializado por contato
        lock = whatsapp_service.acquire_chat_lock(phone)
        if lock:
            try:
                lock_acquired = lock.acquire(blocking=True)
            except Exception as lock_error:
                logger.warning(f"Nao foi possivel adquirir lock para {phone}: {lock_error}")
                raise self.retry(exc=lock_error, countdown=2)

            if not lock_acquired:
                logger.warning(f"Lock ocupado para {phone}, reagendando task")
                raise self.retry(exc=Exception("chat_lock_busy"), countdown=2)
        else:
            logger.warning(f"Processando {phone} sem lock - Redis indisponivel")

        # Marcar como lida
        if message_id:
            _mark_message_as_read_sync(phone, message_id)

        # Verificar comandos administrativos (/pausar)
        lowered = message_text.strip().lower()

        if lowered in {"/pausar", "/pause"}:
            with get_db() as db:
                logger.info(f"Comando /pausar recebido para {phone}")
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
                    logger.info(f"Bot pausado para {phone} ate {paused_contact.paused_until}")
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
            
            # Buscar apenas consultas AGENDADAS, ordenadas por data (crescente)
            appointments = db.query(Appointment).filter(
                Appointment.status == AppointmentStatus.AGENDADA
            ).order_by(
                Appointment.appointment_date.asc(),  # Data crescente
                Appointment.appointment_time.asc()   # Horário crescente
            ).all()

            # Calcular estatísticas
            today = datetime.now().date()
            week_start = today - timedelta(days=today.weekday())  # Início da semana
            week_end = week_start + timedelta(days=6)  # Fim da semana

            # Contar pacientes únicos (apenas agendadas)
            unique_patients = set()
            for apt in appointments:
                unique_patients.add(f"{apt.patient_name}_{apt.patient_birth_date}")

            # Calcular estatísticas com formato com hífen (apenas agendadas)
            today_str = today.strftime('%Y%m%d')
            week_start_str = week_start.strftime('%Y%m%d')
            week_end_str = week_end.strftime('%Y%m%d')

            stats = {
                "scheduled": len(appointments),
                "total_patients": len(unique_patients),
                "today": db.query(Appointment).filter(
                    Appointment.status == AppointmentStatus.AGENDADA,
                    Appointment.appointment_date == today_str
                ).count(),
                "this_week": db.query(Appointment).filter(
                    Appointment.status == AppointmentStatus.AGENDADA,
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
                    "is_new_patient": apt.is_new_patient,  # Paciente novo ou retorno
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


@app.get("/api/appointments/history")
async def get_appointments_history():
    """API para página de histórico - TODAS as consultas (todos os status)"""
    try:
        with get_db() as db:
            from datetime import datetime, timedelta

            # Buscar TODAS as consultas, ordenadas por data decrescente (mais recentes primeiro)
            appointments = db.query(Appointment).order_by(
                Appointment.appointment_date.desc(),  # Data decrescente
                Appointment.appointment_time.desc()   # Horário decrescente
            ).all()

            # Calcular estatísticas por status
            status_counts = {
                "agendada": 0,
                "compareceu": 0,
                "nao_compareceu": 0,
                "cancelada": 0
            }

            for apt in appointments:
                status_value = apt.status.value
                if status_value in status_counts:
                    status_counts[status_value] += 1

            # Formatar consultas
            formatted_appointments = []
            for apt in appointments:
                formatted_appointments.append({
                    "id": apt.id,
                    "patient_name": apt.patient_name,
                    "patient_phone": apt.patient_phone,
                    "patient_birth_date": apt.patient_birth_date,
                    "appointment_date": _format_appointment_date(apt.appointment_date),  # DD/MM/YYYY
                    "appointment_date_sortable": apt.appointment_date,  # YYYYMMDD para sort
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
                "total": len(appointments),
                "status_counts": status_counts,
                "appointments": formatted_appointments
            }

    except Exception as e:
        logger.error(f"Erro ao buscar histórico de consultas: {str(e)}")
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
    """Cancela uma consulta (marca como cancelada ao invés de deletar)"""
    try:
        with get_db() as db:
            appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()

            if not appointment:
                raise HTTPException(status_code=404, detail="Consulta não encontrada")

            # Log do cancelamento
            logger.info(f"Admin {admin} cancelou consulta #{appointment_id}: {appointment.patient_name} - {appointment.appointment_date} {appointment.appointment_time}")

            # Marcar como cancelada em vez de deletar
            appointment.status = AppointmentStatus.CANCELADA
            appointment.cancelled_at = datetime.utcnow()
            appointment.cancelled_reason = "Cancelada pelo administrador"

            # Bypass validação de horário (permite cancelar consultas com horários quebrados criadas por admin)
            appointment._skip_time_validation = True
            db.commit()

            return {
                "success": True,
                "message": "Consulta cancelada com sucesso"
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao cancelar consulta: {str(e)}")
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

            # Bypass validação de horário (permite marcar consultas com horários quebrados criadas por admin)
            appointment._skip_time_validation = True
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

            # Bypass validação de horário (permite marcar consultas com horários quebrados criadas por admin)
            appointment._skip_time_validation = True
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
            from app.utils import parse_appointment_datetime

            rules = AppointmentRules()

            # Verificar se o horário é válido (COM ADMIN OVERRIDE)
            new_datetime = parse_appointment_datetime(new_date, new_time)

            # Remover timezone para compatibilidade com check_slot_availability
            # (mesmo tratamento que o chat faz em ai_agent.py:5356)
            if new_datetime and new_datetime.tzinfo:
                new_datetime = new_datetime.replace(tzinfo=None)

            is_valid, error_msg = rules.is_valid_appointment_date(new_datetime, admin_override=True)
            if not is_valid:
                raise HTTPException(status_code=400, detail=error_msg)

            # Verificar disponibilidade (excluindo a própria consulta sendo remarcada)
            # MANTÉM verificação de conflito mesmo com admin override
            duration = appointment.duration_minutes or 60
            if not rules.check_slot_availability(new_datetime, duration, db, exclude_appointment_id=appointment_id, admin_override=True):
                raise HTTPException(status_code=400, detail="Horário já está ocupado")

            # Verificar regras de convênio (COM ADMIN OVERRIDE)
            insurance_plan = appointment.insurance_plan or "particular"
            allowed, msg = rules.is_plan_allowed_on_date(new_datetime, insurance_plan, admin_override=True)
            if not allowed:
                raise HTTPException(status_code=400, detail=msg)

            capacity_ok, msg = rules.has_capacity_for_insurance(new_datetime, insurance_plan, db, exclude_appointment_id=appointment_id, admin_override=True)
            if not capacity_ok:
                raise HTTPException(status_code=400, detail=msg)

            # Atualizar consulta
            appointment.appointment_date = new_date
            appointment.appointment_time = new_time
            appointment.status = AppointmentStatus.AGENDADA  # Reset status se estava como realizada

            # Permitir horários não-inteiros para admin
            appointment._skip_time_validation = True

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
        from app.utils import normalize_phone, parse_appointment_datetime
        from app.appointment_rules import AppointmentRules

        patient_phone = normalize_phone(body["patient_phone"])
        appointment_date = body["appointment_date"]  # YYYYMMDD
        appointment_time = body["appointment_time"]  # HH:MM
        duration_minutes = body.get("duration_minutes", 60)

        with get_db() as db:
            # Validar disponibilidade (COM ADMIN OVERRIDE)
            rules = AppointmentRules()

            appointment_datetime = parse_appointment_datetime(appointment_date, appointment_time)

            # Remover timezone para compatibilidade com check_slot_availability
            # (mesmo tratamento que o chat faz em ai_agent.py:5356)
            if appointment_datetime and appointment_datetime.tzinfo:
                appointment_datetime = appointment_datetime.replace(tzinfo=None)

            is_valid, error_msg = rules.is_valid_appointment_date(appointment_datetime, admin_override=True)
            if not is_valid:
                raise HTTPException(status_code=400, detail=error_msg)

            # MANTÉM verificação de conflito mesmo com admin override
            if not rules.check_slot_availability(appointment_datetime, duration_minutes, db, admin_override=True):
                raise HTTPException(status_code=400, detail="Horário já está ocupado")

            insurance_plan = body["insurance_plan"]
            allowed, msg = rules.is_plan_allowed_on_date(appointment_datetime, insurance_plan, admin_override=True)
            if not allowed:
                raise HTTPException(status_code=400, detail=msg)

            capacity_ok, msg = rules.has_capacity_for_insurance(appointment_datetime, insurance_plan, db, admin_override=True)
            if not capacity_ok:
                raise HTTPException(status_code=400, detail=msg)

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

            # Permitir horários não-inteiros para admin
            new_appointment._skip_time_validation = True

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


@app.api_route("/admin/migrate-status", methods=["GET", "POST"])
async def migrate_appointment_status(admin: str = Depends(verify_admin_credentials)):
    """
    Migração única para atualizar status antigos:
    - Deleta consultas com status 'cancelada'
    - Converte 'realizada' para 'compareceu'
    - Atualiza o tipo ENUM do PostgreSQL
    """
    try:
        from sqlalchemy import text

        logger.info("=== Iniciando migração de status ===")

        # ETAPA 1: Adicionar 'compareceu' ao enum (transação separada)
        try:
            with get_db() as db:
                logger.info("Tentando adicionar 'compareceu' ao enum...")
                db.execute(text("ALTER TYPE appointmentstatus ADD VALUE 'compareceu'"))
                db.commit()
                logger.info("✅ Valor 'compareceu' adicionado")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.info("ℹ️  Valor 'compareceu' já existe")
            else:
                logger.warning(f"Aviso ao adicionar 'compareceu': {str(e)}")

        # ETAPA 2: Adicionar 'nao_compareceu' ao enum (transação separada)
        try:
            with get_db() as db:
                logger.info("Tentando adicionar 'nao_compareceu' ao enum...")
                db.execute(text("ALTER TYPE appointmentstatus ADD VALUE 'nao_compareceu'"))
                db.commit()
                logger.info("✅ Valor 'nao_compareceu' adicionado")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.info("ℹ️  Valor 'nao_compareceu' já existe")
            else:
                logger.warning(f"Aviso ao adicionar 'nao_compareceu': {str(e)}")

        # ETAPA 3: Migrar dados (nova transação limpa)
        with get_db() as db:
            logger.info("Iniciando migração de dados...")

            # Contar total
            total_count = db.query(Appointment).count()

            # Contar canceladas (usar cast para text)
            canceled_count = 0
            try:
                result = db.execute(text("SELECT COUNT(*) FROM appointments WHERE status::text = 'cancelada'"))
                canceled_count = result.scalar() or 0
            except Exception as e:
                logger.warning(f"Não conseguiu contar canceladas: {str(e)}")

            # Contar realizadas (usar cast para text)
            realizada_count = 0
            try:
                result = db.execute(text("SELECT COUNT(*) FROM appointments WHERE status::text = 'realizada'"))
                realizada_count = result.scalar() or 0
            except Exception as e:
                logger.warning(f"Não conseguiu contar realizadas: {str(e)}")

            # Deletar canceladas
            if canceled_count > 0:
                logger.info(f"Deletando {canceled_count} consultas canceladas...")
                db.execute(text("DELETE FROM appointments WHERE status::text = 'cancelada'"))
                logger.info("✅ Consultas canceladas deletadas")

            # Converter realizada → compareceu
            if realizada_count > 0:
                logger.info(f"Convertendo {realizada_count} consultas 'realizada' → 'compareceu'...")
                db.execute(text("UPDATE appointments SET status = 'compareceu' WHERE status::text = 'realizada'"))
                logger.info("✅ Consultas convertidas")

            db.commit()

            logger.info(f"=== Migração concluída: {canceled_count} deletadas, {realizada_count} convertidas ===")

            return {
                "success": True,
                "message": "✅ Migração concluída com sucesso!",
                "stats": {
                    "total_before": total_count,
                    "canceled_deleted": canceled_count,
                    "realizada_converted": realizada_count,
                    "total_after": total_count - canceled_count
                },
                "next_steps": [
                    "✅ Enum do PostgreSQL atualizado",
                    "✅ Dados migrados com sucesso",
                    "✅ Agora você pode usar COMPARECEU e NAO_COMPARECEU",
                    "✅ Teste marcar um paciente como 'Faltou' no dashboard"
                ]
            }

    except Exception as e:
        logger.error(f"❌ Erro na migração: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.api_route("/admin/fix-enum", methods=["GET", "POST"])
async def fix_enum_values(admin: str = Depends(verify_admin_credentials)):
    """
    Corrige o enum appointmentstatus adicionando o valor 'agendada' que está faltando.
    Também mostra diagnóstico dos valores atuais do enum.
    """
    try:
        from sqlalchemy import text

        logger.info("=== Diagnóstico e correção do enum appointmentstatus ===")

        results = {
            "diagnostico": {},
            "acoes": [],
            "success": True
        }

        # ETAPA 1: Verificar valores atuais do enum
        with get_db() as db:
            logger.info("Consultando valores atuais do enum...")
            try:
                query = text("""
                    SELECT e.enumlabel
                    FROM pg_enum e
                    JOIN pg_type t ON e.enumtypid = t.oid
                    WHERE t.typname = 'appointmentstatus'
                    ORDER BY e.enumsortorder
                """)
                result = db.execute(query)
                current_values = [row[0] for row in result]
                results["diagnostico"]["valores_atuais"] = current_values
                logger.info(f"Valores atuais do enum: {current_values}")
            except Exception as e:
                logger.error(f"Erro ao consultar valores do enum: {str(e)}")
                results["diagnostico"]["erro"] = str(e)

        # ETAPA 2: Adicionar 'agendada' se não existir
        if 'agendada' not in current_values:
            try:
                with get_db() as db:
                    logger.info("Adicionando valor 'agendada' ao enum...")
                    db.execute(text("ALTER TYPE appointmentstatus ADD VALUE 'agendada'"))
                    db.commit()
                    logger.info("✅ Valor 'agendada' adicionado com sucesso")
                    results["acoes"].append("✅ Valor 'agendada' adicionado ao enum")
            except Exception as e:
                if "already exists" in str(e).lower():
                    logger.info("ℹ️ Valor 'agendada' já existe")
                    results["acoes"].append("ℹ️ Valor 'agendada' já existia")
                else:
                    logger.error(f"Erro ao adicionar 'agendada': {str(e)}")
                    results["acoes"].append(f"❌ Erro ao adicionar 'agendada': {str(e)}")
                    results["success"] = False
        else:
            results["acoes"].append("ℹ️ Valor 'agendada' já existe no enum")

        # ETAPA 2.5: Adicionar 'cancelada' se não existir
        if 'cancelada' not in current_values:
            try:
                with get_db() as db:
                    logger.info("Adicionando valor 'cancelada' ao enum...")
                    db.execute(text("ALTER TYPE appointmentstatus ADD VALUE 'cancelada'"))
                    db.commit()
                    logger.info("✅ Valor 'cancelada' adicionado com sucesso")
                    results["acoes"].append("✅ Valor 'cancelada' adicionado ao enum")
            except Exception as e:
                if "already exists" in str(e).lower():
                    logger.info("ℹ️ Valor 'cancelada' já existe")
                    results["acoes"].append("ℹ️ Valor 'cancelada' já existia")
                else:
                    logger.error(f"Erro ao adicionar 'cancelada': {str(e)}")
                    results["acoes"].append(f"❌ Erro ao adicionar 'cancelada': {str(e)}")
                    results["success"] = False
        else:
            results["acoes"].append("ℹ️ Valor 'cancelada' já existe no enum")

        # ETAPA 3: Verificar valores após correção
        with get_db() as db:
            query = text("""
                SELECT e.enumlabel
                FROM pg_enum e
                JOIN pg_type t ON e.enumtypid = t.oid
                WHERE t.typname = 'appointmentstatus'
                ORDER BY e.enumsortorder
            """)
            result = db.execute(query)
            final_values = [row[0] for row in result]
            results["diagnostico"]["valores_finais"] = final_values
            logger.info(f"Valores finais do enum: {final_values}")

        # ETAPA 4: Contar registros por status atual
        with get_db() as db:
            logger.info("Contando registros por status...")
            try:
                # Usar cast para text para evitar problemas de enum
                query = text("""
                    SELECT status::text, COUNT(*)
                    FROM appointments
                    GROUP BY status::text
                """)
                result = db.execute(query)
                status_counts = {row[0]: row[1] for row in result}
                results["diagnostico"]["contagem_por_status"] = status_counts
                logger.info(f"Contagem por status: {status_counts}")
            except Exception as e:
                logger.error(f"Erro ao contar status: {str(e)}")
                results["diagnostico"]["erro_contagem"] = str(e)

        return {
            "success": results["success"],
            "message": "✅ Diagnóstico e correção concluídos!" if results["success"] else "⚠️ Correção parcial",
            "diagnostico": results["diagnostico"],
            "acoes_realizadas": results["acoes"],
            "proximos_passos": [
                "1️⃣ Verificar se os valores finais do enum incluem: 'agendada', 'compareceu', 'nao_compareceu', 'cancelada'",
                "2️⃣ Fazer restart da aplicação no Railway para aplicar mudanças",
                "3️⃣ Testar cancelamento (deve mudar status em vez de deletar)",
                "4️⃣ Testar dashboard e histórico"
            ] if results["success"] else [
                "❌ Verifique os erros acima",
                "⚠️ Talvez seja necessário dropar e recriar o enum manualmente"
            ]
        }

    except Exception as e:
        logger.error(f"❌ Erro no diagnóstico/correção: {str(e)}")
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

            .badge-new-patient {
                background: rgba(245, 158, 11, 0.15);
                color: #D97706;
                font-weight: 600;
            }

            .badge-returning-patient {
                background: rgba(99, 102, 241, 0.1);
                color: #6366F1;
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
                    <p class="text-muted mb-0">Consultório Dra. Rose • Apenas consultas ativas</p>
                </div>
                <div style="display: flex; gap: 12px;">
                    <a href="/domiciliares" class="btn btn-outline-info" style="height: fit-content;">
                        <i class="fas fa-house-medical"></i> Domiciliares
                    </a>
                    <a href="/pausas" class="btn btn-outline-warning" style="height: fit-content;">
                        <i class="fas fa-pause-circle"></i> Pausas
                    </a>
                    <a href="/dashboard/historico" class="btn btn-outline-secondary" style="height: fit-content;">
                        <i class="fas fa-history"></i> Histórico
                    </a>
                    <button class="btn btn-primary" onclick="openCreateModal()" style="height: fit-content;">
                        <i class="fas fa-plus"></i> Criar Consulta
                    </button>
                </div>
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
                            ${getNewPatientBadge(appointment.is_new_patient)}
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

            function getNewPatientBadge(isNewPatient) {
                if (isNewPatient === true) {
                    return '<span class="badge-custom badge-new-patient">🆕 Primeira consulta</span>';
                } else if (isNewPatient === false) {
                    return '<span class="badge-custom badge-returning-patient">🔄 Retorno</span>';
                }
                return '';  // Não mostra nada se for null/undefined
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
                            <select id="new-time" class="swal2-input" style="margin-top: 0;">
                                <option value="">Selecione...</option>
                                <option value="13:00">13:00</option>
                                <option value="14:00">14:00</option>
                                <option value="15:00">15:00</option>
                                <option value="16:00">16:00</option>
                                <option value="17:00">17:00</option>
                                <option value="18:00">18:00</option>
                                <option value="19:00">19:00</option>
                            </select>
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
                            <select id="appointment-time" class="swal2-input" style="margin-top: 0;">
                                <option value="">Selecione...</option>
                                <option value="13:00">13:00</option>
                                <option value="14:00">14:00</option>
                                <option value="15:00">15:00</option>
                                <option value="16:00">16:00</option>
                                <option value="17:00">17:00</option>
                                <option value="18:00">18:00</option>
                                <option value="19:00">19:00</option>
                            </select>
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


@app.get("/dashboard/historico")
async def dashboard_historico(admin: str = Depends(verify_admin_credentials)):
    """Página de histórico completo - TODAS as consultas (todos os status)"""
    # Copiar todo o HTML do dashboard mas adaptar para histórico
    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Histórico Completo - Consultas</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
        <style>
            /* ESTILOS MODERNOS E LIMPOS - Mesmo do dashboard */

            :root {
                --primary: #4F46E5;
                --success: #10B981;
                --warning: #F59E0B;
                --danger: #EF4444;
                --info: #3B82F6;
                --dark: #1F2937;
                --light: #F9FAFB;
                --gray: #6B7280;

                /* Cores por status */
                --status-agendada: #3B82F6;
                --status-compareceu: #10B981;
                --status-faltou: #EF4444;
                --status-cancelada: #6B7280;
            }

            body {
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }

            .dashboard-container {
                max-width: 1400px;
                margin: 0 auto;
            }

            .header {
                background: white;
                border-radius: 16px;
                padding: 24px;
                margin-bottom: 24px;
                box-shadow: 0 4px 6px rgba(0,0,0,0.07);
            }

            .header h1 {
                margin: 0;
                color: var(--dark);
                font-size: 28px;
                font-weight: 700;
            }

            .header .subtitle {
                color: var(--gray);
                font-size: 14px;
                margin-top: 4px;
            }

            .back-btn {
                background: var(--info);
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 8px;
                text-decoration: none;
                display: inline-flex;
                align-items: center;
                gap: 8px;
                font-weight: 500;
                transition: all 0.2s;
            }

            .back-btn:hover {
                background: #2563eb;
                transform: translateY(-1px);
                box-shadow: 0 4px 12px rgba(59, 130, 246, 0.4);
                color: white;
            }

            .stats-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 16px;
                margin-bottom: 24px;
            }

            .stat-card {
                background: white;
                border-radius: 12px;
                padding: 20px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.05);
                transition: transform 0.2s, box-shadow 0.2s;
            }

            .stat-card:hover {
                transform: translateY(-2px);
                box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            }

            .stat-card.agendada { border-left: 4px solid var(--status-agendada); }
            .stat-card.compareceu { border-left: 4px solid var(--status-compareceu); }
            .stat-card.faltou { border-left: 4px solid var(--status-faltou); }
            .stat-card.cancelada { border-left: 4px solid var(--status-cancelada); }

            .stat-label {
                font-size: 12px;
                color: var(--gray);
                text-transform: uppercase;
                font-weight: 600;
                letter-spacing: 0.5px;
            }

            .stat-value {
                font-size: 32px;
                font-weight: 700;
                color: var(--dark);
                margin-top: 8px;
            }

            .appointments-table {
                background: white;
                border-radius: 12px;
                padding: 24px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.05);
            }

            .table {
                margin: 0;
            }

            .table thead th {
                background: var(--light);
                color: var(--dark);
                font-weight: 600;
                font-size: 12px;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                border: none;
                padding: 12px 16px;
            }

            .table tbody tr {
                border-bottom: 1px solid #E5E7EB;
                transition: background-color 0.15s;
            }

            .table tbody tr:hover {
                background-color: #F9FAFB;
            }

            .table tbody td {
                padding: 16px;
                vertical-align: middle;
                border: none;
            }

            /* Cores por status nas linhas */
            tr.status-agendada { border-left: 4px solid var(--status-agendada); }
            tr.status-compareceu { border-left: 4px solid var(--status-compareceu); }
            tr.status-nao_compareceu { border-left: 4px solid var(--status-faltou); }
            tr.status-cancelada { border-left: 4px solid var(--status-cancelada); background-color: #F9FAFB; }

            .badge {
                padding: 6px 12px;
                border-radius: 6px;
                font-size: 11px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            .badge.status-agendada {
                background: #DBEAFE;
                color: #1E40AF;
            }

            .badge.status-compareceu {
                background: #D1FAE5;
                color: #065F46;
            }

            .badge.status-nao_compareceu {
                background: #FEE2E2;
                color: #991B1B;
            }

            .badge.status-cancelada {
                background: #E5E7EB;
                color: #374151;
                text-decoration: line-through;
            }

            .btn-sm {
                padding: 6px 12px;
                font-size: 12px;
                border-radius: 6px;
                font-weight: 500;
            }

            .loading {
                text-align: center;
                padding: 40px;
                color: var(--gray);
            }

            .spinner-border {
                width: 3rem;
                height: 3rem;
            }

            .empty-state {
                text-align: center;
                padding: 60px 20px;
                color: var(--gray);
            }

            .empty-state i {
                font-size: 48px;
                margin-bottom: 16px;
                opacity: 0.5;
            }
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            <!-- Header -->
            <div class="header">
                <div class="d-flex justify-content-between align-items-center">
                    <div>
                        <h1><i class="fas fa-history"></i> Histórico Completo de Consultas</h1>
                        <div class="subtitle">Visualize todas as consultas: agendadas, realizadas, faltadas e canceladas</div>
                    </div>
                    <a href="/dashboard" class="back-btn">
                        <i class="fas fa-arrow-left"></i>
                        Voltar para Consultas Ativas
                    </a>
                </div>
            </div>

            <!-- Estatísticas por Status -->
            <div class="stats-grid" id="stats-grid">
                <div class="stat-card agendada">
                    <div class="stat-label">🔵 Agendadas</div>
                    <div class="stat-value" id="stat-agendada">-</div>
                </div>
                <div class="stat-card compareceu">
                    <div class="stat-label">🟢 Compareceram</div>
                    <div class="stat-value" id="stat-compareceu">-</div>
                </div>
                <div class="stat-card faltou">
                    <div class="stat-label">🔴 Faltaram</div>
                    <div class="stat-value" id="stat-faltou">-</div>
                </div>
                <div class="stat-card cancelada">
                    <div class="stat-label">⚫ Canceladas</div>
                    <div class="stat-value" id="stat-cancelada">-</div>
                </div>
            </div>

            <!-- Tabela de Consultas -->
            <div class="appointments-table">
                <div id="loading" class="loading">
                    <div class="spinner-border text-primary" role="status"></div>
                    <p class="mt-3">Carregando histórico...</p>
                </div>

                <div id="empty-state" class="empty-state" style="display: none;">
                    <i class="fas fa-inbox"></i>
                    <h3>Nenhuma consulta encontrada</h3>
                    <p>O histórico está vazio.</p>
                </div>

                <div id="appointments-container" style="display: none;">
                    <table class="table">
                        <thead>
                            <tr>
                                <th>Paciente</th>
                                <th>Data</th>
                                <th>Horário</th>
                                <th>Tipo</th>
                                <th>Convênio</th>
                                <th>Status</th>
                                <th class="text-end">Ações</th>
                            </tr>
                        </thead>
                        <tbody id="appointments-tbody">
                            <!-- Preenchido via JavaScript -->
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <script>
            // Autenticação
            const adminPassword = prompt("Senha de administrador:");
            if (!adminPassword) {
                window.location.href = "/";
            }

            // Carregar histórico
            async function loadHistory() {
                try {
                    const response = await fetch('/api/appointments/history');
                    if (!response.ok) throw new Error('Erro ao carregar histórico');

                    const data = await response.json();

                    document.getElementById('loading').style.display = 'none';

                    if (data.appointments.length === 0) {
                        document.getElementById('empty-state').style.display = 'block';
                        return;
                    }

                    // Atualizar estatísticas
                    document.getElementById('stat-agendada').textContent = data.status_counts.agendada || 0;
                    document.getElementById('stat-compareceu').textContent = data.status_counts.compareceu || 0;
                    document.getElementById('stat-faltou').textContent = data.status_counts.nao_compareceu || 0;
                    document.getElementById('stat-cancelada').textContent = data.status_counts.cancelada || 0;

                    // Renderizar tabela
                    const tbody = document.getElementById('appointments-tbody');
                    tbody.innerHTML = '';

                    data.appointments.forEach(appointment => {
                        const tr = document.createElement('tr');
                        tr.className = `status-${appointment.status}`;

                        const statusLabels = {
                            'agendada': '🔵 Agendada',
                            'compareceu': '🟢 Compareceu',
                            'nao_compareceu': '🔴 Faltou',
                            'cancelada': '⚫ Cancelada'
                        };

                        const statusBadge = `<span class="badge status-${appointment.status}">${statusLabels[appointment.status]}</span>`;

                        // Botões de ação baseados no status
                        let actionButtons = '';

                        if (appointment.status === 'agendada') {
                            actionButtons = `
                                <button class="btn btn-sm btn-warning" onclick="editAppointment(${appointment.id})">
                                    <i class="fas fa-edit"></i>
                                </button>
                                <button class="btn btn-sm btn-success" onclick="markAttended(${appointment.id}, '${appointment.patient_name}')">
                                    <i class="fas fa-check"></i>
                                </button>
                                <button class="btn btn-sm btn-danger" onclick="markMissed(${appointment.id}, '${appointment.patient_name}')">
                                    <i class="fas fa-times"></i>
                                </button>
                                <button class="btn btn-sm btn-secondary" onclick="cancelAppointment(${appointment.id}, '${appointment.patient_name}')">
                                    <i class="fas fa-ban"></i>
                                </button>
                            `;
                        } else {
                            actionButtons = `<span class="text-muted">-</span>`;
                        }

                        tr.innerHTML = `
                            <td><strong>${appointment.patient_name}</strong><br><small class="text-muted">${appointment.patient_phone}</small></td>
                            <td>${appointment.appointment_date}</td>
                            <td>${appointment.appointment_time}</td>
                            <td>${appointment.consultation_type || '-'}</td>
                            <td>${appointment.insurance_plan || '-'}</td>
                            <td>${statusBadge}</td>
                            <td class="text-end">${actionButtons}</td>
                        `;

                        tbody.appendChild(tr);
                    });

                    document.getElementById('appointments-container').style.display = 'block';

                } catch (error) {
                    console.error('Erro:', error);
                    document.getElementById('loading').innerHTML = `
                        <i class="fas fa-exclamation-circle text-danger" style="font-size: 48px;"></i>
                        <p class="mt-3 text-danger">Erro ao carregar consultas. Tente novamente.</p>
                    `;
                }
            }

            // Funções de ação (mesmas do dashboard principal)
            async function editAppointment(id) {
                // TODO: Implementar edição
                alert('Edição em desenvolvimento');
            }

            async function markAttended(id, patientName) {
                const confirmed = await Swal.fire({
                    title: 'Marcar como Compareceu?',
                    text: `Confirmar que ${patientName} compareceu à consulta?`,
                    icon: 'question',
                    showCancelButton: true,
                    confirmButtonText: 'Sim, compareceu',
                    cancelButtonText: 'Cancelar',
                    confirmButtonColor: '#10B981'
                });

                if (confirmed.isConfirmed) {
                    try {
                        const response = await fetch(`/admin/appointments/${id}/mark-attended?admin=${adminPassword}`, {
                            method: 'POST'
                        });

                        if (response.ok) {
                            Swal.fire('Sucesso!', 'Paciente marcado como compareceu', 'success');
                            loadHistory();
                        } else {
                            throw new Error('Erro ao marcar presença');
                        }
                    } catch (error) {
                        Swal.fire('Erro!', error.message, 'error');
                    }
                }
            }

            async function markMissed(id, patientName) {
                const confirmed = await Swal.fire({
                    title: 'Marcar como Faltou?',
                    text: `Confirmar que ${patientName} não compareceu à consulta?`,
                    icon: 'warning',
                    showCancelButton: true,
                    confirmButtonText: 'Sim, faltou',
                    cancelButtonText: 'Cancelar',
                    confirmButtonColor: '#EF4444'
                });

                if (confirmed.isConfirmed) {
                    try {
                        const response = await fetch(`/admin/appointments/${id}/mark-missed?admin=${adminPassword}`, {
                            method: 'POST'
                        });

                        if (response.ok) {
                            Swal.fire('Registrado!', 'Falta registrada', 'success');
                            loadHistory();
                        } else {
                            throw new Error('Erro ao marcar falta');
                        }
                    } catch (error) {
                        Swal.fire('Erro!', error.message, 'error');
                    }
                }
            }

            async function cancelAppointment(id, patientName) {
                const confirmed = await Swal.fire({
                    title: 'Cancelar Consulta?',
                    text: `Tem certeza que deseja cancelar a consulta de ${patientName}?`,
                    icon: 'warning',
                    showCancelButton: true,
                    confirmButtonText: 'Sim, cancelar',
                    cancelButtonText: 'Não',
                    confirmButtonColor: '#EF4444'
                });

                if (confirmed.isConfirmed) {
                    try {
                        const response = await fetch(`/admin/appointments/${id}?admin=${adminPassword}`, {
                            method: 'DELETE'
                        });

                        if (response.ok) {
                            Swal.fire('Cancelada!', 'Consulta cancelada com sucesso', 'success');
                            loadHistory();
                        } else {
                            throw new Error('Erro ao cancelar');
                        }
                    } catch (error) {
                        Swal.fire('Erro!', error.message, 'error');
                    }
                }
            }

            // Carregar ao iniciar
            loadHistory();

            // Auto-refresh a cada 30 segundos
            setInterval(loadHistory, 30000);
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


# ==================== GERENCIAMENTO DE PAUSAS ====================

@app.get("/api/paused-contacts")
async def get_paused_contacts(admin: str = Depends(verify_admin_credentials)):
    """Lista todos os contatos pausados ativos"""
    from datetime import datetime, timedelta

    with get_db() as db:
        now = datetime.utcnow()
        paused = db.query(PausedContact).filter(
            PausedContact.paused_until > now
        ).order_by(PausedContact.paused_until.asc()).all()

        result = []
        for p in paused:
            remaining = p.paused_until - now
            remaining_hours = remaining.total_seconds() / 3600
            result.append({
                "phone": p.phone,
                "reason": p.reason or "manual",
                "paused_at": p.paused_at.isoformat() if p.paused_at else None,
                "paused_until": p.paused_until.isoformat(),
                "remaining_hours": round(remaining_hours, 1),
                "remaining_formatted": f"{int(remaining_hours)}h {int((remaining_hours % 1) * 60)}min"
            })

        return {"paused_contacts": result, "count": len(result)}


@app.post("/api/paused-contacts")
async def pause_contact(request: Request, admin: str = Depends(verify_admin_credentials)):
    """Pausar um contato manualmente"""
    from datetime import datetime, timedelta

    data = await request.json()
    phone = data.get("phone", "").strip()
    hours = data.get("hours", 24)
    reason = data.get("reason", "secretary_dashboard_pause")

    if not phone:
        raise HTTPException(status_code=400, detail="Telefone é obrigatório")

    # Normalizar telefone
    phone = normalize_phone(phone)

    with get_db() as db:
        # Verificar se já existe
        existing = db.query(PausedContact).filter(PausedContact.phone == phone).first()

        paused_until = datetime.utcnow() + timedelta(hours=hours)

        if existing:
            existing.paused_until = paused_until
            existing.reason = reason
            existing.paused_at = datetime.utcnow()
        else:
            new_pause = PausedContact(
                phone=phone,
                paused_until=paused_until,
                reason=reason,
                paused_at=datetime.utcnow()
            )
            db.add(new_pause)

        db.commit()

        logger.info(f"⏸️ Contato {phone} pausado via dashboard até {paused_until}")
        return {"success": True, "phone": phone, "paused_until": paused_until.isoformat()}


@app.delete("/api/paused-contacts/{phone}")
async def unpause_contact(phone: str, admin: str = Depends(verify_admin_credentials)):
    """Despausar um contato"""
    phone = normalize_phone(phone)

    with get_db() as db:
        existing = db.query(PausedContact).filter(PausedContact.phone == phone).first()

        if not existing:
            raise HTTPException(status_code=404, detail="Contato não encontrado na lista de pausados")

        db.delete(existing)
        db.commit()

        logger.info(f"▶️ Contato {phone} despausado via dashboard")
        return {"success": True, "phone": phone}


@app.put("/api/paused-contacts/{phone}/extend")
async def extend_pause(phone: str, request: Request, admin: str = Depends(verify_admin_credentials)):
    """Estender pausa de um contato"""
    from datetime import datetime, timedelta

    data = await request.json()
    hours = data.get("hours", 24)
    phone = normalize_phone(phone)

    with get_db() as db:
        existing = db.query(PausedContact).filter(PausedContact.phone == phone).first()

        if not existing:
            raise HTTPException(status_code=404, detail="Contato não encontrado na lista de pausados")

        # Adicionar horas ao tempo atual de pausa
        existing.paused_until = existing.paused_until + timedelta(hours=hours)
        db.commit()

        logger.info(f"⏸️ Pausa do contato {phone} estendida por +{hours}h até {existing.paused_until}")
        return {"success": True, "phone": phone, "paused_until": existing.paused_until.isoformat()}


@app.get("/api/active-conversations")
async def get_active_conversations(admin: str = Depends(verify_admin_credentials)):
    """Lista conversas ativas (contextos ativos na última hora)"""
    from datetime import datetime, timedelta

    with get_db() as db:
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)

        conversations = db.query(ConversationContext).filter(
            ConversationContext.status == "active",
            ConversationContext.last_activity > one_hour_ago
        ).order_by(ConversationContext.last_activity.desc()).all()

        # Também buscar se o contato está pausado
        paused_phones = {p.phone for p in db.query(PausedContact).filter(
            PausedContact.paused_until > datetime.utcnow()
        ).all()}

        result = []
        for c in conversations:
            # Extrair nome do flow_data se disponível
            flow_data = c.flow_data or {}
            patient_name = flow_data.get("patient_name")

            # Calcular tempo desde última atividade
            time_diff = datetime.utcnow() - c.last_activity
            minutes_ago = int(time_diff.total_seconds() / 60)

            if minutes_ago < 1:
                time_ago = "agora"
            elif minutes_ago < 60:
                time_ago = f"há {minutes_ago} min"
            else:
                hours_ago = int(minutes_ago / 60)
                time_ago = f"há {hours_ago}h"

            result.append({
                "phone": c.phone,
                "patient_name": patient_name,
                "last_activity": c.last_activity.isoformat(),
                "time_ago": time_ago,
                "current_flow": c.current_flow,
                "message_count": len(c.messages) if c.messages else 0,
                "is_paused": c.phone in paused_phones
            })

        return {"conversations": result, "count": len(result)}


@app.get("/pausas")
async def pausas_dashboard(admin: str = Depends(verify_admin_credentials)):
    """Dashboard de gerenciamento de pausas e conversas ativas"""
    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Gerenciamento de Pausas</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
        <style>
            :root {
                --primary: #4F46E5;
                --success: #10B981;
                --warning: #F59E0B;
                --danger: #EF4444;
                --info: #3B82F6;
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
                font-size: 0.9rem;
            }

            .dashboard-container {
                max-width: 1200px;
                margin: 0 auto;
                padding: 1rem;
            }

            .header {
                background: var(--card-bg);
                border-radius: 12px;
                padding: 0.75rem 1.25rem;
                margin-bottom: 1rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
                display: flex;
                justify-content: space-between;
                align-items: center;
            }

            .header h1 {
                font-size: 1.25rem;
                font-weight: 700;
                color: var(--primary);
                margin: 0;
            }

            .header h1 i {
                margin-right: 0.5rem;
            }

            .refresh-info {
                color: var(--text-muted);
                font-size: 0.75rem;
            }

            .section {
                background: var(--card-bg);
                border-radius: 12px;
                padding: 1rem;
                margin-bottom: 1rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            }

            .section-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 0.75rem;
                padding-bottom: 0.5rem;
                border-bottom: 1px solid var(--border);
            }

            .section-title {
                font-size: 1rem;
                font-weight: 600;
                margin: 0;
                display: flex;
                align-items: center;
                gap: 0.5rem;
            }

            .badge-count {
                background: var(--primary);
                color: white;
                padding: 0.15rem 0.5rem;
                border-radius: 20px;
                font-size: 0.75rem;
                font-weight: 600;
            }

            .badge-count.warning {
                background: var(--warning);
            }

            .table-container {
                overflow-x: auto;
            }

            table {
                width: 100%;
                border-collapse: collapse;
            }

            th {
                text-align: left;
                padding: 0.5rem 0.75rem;
                background: var(--bg);
                color: var(--text-muted);
                font-weight: 600;
                font-size: 0.75rem;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }

            td {
                padding: 0.5rem 0.75rem;
                border-bottom: 1px solid var(--border);
                vertical-align: middle;
            }

            tr:hover {
                background: var(--bg);
            }

            .phone-cell {
                font-family: monospace;
                font-size: 0.85rem;
            }

            .name-cell {
                font-weight: 500;
            }

            .badge-reason {
                padding: 0.15rem 0.4rem;
                border-radius: 4px;
                font-size: 0.7rem;
                font-weight: 500;
            }

            .badge-reason.manual { background: #E0E7FF; color: #4338CA; }
            .badge-reason.prescription { background: #FEF3C7; color: #92400E; }
            .badge-reason.holiday { background: #DBEAFE; color: #1E40AF; }
            .badge-reason.human { background: #D1FAE5; color: #065F46; }
            .badge-reason.requisicao { background: #FCE7F3; color: #9D174D; }

            .time-badge {
                display: inline-flex;
                align-items: center;
                gap: 0.2rem;
                padding: 0.15rem 0.4rem;
                border-radius: 4px;
                font-size: 0.75rem;
                font-weight: 500;
            }

            .time-badge.urgent { background: #FEE2E2; color: #B91C1C; }
            .time-badge.normal { background: #FEF3C7; color: #92400E; }
            .time-badge.relaxed { background: #D1FAE5; color: #065F46; }

            .action-btn {
                padding: 0.25rem 0.5rem;
                border-radius: 4px;
                border: none;
                font-size: 0.75rem;
                font-weight: 500;
                cursor: pointer;
                transition: all 0.2s;
                margin-right: 0.25rem;
            }

            .action-btn:hover {
                transform: translateY(-1px);
            }

            .btn-pause {
                background: var(--warning);
                color: white;
            }

            .btn-unpause {
                background: var(--success);
                color: white;
            }

            .btn-extend {
                background: var(--info);
                color: white;
            }

            .btn-add {
                background: var(--primary);
                color: white;
                padding: 0.25rem 0.75rem;
                font-size: 0.75rem;
            }

            .empty-state {
                text-align: center;
                padding: 1.5rem;
                color: var(--text-muted);
            }

            .empty-state i {
                font-size: 2rem;
                margin-bottom: 0.5rem;
                opacity: 0.5;
            }

            .empty-state p {
                margin: 0;
                font-size: 0.85rem;
            }

            .paused-badge {
                background: var(--danger);
                color: white;
                padding: 0.1rem 0.35rem;
                border-radius: 3px;
                font-size: 0.65rem;
                margin-left: 0.35rem;
            }

            .nav-links {
                display: flex;
                gap: 0.75rem;
            }

            .nav-link {
                color: var(--text-muted);
                text-decoration: none;
                font-size: 0.8rem;
                transition: color 0.2s;
            }

            .nav-link:hover {
                color: var(--primary);
            }

            @keyframes pulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.5; }
            }

            .live-indicator {
                display: inline-flex;
                align-items: center;
                gap: 0.35rem;
                color: var(--success);
                font-size: 0.75rem;
            }

            .live-dot {
                width: 6px;
                height: 6px;
                background: var(--success);
                border-radius: 50%;
                animation: pulse 2s infinite;
            }
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            <div class="header">
                <h1><i class="fas fa-pause-circle"></i> Gerenciamento de Pausas</h1>
                <div class="nav-links">
                    <a href="/dashboard" class="nav-link"><i class="fas fa-calendar"></i> Consultas</a>
                    <a href="/dashboard/historico" class="nav-link"><i class="fas fa-history"></i> Histórico</a>
                </div>
            </div>

            <!-- Conversas Ativas -->
            <div class="section">
                <div class="section-header">
                    <h2 class="section-title">
                        <i class="fas fa-comments" style="color: var(--success)"></i>
                        Conversas Ativas
                        <span class="live-indicator"><span class="live-dot"></span> Ao vivo</span>
                    </h2>
                    <span class="badge-count" id="conversations-count">0</span>
                </div>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Telefone</th>
                                <th>Nome</th>
                                <th>Última Atividade</th>
                                <th>Mensagens</th>
                                <th>Ações</th>
                            </tr>
                        </thead>
                        <tbody id="conversations-table">
                            <tr>
                                <td colspan="5" class="empty-state">
                                    <i class="fas fa-spinner fa-spin"></i>
                                    <p>Carregando...</p>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Contatos Pausados -->
            <div class="section">
                <div class="section-header">
                    <h2 class="section-title">
                        <i class="fas fa-pause" style="color: var(--warning)"></i>
                        Contatos Pausados
                    </h2>
                    <div>
                        <span class="badge-count warning" id="paused-count">0</span>
                        <button class="action-btn btn-add" onclick="showAddPauseModal()">
                            <i class="fas fa-plus"></i> Pausar Contato
                        </button>
                    </div>
                </div>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Telefone</th>
                                <th>Motivo</th>
                                <th>Tempo Restante</th>
                                <th>Desbloqueia em</th>
                                <th>Ações</th>
                            </tr>
                        </thead>
                        <tbody id="paused-table">
                            <tr>
                                <td colspan="5" class="empty-state">
                                    <i class="fas fa-spinner fa-spin"></i>
                                    <p>Carregando...</p>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="refresh-info" style="text-align: center; margin-top: 1rem;">
                <i class="fas fa-sync-alt"></i> Atualização automática a cada 10 segundos
            </div>
        </div>

        <script>
            // Formatadores
            function formatPhone(phone) {
                if (phone.length === 13) {
                    return `+${phone.slice(0,2)} (${phone.slice(2,4)}) ${phone.slice(4,9)}-${phone.slice(9)}`;
                }
                return phone;
            }

            function getReasonBadge(reason) {
                const reasons = {
                    'secretary_dashboard_pause': { label: 'Manual', class: 'manual' },
                    'secretary_manual_pause': { label: 'Manual', class: 'manual' },
                    'prescription_payment': { label: 'Receita', class: 'prescription' },
                    'special_holiday_request': { label: 'Período Especial', class: 'holiday' },
                    'user_requested_human_assistance': { label: 'Pediu Atendente', class: 'human' },
                    'requisicao_exames': { label: 'Requisição', class: 'requisicao' }
                };
                const r = reasons[reason] || { label: reason || 'Manual', class: 'manual' };
                return `<span class="badge-reason ${r.class}">${r.label}</span>`;
            }

            function getTimeBadge(hours) {
                let cls = 'relaxed';
                if (hours < 2) cls = 'urgent';
                else if (hours < 12) cls = 'normal';
                return cls;
            }

            function formatDateTime(isoString) {
                const date = new Date(isoString);
                return date.toLocaleString('pt-BR', {
                    day: '2-digit',
                    month: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit'
                });
            }

            // Carregar dados
            async function loadData() {
                try {
                    const [pausedRes, convsRes] = await Promise.all([
                        fetch('/api/paused-contacts'),
                        fetch('/api/active-conversations')
                    ]);

                    const pausedData = await pausedRes.json();
                    const convsData = await convsRes.json();

                    renderPausedTable(pausedData.paused_contacts);
                    renderConversationsTable(convsData.conversations);

                    document.getElementById('paused-count').textContent = pausedData.count;
                    document.getElementById('conversations-count').textContent = convsData.count;
                } catch (error) {
                    console.error('Erro ao carregar dados:', error);
                }
            }

            function renderPausedTable(contacts) {
                const tbody = document.getElementById('paused-table');

                if (!contacts || contacts.length === 0) {
                    tbody.innerHTML = `
                        <tr>
                            <td colspan="5" class="empty-state">
                                <i class="fas fa-check-circle"></i>
                                <p>Nenhum contato pausado</p>
                            </td>
                        </tr>
                    `;
                    return;
                }

                tbody.innerHTML = contacts.map(c => `
                    <tr>
                        <td class="phone-cell">${formatPhone(c.phone)}</td>
                        <td>${getReasonBadge(c.reason)}</td>
                        <td>
                            <span class="time-badge ${getTimeBadge(c.remaining_hours)}">
                                <i class="fas fa-clock"></i> ${c.remaining_formatted}
                            </span>
                        </td>
                        <td>${formatDateTime(c.paused_until)}</td>
                        <td>
                            <button class="action-btn btn-extend" onclick="extendPause('${c.phone}')">
                                <i class="fas fa-plus"></i> Estender
                            </button>
                            <button class="action-btn btn-unpause" onclick="unpauseContact('${c.phone}')">
                                <i class="fas fa-play"></i> Despausar
                            </button>
                        </td>
                    </tr>
                `).join('');
            }

            function renderConversationsTable(conversations) {
                const tbody = document.getElementById('conversations-table');

                if (!conversations || conversations.length === 0) {
                    tbody.innerHTML = `
                        <tr>
                            <td colspan="5" class="empty-state">
                                <i class="fas fa-comment-slash"></i>
                                <p>Nenhuma conversa ativa no momento</p>
                            </td>
                        </tr>
                    `;
                    return;
                }

                tbody.innerHTML = conversations.map(c => `
                    <tr>
                        <td class="phone-cell">
                            ${formatPhone(c.phone)}
                            ${c.is_paused ? '<span class="paused-badge">PAUSADO</span>' : ''}
                        </td>
                        <td class="name-cell">${c.patient_name || '<span style="color: var(--text-muted)">-</span>'}</td>
                        <td>${c.time_ago}</td>
                        <td>${c.message_count}</td>
                        <td>
                            ${c.is_paused ?
                                `<button class="action-btn btn-unpause" onclick="unpauseContact('${c.phone}')">
                                    <i class="fas fa-play"></i> Despausar
                                </button>` :
                                `<button class="action-btn btn-pause" onclick="pauseContact('${c.phone}')">
                                    <i class="fas fa-pause"></i> Pausar
                                </button>`
                            }
                        </td>
                    </tr>
                `).join('');
            }

            // Ações
            async function pauseContact(phone) {
                const { value: hours } = await Swal.fire({
                    title: 'Pausar Contato',
                    text: `Pausar ${formatPhone(phone)} por quantas horas?`,
                    input: 'select',
                    inputOptions: {
                        '2': '2 horas',
                        '6': '6 horas',
                        '12': '12 horas',
                        '24': '24 horas',
                        '48': '48 horas'
                    },
                    inputValue: '24',
                    showCancelButton: true,
                    confirmButtonText: 'Pausar',
                    cancelButtonText: 'Cancelar',
                    confirmButtonColor: '#F59E0B'
                });

                if (hours) {
                    try {
                        const res = await fetch('/api/paused-contacts', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ phone, hours: parseInt(hours) })
                        });

                        if (res.ok) {
                            Swal.fire('Pausado!', 'Contato pausado com sucesso.', 'success');
                            loadData();
                        } else {
                            throw new Error('Erro ao pausar');
                        }
                    } catch (error) {
                        Swal.fire('Erro', 'Não foi possível pausar o contato.', 'error');
                    }
                }
            }

            async function unpauseContact(phone) {
                const result = await Swal.fire({
                    title: 'Despausar Contato?',
                    text: `O bot voltará a responder para ${formatPhone(phone)}`,
                    icon: 'question',
                    showCancelButton: true,
                    confirmButtonText: 'Sim, despausar',
                    cancelButtonText: 'Cancelar',
                    confirmButtonColor: '#10B981'
                });

                if (result.isConfirmed) {
                    try {
                        const res = await fetch(`/api/paused-contacts/${phone}`, {
                            method: 'DELETE'
                        });

                        if (res.ok) {
                            Swal.fire('Despausado!', 'Contato liberado.', 'success');
                            loadData();
                        } else {
                            throw new Error('Erro ao despausar');
                        }
                    } catch (error) {
                        Swal.fire('Erro', 'Não foi possível despausar o contato.', 'error');
                    }
                }
            }

            async function extendPause(phone) {
                const { value: hours } = await Swal.fire({
                    title: 'Estender Pausa',
                    text: `Adicionar mais quantas horas para ${formatPhone(phone)}?`,
                    input: 'select',
                    inputOptions: {
                        '2': '+2 horas',
                        '6': '+6 horas',
                        '12': '+12 horas',
                        '24': '+24 horas'
                    },
                    inputValue: '24',
                    showCancelButton: true,
                    confirmButtonText: 'Estender',
                    cancelButtonText: 'Cancelar',
                    confirmButtonColor: '#3B82F6'
                });

                if (hours) {
                    try {
                        const res = await fetch(`/api/paused-contacts/${phone}/extend`, {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ hours: parseInt(hours) })
                        });

                        if (res.ok) {
                            Swal.fire('Estendido!', `Pausa estendida por +${hours} horas.`, 'success');
                            loadData();
                        } else {
                            throw new Error('Erro ao estender');
                        }
                    } catch (error) {
                        Swal.fire('Erro', 'Não foi possível estender a pausa.', 'error');
                    }
                }
            }

            async function showAddPauseModal() {
                const { value: formValues } = await Swal.fire({
                    title: 'Pausar Novo Contato',
                    html: `
                        <input id="swal-phone" class="swal2-input" placeholder="Telefone (ex: 5551999999999)">
                        <select id="swal-hours" class="swal2-select">
                            <option value="2">2 horas</option>
                            <option value="6">6 horas</option>
                            <option value="12">12 horas</option>
                            <option value="24" selected>24 horas</option>
                            <option value="48">48 horas</option>
                        </select>
                    `,
                    focusConfirm: false,
                    showCancelButton: true,
                    confirmButtonText: 'Pausar',
                    cancelButtonText: 'Cancelar',
                    confirmButtonColor: '#F59E0B',
                    preConfirm: () => {
                        const phone = document.getElementById('swal-phone').value.replace(/\\D/g, '');
                        const hours = document.getElementById('swal-hours').value;
                        if (!phone || phone.length < 10) {
                            Swal.showValidationMessage('Digite um telefone válido');
                            return false;
                        }
                        return { phone, hours: parseInt(hours) };
                    }
                });

                if (formValues) {
                    try {
                        const res = await fetch('/api/paused-contacts', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(formValues)
                        });

                        if (res.ok) {
                            Swal.fire('Pausado!', 'Contato pausado com sucesso.', 'success');
                            loadData();
                        } else {
                            throw new Error('Erro ao pausar');
                        }
                    } catch (error) {
                        Swal.fire('Erro', 'Não foi possível pausar o contato.', 'error');
                    }
                }
            }

            // Inicialização
            loadData();
            setInterval(loadData, 10000); // Refresh a cada 10 segundos
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


# ============================================
# API de Solicitações de Atendimento Domiciliar
# ============================================

@app.get("/api/home-visits")
async def get_home_visits(db: Session = Depends(get_db)):
    """Lista todas as solicitações de atendimento domiciliar"""
    from app.models import HomeVisitRequest

    requests = db.query(HomeVisitRequest).order_by(HomeVisitRequest.created_at.desc()).all()

    return {
        "count": len(requests),
        "requests": [
            {
                "id": r.id,
                "patient_name": r.patient_name,
                "patient_phone": r.patient_phone,
                "patient_birth_date": r.patient_birth_date,
                "patient_address": r.patient_address,
                "created_at": r.created_at.isoformat() if r.created_at else None
            }
            for r in requests
        ]
    }


@app.get("/domiciliares")
async def domiciliares_dashboard(admin: str = Depends(verify_admin_credentials)):
    """Dashboard de solicitações de atendimento domiciliar (somente visualização)"""
    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Solicitações Domiciliares</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        <style>
            :root {
                --primary: #4F46E5;
                --success: #10B981;
                --warning: #F59E0B;
                --danger: #EF4444;
                --info: #3B82F6;
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
                font-size: 0.9rem;
            }

            .dashboard-container {
                max-width: 1200px;
                margin: 0 auto;
                padding: 1rem;
            }

            .header {
                background: var(--card-bg);
                border-radius: 12px;
                padding: 0.75rem 1.25rem;
                margin-bottom: 1rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
                display: flex;
                justify-content: space-between;
                align-items: center;
            }

            .header h1 {
                font-size: 1.25rem;
                font-weight: 700;
                color: var(--primary);
                margin: 0;
            }

            .header h1 i {
                margin-right: 0.5rem;
            }

            .header-actions {
                display: flex;
                gap: 0.5rem;
                align-items: center;
            }

            .btn-nav {
                background: var(--bg);
                border: 1px solid var(--border);
                color: var(--text);
                padding: 0.4rem 0.75rem;
                border-radius: 8px;
                font-size: 0.8rem;
                font-weight: 500;
                cursor: pointer;
                text-decoration: none;
                display: flex;
                align-items: center;
                gap: 0.3rem;
            }

            .btn-nav:hover {
                background: var(--primary);
                color: white;
                border-color: var(--primary);
            }

            .section {
                background: var(--card-bg);
                border-radius: 12px;
                padding: 1rem;
                margin-bottom: 1rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            }

            .section-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 0.75rem;
                padding-bottom: 0.5rem;
                border-bottom: 1px solid var(--border);
            }

            .section-title {
                font-size: 1rem;
                font-weight: 600;
                margin: 0;
                display: flex;
                align-items: center;
                gap: 0.5rem;
            }

            .badge-count {
                background: var(--primary);
                color: white;
                padding: 0.15rem 0.5rem;
                border-radius: 20px;
                font-size: 0.75rem;
                font-weight: 600;
            }

            .request-card {
                background: var(--bg);
                border-radius: 10px;
                padding: 1rem;
                margin-bottom: 0.75rem;
                border-left: 4px solid var(--primary);
            }

            .request-header {
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                margin-bottom: 0.5rem;
            }

            .patient-name {
                font-weight: 600;
                font-size: 1rem;
                color: var(--text);
            }

            .request-date {
                font-size: 0.75rem;
                color: var(--text-muted);
            }

            .request-details {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 0.5rem;
            }

            .detail-item {
                display: flex;
                align-items: center;
                gap: 0.5rem;
                font-size: 0.85rem;
            }

            .detail-item i {
                color: var(--primary);
                width: 16px;
            }

            .address-item {
                grid-column: 1 / -1;
                background: white;
                padding: 0.5rem;
                border-radius: 6px;
                margin-top: 0.25rem;
            }

            .empty-state {
                text-align: center;
                padding: 3rem;
                color: var(--text-muted);
            }

            .empty-state i {
                font-size: 3rem;
                margin-bottom: 1rem;
                opacity: 0.5;
            }

            .refresh-info {
                color: var(--text-muted);
                font-size: 0.75rem;
            }

            @media (max-width: 768px) {
                .header {
                    flex-direction: column;
                    gap: 0.75rem;
                    text-align: center;
                }
                .request-details {
                    grid-template-columns: 1fr;
                }
            }
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            <div class="header">
                <h1><i class="fas fa-house-medical"></i> Solicitações Domiciliares</h1>
                <div class="header-actions">
                    <span class="refresh-info" id="lastUpdate"></span>
                    <button class="btn-nav" onclick="loadRequests()">
                        <i class="fas fa-sync-alt"></i> Atualizar
                    </button>
                    <a href="/dashboard" class="btn-nav">
                        <i class="fas fa-calendar"></i> Dashboard
                    </a>
                </div>
            </div>

            <div class="section">
                <div class="section-header">
                    <h2 class="section-title">
                        <i class="fas fa-list"></i> Solicitações
                        <span class="badge-count" id="requestCount">0</span>
                    </h2>
                </div>
                <div id="requestsList"></div>
            </div>
        </div>

        <script>
            let allRequests = [];

            async function loadRequests() {
                try {
                    const response = await fetch('/api/home-visits');
                    const data = await response.json();
                    allRequests = data.requests;
                    document.getElementById('requestCount').textContent = data.count;
                    displayRequests();
                    document.getElementById('lastUpdate').textContent =
                        'Atualizado: ' + new Date().toLocaleTimeString('pt-BR');
                } catch (error) {
                    console.error('Erro ao carregar solicitações:', error);
                }
            }

            function formatDate(isoDate) {
                if (!isoDate) return '-';
                const date = new Date(isoDate);
                return date.toLocaleDateString('pt-BR') + ' ' + date.toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit'});
            }

            function formatPhone(phone) {
                if (!phone) return '-';
                // Remove 55 do início se tiver
                let p = phone.replace(/^55/, '');
                if (p.length === 11) {
                    return `(${p.slice(0,2)}) ${p.slice(2,7)}-${p.slice(7)}`;
                }
                return phone;
            }

            function displayRequests() {
                const container = document.getElementById('requestsList');

                if (allRequests.length === 0) {
                    container.innerHTML = `
                        <div class="empty-state">
                            <i class="fas fa-inbox"></i>
                            <p>Nenhuma solicitação de atendimento domiciliar</p>
                        </div>
                    `;
                    return;
                }

                let html = '';
                for (const req of allRequests) {
                    html += `
                        <div class="request-card">
                            <div class="request-header">
                                <span class="patient-name">${req.patient_name || '-'}</span>
                                <span class="request-date">${formatDate(req.created_at)}</span>
                            </div>
                            <div class="request-details">
                                <div class="detail-item">
                                    <i class="fas fa-phone"></i>
                                    <span>${formatPhone(req.patient_phone)}</span>
                                </div>
                                <div class="detail-item">
                                    <i class="fas fa-birthday-cake"></i>
                                    <span>${req.patient_birth_date || '-'}</span>
                                </div>
                                <div class="detail-item address-item">
                                    <i class="fas fa-map-marker-alt"></i>
                                    <span>${req.patient_address || '-'}</span>
                                </div>
                            </div>
                        </div>
                    `;
                }
                container.innerHTML = html;
            }

            // Carregar ao iniciar
            document.addEventListener('DOMContentLoaded', loadRequests);

            // Auto-refresh a cada 60 segundos
            setInterval(loadRequests, 60000);
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


# =============================================================================
# AMBIENTE DE TESTE - Simulador de Chat WhatsApp
# =============================================================================

TEST_PHONE = "5500000000000"  # Número simulado para testes

@app.get("/test/chat", response_class=HTMLResponse)
async def test_chat_page():
    """Página de teste com interface de chat estilo WhatsApp"""
    return """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Teste do Bot - Simulador WhatsApp</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: #0b141a;
                height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
            }
            .chat-container {
                width: 100%;
                max-width: 500px;
                height: 95vh;
                background: #0b141a;
                display: flex;
                flex-direction: column;
                border-radius: 10px;
                overflow: hidden;
                box-shadow: 0 0 20px rgba(0,0,0,0.5);
            }
            .chat-header {
                background: #202c33;
                padding: 10px 16px;
                display: flex;
                align-items: center;
                gap: 12px;
                border-bottom: 1px solid #2a3942;
            }
            .avatar {
                width: 40px;
                height: 40px;
                background: #00a884;
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 20px;
            }
            .header-info h3 {
                color: #e9edef;
                font-size: 16px;
                font-weight: 500;
            }
            .header-info span {
                color: #8696a0;
                font-size: 12px;
            }
            .header-actions {
                margin-left: auto;
                display: flex;
                gap: 8px;
            }
            .header-actions button {
                background: #ea4335;
                border: none;
                color: white;
                padding: 6px 12px;
                border-radius: 4px;
                cursor: pointer;
                font-size: 12px;
            }
            .header-actions button:hover { background: #d33426; }
            .chat-messages {
                flex: 1;
                overflow-y: auto;
                padding: 20px;
                background: #0b141a url("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADIAAAAyCAYAAAAeP4ixAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAABnSURBVHgB7dCxDQAgDAOwkv9/GRZgYKJ7Qcq8SQIAAAAAAAAAAAAAAAAAAACAt9Xd5z1JkvX+3Jckd/eZuz8fAAAAAAAAAAAAAAAAAAAAAAAAAADgny4L/RLLnhL7pgAAAABJRU5ErkJggg==");
            }
            .message {
                max-width: 80%;
                margin-bottom: 8px;
                padding: 8px 12px;
                border-radius: 8px;
                font-size: 14px;
                line-height: 1.4;
                position: relative;
                word-wrap: break-word;
                white-space: pre-wrap;
            }
            .message.user {
                background: #005c4b;
                color: #e9edef;
                margin-left: auto;
                border-bottom-right-radius: 0;
            }
            .message.bot {
                background: #202c33;
                color: #e9edef;
                margin-right: auto;
                border-bottom-left-radius: 0;
            }
            .message .time {
                font-size: 11px;
                color: #8696a0;
                text-align: right;
                margin-top: 4px;
            }
            .message.bot .time { color: #8696a0; }
            .chat-input {
                background: #202c33;
                padding: 10px 16px;
                display: flex;
                gap: 10px;
                align-items: center;
            }
            .chat-input input {
                flex: 1;
                background: #2a3942;
                border: none;
                padding: 12px 16px;
                border-radius: 8px;
                color: #e9edef;
                font-size: 14px;
                outline: none;
            }
            .chat-input input::placeholder { color: #8696a0; }
            .chat-input button {
                background: #00a884;
                border: none;
                width: 42px;
                height: 42px;
                border-radius: 50%;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .chat-input button:hover { background: #06cf9c; }
            .chat-input button:disabled { background: #2a3942; cursor: not-allowed; }
            .chat-input button svg { fill: #e9edef; width: 20px; height: 20px; }
            .typing {
                display: none;
                color: #8696a0;
                font-size: 12px;
                padding: 8px 12px;
            }
            .typing.visible { display: block; }
            .system-message {
                text-align: center;
                color: #8696a0;
                font-size: 12px;
                margin: 16px 0;
                padding: 6px 12px;
                background: #202c33;
                border-radius: 8px;
                display: inline-block;
                margin-left: 50%;
                transform: translateX(-50%);
            }
            .phone-display {
                color: #8696a0;
                font-size: 11px;
                text-align: center;
                padding: 4px;
                background: #202c33;
            }
        </style>
    </head>
    <body>
        <div class="chat-container">
            <div class="chat-header">
                <div class="avatar">🏥</div>
                <div class="header-info">
                    <h3>Bot da Clínica</h3>
                    <span>Ambiente de Teste</span>
                </div>
                <div class="header-actions">
                    <button onclick="resetChat()">Resetar Conversa</button>
                </div>
            </div>
            <div class="phone-display">Telefone simulado: <strong id="phone-number">5500000000000</strong></div>
            <div class="chat-messages" id="messages">
                <div class="system-message">Envie mensagens para testar o bot (debounce: 7s)</div>
            </div>
            <div class="typing" id="typing">Bot está digitando...</div>
            <div class="chat-input">
                <input type="text" id="messageInput" placeholder="Digite uma mensagem..." onkeypress="if(event.key==='Enter') sendMessage()">
                <button onclick="sendMessage()" id="sendBtn">
                    <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
                </button>
            </div>
        </div>
        <script>
            const messagesDiv = document.getElementById('messages');
            const input = document.getElementById('messageInput');
            const typing = document.getElementById('typing');
            const sendBtn = document.getElementById('sendBtn');

            // Debounce configuration (simula produção)
            const DEBOUNCE_SECONDS = 10;
            let messageBuffer = [];
            let debounceTimer = null;
            let countdownInterval = null;
            let countdownValue = 0;

            function getTime() {
                return new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
            }

            function addMessage(text, isUser) {
                const msg = document.createElement('div');
                msg.className = 'message ' + (isUser ? 'user' : 'bot');
                msg.innerHTML = text + '<div class="time">' + getTime() + '</div>';
                messagesDiv.appendChild(msg);
                messagesDiv.scrollTop = messagesDiv.scrollHeight;
            }

            function startCountdown() {
                countdownValue = DEBOUNCE_SECONDS;
                updateTypingText();
                typing.classList.add('visible');

                if (countdownInterval) clearInterval(countdownInterval);
                countdownInterval = setInterval(() => {
                    countdownValue--;
                    if (countdownValue > 0) {
                        updateTypingText();
                    }
                }, 1000);
            }

            function updateTypingText() {
                const bufferInfo = messageBuffer.length > 1 ? ' [' + messageBuffer.length + ' msgs]' : '';
                typing.textContent = 'Aguardando mais mensagens... (' + countdownValue + 's)' + bufferInfo;
            }

            function stopCountdown() {
                if (countdownInterval) {
                    clearInterval(countdownInterval);
                    countdownInterval = null;
                }
            }

            function sendMessage() {
                const text = input.value.trim();
                if (!text) return;

                // Adiciona mensagem na UI imediatamente
                addMessage(text, true);
                input.value = '';

                // Adiciona ao buffer
                messageBuffer.push(text);

                // Reseta o timer de debounce
                if (debounceTimer) clearTimeout(debounceTimer);
                startCountdown();

                debounceTimer = setTimeout(processBuffer, DEBOUNCE_SECONDS * 1000);
            }

            async function processBuffer() {
                if (messageBuffer.length === 0) return;

                stopCountdown();
                input.disabled = true;
                sendBtn.disabled = true;
                typing.textContent = 'Bot esta digitando...';
                typing.classList.add('visible');

                // Concatena todas as mensagens do buffer
                const concatenated = messageBuffer.join('\\n');
                const msgCount = messageBuffer.length;
                messageBuffer = [];

                console.log('[Debounce] Processando ' + msgCount + ' mensagem(ns): ' + concatenated);

                try {
                    const response = await fetch('/test/chat', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ message: concatenated })
                    });
                    const data = await response.json();

                    if (data.response) {
                        addMessage(data.response, false);
                    } else if (data.error) {
                        addMessage('Erro: ' + data.error, false);
                    }
                } catch (err) {
                    addMessage('Erro de conexao: ' + err.message, false);
                }

                typing.classList.remove('visible');
                input.disabled = false;
                sendBtn.disabled = false;
                input.focus();
            }

            async function resetChat() {
                if (!confirm('Tem certeza que deseja resetar a conversa? Todo o histórico será apagado.')) return;

                try {
                    const response = await fetch('/test/reset', { method: 'POST' });
                    const data = await response.json();

                    messagesDiv.innerHTML = '<div class="system-message">Conversa resetada - ' + getTime() + '</div>';
                    alert(data.message || 'Conversa resetada!');
                } catch (err) {
                    alert('Erro ao resetar: ' + err.message);
                }
            }

            input.focus();
        </script>
    </body>
    </html>
    """


@app.post("/test/chat")
async def test_chat_send(request: Request):
    """
    Endpoint de teste que processa mensagem e retorna resposta diretamente.
    Simula exatamente o comportamento do bot no WhatsApp, mas sem:
    - Celery (processamento síncrono)
    - Evolution API (não envia para WhatsApp)
    - Redis locks (não precisa)
    """
    try:
        data = await request.json()
        message_text = data.get("message", "").strip()

        if not message_text:
            return JSONResponse({"error": "Mensagem vazia"}, status_code=400)

        phone = TEST_PHONE
        logger.info(f"[TEST] Mensagem recebida: {message_text}")

        # Verificar comandos administrativos
        lowered = message_text.lower()
        if lowered in {"/pausar", "/pause"}:
            with get_db() as db:
                response = ai_agent._handle_request_human_assistance({}, db, phone)
                return {"response": response, "phone": phone}

        # Verificar se bot está pausado
        with get_db() as db:
            paused = db.query(PausedContact).filter_by(phone=phone).first()
            if paused:
                from datetime import datetime
                if datetime.utcnow() < paused.paused_until:
                    return {"response": "[Bot pausado para este número - aguardando atendimento humano]", "phone": phone}
                else:
                    db.delete(paused)
                    db.commit()

            # Processar com IA (mesmo código do webhook real)
            response = ai_agent.process_message(message_text, phone, db)

            logger.info(f"[TEST] Resposta gerada: {response[:100] if response else 'None'}...")

            return {"response": response or "[Sem resposta]", "phone": phone}

    except Exception as e:
        logger.error(f"[TEST] Erro: {str(e)}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/test/reset")
async def test_chat_reset():
    """Reseta o contexto de conversa do número de teste"""
    try:
        with get_db() as db:
            # Deletar contexto de conversa
            context = db.query(ConversationContext).filter_by(phone=TEST_PHONE).first()
            if context:
                db.delete(context)

            # Deletar pausa se existir
            paused = db.query(PausedContact).filter_by(phone=TEST_PHONE).first()
            if paused:
                db.delete(paused)

            # Deletar agendamentos de teste
            appointments = db.query(Appointment).filter_by(patient_phone=TEST_PHONE).all()
            for apt in appointments:
                db.delete(apt)

            db.commit()

        logger.info(f"[TEST] Contexto resetado para {TEST_PHONE}")
        return {"message": "Conversa resetada com sucesso!", "phone": TEST_PHONE}

    except Exception as e:
        logger.error(f"[TEST] Erro ao resetar: {str(e)}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)