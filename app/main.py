"""
Aplicação FastAPI principal com webhooks do WhatsApp.
"""
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from contextlib import asynccontextmanager
import logging
from typing import Dict, Any, List

from app.simple_config import settings

# Debug: Log das configurações no startup
logger = logging.getLogger(__name__)
logger.info("=== STARTUP DEBUG ===")
logger.info(f"Evolution API URL: {settings.evolution_api_url}")
logger.info(f"Instance Name: {settings.evolution_instance_name}")
logger.info(f"API Key: {settings.evolution_api_key[:10] if settings.evolution_api_key else 'None'}...")
logger.info("===================")
from app.database import init_db, get_db
from app.ai_agent import ai_agent
from app.whatsapp_service import whatsapp_service
from app.utils import normalize_phone
from app.models import Patient, Appointment, ConversationContext, AppointmentStatus, ConversationState

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
    logger.info("✅ Bot iniciado com sucesso!")
    
    yield
    
    # Shutdown
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
                    <li><code>GET /health</code> - Health check</li>
                    <li><code>POST /webhook/whatsapp</code> - Webhook do WhatsApp</li>
                </ul>
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
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
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
        
        # Processar mensagem em background
        background_tasks.add_task(
            process_message_task,
            phone,
            message_text,
            key.get('id')
        )
        
        return {"status": "processing"}
        
    except Exception as e:
        logger.error(f"Erro no webhook: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def process_message_task(phone: str, message_text: str, message_id: str = None):
    """
    Processa mensagem em background.
    
    Args:
        phone: Número do telefone
        message_text: Texto da mensagem
        message_id: ID da mensagem (para marcar como lida)
    """
    try:
        # Normalizar telefone
        phone = normalize_phone(phone)
        
        # Marcar como lida
        if message_id:
            await whatsapp_service.mark_message_as_read(phone, message_id)
        
        # Processar com IA
        with get_db() as db:
            response = await ai_agent.process_message(phone, message_text, db)
        
        # Enviar resposta
        if response:
            success = await whatsapp_service.send_message(phone, response)
            if success:
                logger.info(f"Resposta enviada para {phone}")
            else:
                logger.error(f"Falha ao enviar resposta para {phone}")
        
    except Exception as e:
        logger.error(f"Erro ao processar mensagem: {str(e)}", exc_info=True)
        
        # Tentar enviar mensagem de erro ao usuário
        try:
            await whatsapp_service.send_message(
                phone,
                "Desculpe, ocorreu um erro ao processar sua mensagem. Por favor, tente novamente em instantes."
            )
        except:
            pass


@app.get("/status")
async def status():
    """Retorna status detalhado do sistema"""
    try:
        # Verificar WhatsApp
        whatsapp_status = await whatsapp_service.get_instance_status()
        
        # Verificar Google Calendar
        calendar_available = "available" if calendar_service.is_available() else "unavailable"
        
        return {
            "status": "operational",
            "whatsapp": whatsapp_status,
            "google_calendar": calendar_available,
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


# Importar calendar_service aqui para evitar import circular
from app.calendar_service import calendar_service


# ==================== ENDPOINTS DO BANCO DE DADOS ====================

@app.get("/admin/patients")
async def get_patients():
    """Lista todos os pacientes cadastrados"""
    try:
        with get_db() as db:
            patients = db.query(Patient).order_by(Patient.created_at.desc()).all()
            return {
                "total": len(patients),
                "patients": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "phone": p.phone,
                        "birth_date": p.birth_date,
                        "created_at": p.created_at.isoformat(),
                        "appointments_count": len(p.appointments)
                    }
                    for p in patients
                ]
            }
    except Exception as e:
        logger.error(f"Erro ao buscar pacientes: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


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
                        "patient_name": a.patient.name,
                        "patient_phone": a.patient.phone,
                        "appointment_date": a.appointment_date,
                        "appointment_time": a.appointment_time,
                        "consult_type": a.consult_type,
                        "status": a.status.value,
                        "notes": a.notes,
                        "created_at": a.created_at.isoformat()
                    }
                    for a in appointments
                ]
            }
    except Exception as e:
        logger.error(f"Erro ao buscar consultas: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/conversations")
async def get_conversations():
    """Lista contextos de conversas ativas"""
    try:
        with get_db() as db:
            conversations = db.query(ConversationContext).order_by(ConversationContext.last_message_at.desc()).all()
            return {
                "total": len(conversations),
                "conversations": [
                    {
                        "id": c.id,
                        "patient_name": c.patient.name,
                        "patient_phone": c.patient.phone,
                        "state": c.state.value,
                        "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
                        "created_at": c.created_at.isoformat()
                    }
                    for c in conversations
                ]
            }
    except Exception as e:
        logger.error(f"Erro ao buscar conversas: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/dashboard")
async def get_dashboard():
    """Dashboard com estatísticas gerais"""
    try:
        with get_db() as db:
            # Contadores
            total_patients = db.query(Patient).count()
            total_appointments = db.query(Appointment).count()
            active_conversations = db.query(ConversationContext).filter(
                ConversationContext.state != ConversationState.IDLE
            ).count()
            
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
                    "active_conversations": active_conversations,
                    "recent_appointments": recent_appointments
                },
                "appointments_by_status": appointments_by_status
            }
    except Exception as e:
        logger.error(f"Erro ao buscar dashboard: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/patient/{patient_id}")
async def get_patient_details(patient_id: int):
    """Detalhes de um paciente específico"""
    try:
        with get_db() as db:
            patient = db.query(Patient).filter(Patient.id == patient_id).first()
            if not patient:
                raise HTTPException(status_code=404, detail="Paciente não encontrado")
            
            appointments = db.query(Appointment).filter(Appointment.patient_id == patient_id).all()
            conversations = db.query(ConversationContext).filter(ConversationContext.patient_id == patient_id).all()
            
            return {
                "patient": {
                    "id": patient.id,
                    "name": patient.name,
                    "phone": patient.phone,
                    "birth_date": patient.birth_date,
                    "created_at": patient.created_at.isoformat()
                },
                "appointments": [
                    {
                        "id": a.id,
                        "appointment_date": a.appointment_date,
                        "appointment_time": a.appointment_time,
                        "consult_type": a.consult_type,
                        "status": a.status.value,
                        "notes": a.notes
                    }
                    for a in appointments
                ],
                "conversations": [
                    {
                        "id": c.id,
                        "state": c.state.value,
                        "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None
                    }
                    for c in conversations
                ]
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Erro ao buscar paciente: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

