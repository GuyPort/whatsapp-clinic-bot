"""
Aplicação FastAPI principal com webhooks do WhatsApp.
"""
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
# StaticFiles removido - não utilizado
from contextlib import asynccontextmanager
import logging
from typing import Dict, Any, List
from datetime import datetime, date

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
from app.models import Appointment, ConversationContext

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
        
        # Verificar se bot está pausado para este telefone
        with get_db() as db:
            context = db.query(ConversationContext).filter_by(phone=phone).first()
            
            if context and context.status == "paused_human":
                if context.paused_until and datetime.utcnow() < context.paused_until:
                    # Ainda pausado - bot ignora mensagem
                    logger.info(f"Bot pausado para {phone} até {context.paused_until}")
                    return
                else:
                    # Passou 2 horas - reativar silenciosamente
                    logger.info(f"Bot reativado automaticamente para {phone}")
                    context.status = "active"
                    context.paused_until = None
                    context.messages = []
                    context.flow_data = {}
                    db.commit()
            
            # Processar com IA
            response = ai_agent.process_message(message_text, phone, db)
        
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


@app.post("/admin/migrate-fix-date")
async def migrate_fix_date_admin():
    """Endpoint admin para executar migração de data no PostgreSQL"""
    try:
        from migrate_railway_fix_date import migrate_railway_appointment_date
        
        logger.info("🚀 Executando migração de data via endpoint admin...")
        success = migrate_railway_appointment_date()
        
        if success:
            logger.info("✅ Migração executada com sucesso!")
            return {"status": "success", "message": "Migração executada com sucesso! Coluna appointment_date alterada para VARCHAR(10)."}
        else:
            logger.error("❌ Erro durante migração")
            return {"status": "error", "message": "Erro durante migração"}
            
    except Exception as e:
        logger.error(f"❌ Erro ao executar migração: {str(e)}")
        return {"status": "error", "message": f"Erro: {str(e)}"}



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
            
            # Buscar todas as consultas ordenadas por data - AGORA COM STRINGS
            appointments = db.query(Appointment).order_by(
                Appointment.created_at.desc()
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
                    "appointment_date": _format_appointment_date(apt.appointment_date),  # ← FORMATAR AQUI TAMBÉM
                    "appointment_date_br": _format_appointment_date(apt.appointment_date),  # Converter qualquer formato para DD/MM/YYYY
                    "appointment_time": apt.appointment_time,  # String HH:MM
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


# Endpoint de conversas removido - ConversationContext não existe mais


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
                    "active_conversations": active_conversations,
                    "recent_appointments": recent_appointments
                },
                "appointments_by_status": appointments_by_status
            }
    except Exception as e:
        logger.error(f"Erro ao buscar dashboard: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/dashboard")
async def dashboard():
    """Dashboard simples para visualizar consultas agendadas"""
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
            body {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            }
            .dashboard-container {
                background: rgba(255, 255, 255, 0.95);
                border-radius: 15px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                margin: 20px;
                padding: 30px;
            }
            .header {
                text-align: center;
                margin-bottom: 30px;
                border-bottom: 2px solid #667eea;
                padding-bottom: 20px;
            }
            .stats-card {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border-radius: 10px;
                padding: 20px;
                margin-bottom: 20px;
                text-align: center;
            }
            .appointment-card {
                background: white;
                border: 1px solid #e0e0e0;
                border-radius: 10px;
                padding: 20px;
                margin-bottom: 15px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                transition: transform 0.2s;
            }
            .appointment-card:hover {
                transform: translateY(-2px);
                box-shadow: 0 4px 20px rgba(0,0,0,0.15);
            }
            .status-badge {
                padding: 5px 15px;
                border-radius: 20px;
                font-size: 12px;
                font-weight: bold;
            }
            .status-agendada { background-color: #28a745; color: white; }
            .status-cancelada { background-color: #dc3545; color: white; }
            .status-realizada { background-color: #17a2b8; color: white; }
            .btn-refresh {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                border: none;
                color: white;
                padding: 12px 30px;
                border-radius: 25px;
                font-weight: bold;
                transition: all 0.3s;
            }
            .btn-refresh:hover {
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
                color: white;
            }
            .loading {
                text-align: center;
                padding: 50px;
                color: #666;
            }
            .no-appointments {
                text-align: center;
                padding: 50px;
                color: #666;
                background: #f8f9fa;
                border-radius: 10px;
                margin-top: 20px;
            }
            .patient-info {
                font-size: 14px;
                color: #666;
                margin-top: 5px;
            }
            .appointment-time {
                font-size: 18px;
                font-weight: bold;
                color: #667eea;
            }
            .table {
                background: white;
                border-radius: 10px;
                overflow: hidden;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }
            .table thead th {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                border: none;
                color: white;
                font-weight: bold;
                padding: 15px;
            }
            .table tbody tr {
                transition: all 0.2s;
            }
            .table tbody tr:hover {
                background-color: #f8f9fa;
                transform: scale(1.01);
            }
            .table td {
                padding: 15px;
                vertical-align: middle;
                border-color: #e9ecef;
            }
            .table-striped tbody tr:nth-of-type(odd) {
                background-color: rgba(102, 126, 234, 0.05);
            }
        </style>
    </head>
    <body>
        <div class="container-fluid">
            <div class="dashboard-container">
                <div class="header">
                    <h1><i class="fas fa-calendar-check"></i> Dashboard - Consultas Agendadas</h1>
                    <p class="text-muted">Consultório Dra. Rose</p>
                </div>

                <!-- Estatísticas -->
                <div class="row mb-4">
                    <div class="col-md-3">
                        <div class="stats-card">
                            <h3 id="total-scheduled">-</h3>
                            <p>Consultas Agendadas</p>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="stats-card">
                            <h3 id="total-patients">-</h3>
                            <p>Total de Pacientes</p>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="stats-card">
                            <h3 id="today-appointments">-</h3>
                            <p>Consultas Hoje</p>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="stats-card">
                            <h3 id="week-appointments">-</h3>
                            <p>Esta Semana</p>
                        </div>
                    </div>
                </div>

                <!-- Botão Atualizar -->
                <div class="text-center mb-4">
                    <button class="btn btn-refresh" onclick="loadAppointments()">
                        <i class="fas fa-sync-alt"></i> Atualizar Consultas
                    </button>
                    <p class="text-muted mt-2">
                        Última atualização: <span id="last-update">-</span>
                    </p>
                </div>

                <!-- Lista de Consultas -->
                <div id="appointments-container">
                    <div class="loading">
                        <i class="fas fa-spinner fa-spin fa-2x"></i>
                        <p>Carregando consultas...</p>
                    </div>
                </div>
            </div>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
        <script>
            // Carregar dados ao abrir a página
            document.addEventListener('DOMContentLoaded', function() {
                loadAppointments();
            });

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

                    // Atualizar estatísticas
                    updateStats(data.stats);

                    // Atualizar lista de consultas
                    displayAppointments(data.appointments);

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

            function displayAppointments(appointments) {
                const container = document.getElementById('appointments-container');
                
                if (!appointments || appointments.length === 0) {
                    container.innerHTML = `
                        <div class="no-appointments">
                            <i class="fas fa-calendar-times fa-3x mb-3"></i>
                            <h4>Nenhuma consulta agendada</h4>
                            <p>As consultas agendadas aparecerão aqui.</p>
                        </div>
                    `;
                    return;
                }

                const html = `
                    <div class="table-responsive">
                        <table class="table table-striped table-hover">
                            <thead class="table-dark">
                                <tr>
                                    <th>Nome</th>
                                    <th>Telefone</th>
                                    <th>Data de Nascimento</th>
                                    <th>Data da Consulta</th>
                                    <th>Horário</th>
                                    <th>Status</th>
                                    <th>Duração</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${appointments.map(appointment => `
                                    <tr>
                                        <td>
                                            <strong>${appointment.patient_name}</strong>
                                        </td>
                                        <td>
                                            <small class="text-muted">📞 ${appointment.patient_phone}</small>
                                        </td>
                                        <td>${appointment.patient_birth_date}</td>
                                        <td>${formatDate(appointment.appointment_date)}</td>
                                        <td>
                                            <strong class="text-primary">${formatTime(appointment.appointment_time)}</strong>
                                        </td>
                                        <td>
                                            <span class="status-badge status-${appointment.status}">
                                                ${getStatusText(appointment.status)}
                                            </span>
                                        </td>
                                        <td>
                                            <small class="text-muted">${appointment.duration_minutes} min</small>
                                        </td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                `;

                container.innerHTML = html;
            }

            function formatTime(timeStr) {
                return timeStr.substring(0, 5); // HH:MM
            }

            function formatDate(dateStr) {
                const date = new Date(dateStr);
                return date.toLocaleDateString('pt-BR', {
                    weekday: 'short',
                    day: '2-digit',
                    month: '2-digit',
                    year: 'numeric'
                });
            }

            function formatDateTime(dateTimeStr) {
                const date = new Date(dateTimeStr);
                return date.toLocaleString('pt-BR');
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


# Endpoint de paciente removido - dados agora estão na tabela appointments


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

