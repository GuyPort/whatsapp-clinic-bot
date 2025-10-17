"""
Agente de IA simplificado para agendamento de consultas.
Versão sem contexto de conversa - apenas agendamento direto.
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import json
import logging
from anthropic import Anthropic

from sqlalchemy.orm import Session

from app.simple_config import settings
from app.models import Appointment
from app.utils import (
    load_clinic_info, normalize_phone, extract_name_from_message,
    extract_date_from_message, parse_date_br, is_valid_birth_date,
    format_datetime_br, detect_frustration_keywords,
    detect_inappropriate_language, now_brazil, format_currency,
    parse_weekday_from_message, get_brazil_timezone
)
from app.appointment_rules import appointment_rules

logger = logging.getLogger(__name__)


class AIAgent:
    """Agente de IA simplificado para agendamento de consultas"""
    
    def __init__(self):
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self.clinic_info = load_clinic_info()
        self.timezone = get_brazil_timezone()
        
    async def process_message(self, phone: str, message: str, db: Session) -> str:
        """
        Processa mensagem e retorna resposta.
        Versão simplificada que foca apenas em agendamento.
        """
        try:
            # Normalizar telefone
            phone = normalize_phone(phone)
            
            # Detectar intenção da mensagem
            intent = self._detect_intent(message)
            
            if intent == "agendar":
                return await self._handle_appointment_request(phone, message, db)
            elif intent == "cancelar":
                return await self._handle_cancel_request(phone, message, db)
            elif intent == "duvidas":
                return self._handle_questions(message)
            else:
                return self._get_greeting()
                
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {str(e)}")
            return "Desculpe, ocorreu um erro. Tente novamente."
    
    def _detect_intent(self, message: str) -> str:
        """Detecta a intenção da mensagem"""
        message_lower = message.lower()
        
        if any(word in message_lower for word in ["agendar", "marcar", "consulta", "horário"]):
            return "agendar"
        elif any(word in message_lower for word in ["cancelar", "desmarcar"]):
            return "cancelar"
        elif any(word in message_lower for word in ["dúvida", "pergunta", "horário", "funcionamento"]):
            return "duvidas"
        else:
            return "greeting"
    
    async def _handle_appointment_request(self, phone: str, message: str, db: Session) -> str:
        """Processa solicitação de agendamento"""
        try:
            # Extrair nome da mensagem
            name = extract_name_from_message(message)
            if not name:
                return "Para agendar uma consulta, preciso do seu nome completo. Por favor, me informe seu nome."
            
            # Extrair data da mensagem
            date_str = extract_date_from_message(message)
            if not date_str:
                return f"Olá {name}! Para agendar sua consulta, preciso saber em que data você gostaria de ser atendido. Por favor, me informe a data desejada (ex: 25/10/2025)."
            
            # Validar e converter data
            try:
                appointment_date = parse_date_br(date_str)
                if not appointment_date:
                    return "Data inválida. Por favor, informe a data no formato DD/MM/AAAA (ex: 25/10/2025)."
            except Exception:
                return "Data inválida. Por favor, informe a data no formato DD/MM/AAAA (ex: 25/10/2025)."
            
            # Solicitar horário
            return f"Perfeito {name}! Você gostaria de agendar para {appointment_date.strftime('%d/%m/%Y')}. Qual horário você prefere? (ex: 14:30)"
            
        except Exception as e:
            logger.error(f"Erro ao processar agendamento: {str(e)}")
            return "Desculpe, ocorreu um erro ao processar seu agendamento. Tente novamente."
    
    async def _handle_cancel_request(self, phone: str, message: str, db: Session) -> str:
        """Processa solicitação de cancelamento"""
        # Buscar consultas do paciente
        appointments = db.query(Appointment).filter(
            Appointment.patient_name.ilike(f"%{phone}%")
        ).all()
        
        if not appointments:
            return "Não encontrei consultas agendadas para você. Verifique se o nome está correto."
        
        # Listar consultas
        response = "Encontrei as seguintes consultas:\n\n"
        for i, apt in enumerate(appointments, 1):
            response += f"{i}. {apt.patient_name} - {apt.appointment_date.strftime('%d/%m/%Y')} às {apt.appointment_time.strftime('%H:%M')}\n"
        
        response += "\nQual consulta você gostaria de cancelar? Informe o número."
        return response
    
    def _handle_questions(self, message: str) -> str:
        """Responde dúvidas sobre a clínica"""
        clinic_name = self.clinic_info.get('nome', 'Nossa Clínica')
        horarios = self.clinic_info.get('horario_funcionamento', {})
        
        response = f"📋 Informações sobre {clinic_name}:\n\n"
        response += "🕒 Horários de Funcionamento:\n"
        
        for dia, horario in horarios.items():
            if horario != "FECHADO":
                response += f"• {dia.capitalize()}: {horario}\n"
        
        response += f"\n📞 Telefone: {self.clinic_info.get('telefone', 'N/A')}\n"
        response += f"📍 Endereço: {self.clinic_info.get('endereco', 'N/A')}\n\n"
        response += "Para agendar uma consulta, me informe seu nome e a data desejada!"
        
        return response
    
    def _get_greeting(self) -> str:
        """Retorna mensagem de boas-vindas"""
        clinic_name = self.clinic_info.get('nome', 'Nossa Clínica')
        
        return f"""👋 Olá! Bem-vindo(a) à {clinic_name}!

Como posso ajudá-lo(a) hoje?

• Para agendar uma consulta, me informe seu nome e a data desejada
• Para cancelar uma consulta, me informe seu nome
• Para tirar dúvidas, é só perguntar!

Estou aqui para ajudar! 😊"""


# Instância global do agente
ai_agent = AIAgent()
