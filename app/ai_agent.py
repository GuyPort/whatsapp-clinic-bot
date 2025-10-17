"""
Agente de IA com Claude SDK + Tools para agendamento de consultas.
Versão completa com menu estruturado e gerenciamento de contexto.
"""
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
import json
import logging
from anthropic import Anthropic

from sqlalchemy.orm import Session

from app.simple_config import settings
from app.models import Appointment, AppointmentStatus
from app.utils import (
    load_clinic_info, normalize_phone, parse_date_br, 
    format_datetime_br, now_brazil, get_brazil_timezone
)
from app.appointment_rules import appointment_rules

logger = logging.getLogger(__name__)


class ClaudeToolAgent:
    """Agente de IA com Claude SDK + Tools para agendamento de consultas"""
    
    def __init__(self):
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self.clinic_info = load_clinic_info()
        self.timezone = get_brazil_timezone()
        self.tools = self._define_tools()
        self.system_prompt = self._create_system_prompt()
        
    def _create_system_prompt(self) -> str:
        """Cria o prompt do sistema para o Claude"""
        clinic_name = self.clinic_info.get('nome_clinica', 'Clínica')
        endereco = self.clinic_info.get('endereco', 'Endereço não informado')
        horarios = self.clinic_info.get('horario_funcionamento', {})
        
        horarios_str = ""
        for dia, horario in horarios.items():
            if horario != "FECHADO":
                horarios_str += f"• {dia.capitalize()}: {horario}\n"
        
        return f"""Você é a assistente virtual da {clinic_name}.

INFORMAÇÕES DA CLÍNICA:
📍 Endereço: {endereco}
🕒 Horários de Funcionamento:
{horarios_str}

FLUXO DE ATENDIMENTO:
1. SEMPRE comece com a mensagem de boas-vindas e menu de 3 opções
2. Use as tools quando necessário para buscar informações ou criar/cancelar consultas
3. Mantenha conversas naturais e cordiais
4. Colete sempre: nome completo, telefone, data de nascimento, data e horário da consulta

MENU PADRÃO (sempre mostrar):
"Olá! Bem-vindo(a) à {clinic_name}! 😊

Como posso te ajudar hoje?

1️⃣ Marcar consulta
2️⃣ Remarcar/Cancelar consulta  
3️⃣ Tirar dúvidas

Digite o número da opção desejada."

REGRAS IMPORTANTES:
- Use as tools para verificar disponibilidade e criar consultas
- Sempre confirme os dados antes de agendar
- Seja cordial e profissional
- Mantenha o foco no atendimento médico"""

    def _define_tools(self) -> List[Dict]:
        """Define as tools disponíveis para o Claude"""
        return [
            {
                "name": "get_clinic_info",
                "description": "Obtém informações sobre a clínica (horários, endereço, valores)",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "info_type": {
                            "type": "string",
                            "description": "Tipo de informação desejada",
                            "enum": ["horarios", "endereco", "valores", "convenios", "todos"]
                        }
                    },
                    "required": ["info_type"]
                }
            },
            {
                "name": "check_availability",
                "description": "Verifica horários disponíveis para agendamento em uma data específica",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "Data no formato DD/MM/AAAA"
                        }
                    },
                    "required": ["date"]
                }
            },
            {
                "name": "create_appointment",
                "description": "Cria um novo agendamento de consulta",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_name": {"type": "string", "description": "Nome completo do paciente"},
                        "patient_phone": {"type": "string", "description": "Telefone do paciente"},
                        "patient_birth_date": {"type": "string", "description": "Data de nascimento DD/MM/AAAA"},
                        "appointment_date": {"type": "string", "description": "Data da consulta DD/MM/AAAA"},
                        "appointment_time": {"type": "string", "description": "Horário da consulta HH:MM"}
                    },
                    "required": ["patient_name", "patient_phone", "patient_birth_date", "appointment_date", "appointment_time"]
                }
            },
            {
                "name": "search_appointments",
                "description": "Busca consultas agendadas de um paciente",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_name": {"type": "string", "description": "Nome do paciente"},
                        "patient_birth_date": {"type": "string", "description": "Data de nascimento DD/MM/AAAA"}
                    },
                    "required": ["patient_name", "patient_birth_date"]
                }
            },
            {
                "name": "cancel_appointment",
                "description": "Cancela uma consulta agendada",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {"type": "integer", "description": "ID da consulta a ser cancelada"},
                        "reason": {"type": "string", "description": "Motivo do cancelamento"}
                    },
                    "required": ["appointment_id"]
                }
            }
        ]
    
    async def process_message(self, phone: str, message: str, db: Session) -> str:
        """
        Processa mensagem usando Claude com Tools.
        Mantém contexto através do histórico de mensagens.
        """
        try:
            # Normalizar telefone
            phone = normalize_phone(phone)
            
            # Buscar histórico de conversa (últimas 10 mensagens)
            conversation_history = self._get_conversation_history(phone, db)
            
            # Adicionar mensagem atual
            conversation_history.append({"role": "user", "content": message})
            
            # Fazer chamada ao Claude
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4096,
                system=self.system_prompt,
                tools=self.tools,
                messages=conversation_history
            )
            
            # Processar tool calls se houver
            if response.stop_reason == "tool_use":
                tool_results = []
                for content in response.content:
                    if content.type == "tool_use":
                        tool_result = await self._execute_tool(
                            content.name, 
                            content.input, 
                            phone,
                            db
                        )
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": content.id,
                            "content": tool_result
                        })
                
                # Continuar conversa com resultados das tools
                final_response = self.client.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    max_tokens=4096,
                    system=self.system_prompt,
                    tools=self.tools,
                    messages=conversation_history + [
                        {"role": "assistant", "content": response.content},
                        {"role": "user", "content": tool_results}
                    ]
                )
                
                return final_response.content[0].text
            
            # Resposta direta (sem tool calls)
            return response.content[0].text
            
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {str(e)}")
            return "Desculpe, ocorreu um erro. Tente novamente."
    
    def _get_conversation_history(self, phone: str, db: Session) -> List[Dict]:
        """
        Busca histórico de conversa do paciente.
        Por simplicidade, retorna apenas a mensagem de boas-vindas.
        Em uma implementação completa, salvaria no banco.
        """
        # Por enquanto, sempre começar com mensagem de boas-vindas
        # Em produção, salvaria histórico no banco de dados
        return []
    
    async def _execute_tool(self, tool_name: str, tool_input: Dict, phone: str, db: Session) -> str:
        """Executa uma tool específica"""
        try:
            if tool_name == "get_clinic_info":
                return self._handle_get_clinic_info(tool_input)
            elif tool_name == "check_availability":
                return self._handle_check_availability(tool_input, db)
            elif tool_name == "create_appointment":
                return self._handle_create_appointment(tool_input, phone, db)
            elif tool_name == "search_appointments":
                return self._handle_search_appointments(tool_input, db)
            elif tool_name == "cancel_appointment":
                return self._handle_cancel_appointment(tool_input, db)
            else:
                return f"Tool '{tool_name}' não reconhecida."
                
        except Exception as e:
            logger.error(f"Erro ao executar tool {tool_name}: {str(e)}")
            return f"Erro ao executar {tool_name}: {str(e)}"
    
    def _handle_get_clinic_info(self, tool_input: Dict) -> str:
        """Tool: get_clinic_info"""
        info_type = tool_input.get("info_type", "todos")
        
        if info_type == "horarios":
            horarios = self.clinic_info.get('horario_funcionamento', {})
            response = "🕒 Horários de Funcionamento:\n"
            for dia, horario in horarios.items():
                if horario != "FECHADO":
                    response += f"• {dia.capitalize()}: {horario}\n"
            return response
        
        elif info_type == "endereco":
            return f"📍 Endereço: {self.clinic_info.get('endereco', 'Não informado')}"
        
        elif info_type == "valores":
            return "💰 Valores: Consulte diretamente na clínica para informações sobre valores."
        
        else:  # todos
            clinic_name = self.clinic_info.get('nome_clinica', 'Clínica')
            endereco = self.clinic_info.get('endereco', 'Não informado')
            horarios = self.clinic_info.get('horario_funcionamento', {})
            
            response = f"📋 Informações da {clinic_name}:\n\n"
            response += f"📍 Endereço: {endereco}\n\n"
            response += "🕒 Horários de Funcionamento:\n"
            for dia, horario in horarios.items():
                if horario != "FECHADO":
                    response += f"• {dia.capitalize()}: {horario}\n"
            
            return response
    
    def _handle_check_availability(self, tool_input: Dict, db: Session) -> str:
        """Tool: check_availability"""
        try:
            date_str = tool_input.get("date")
            if not date_str:
                return "Data não fornecida."
            
            # Converter data
            appointment_date = parse_date_br(date_str)
            if not appointment_date:
                return "Data inválida. Use o formato DD/MM/AAAA."
            
            # Buscar horários disponíveis
            available_slots = appointment_rules.get_available_slots(
                appointment_date, 
                30,  # 30 minutos de duração
                db,
                limit=5
            )
            
            if not available_slots:
                return f"Não há horários disponíveis para {date_str}. Tente outra data."
            
            response = f"Horários disponíveis para {date_str}:\n\n"
            for i, slot in enumerate(available_slots, 1):
                response += f"{i} - {slot.strftime('%H:%M')}\n"
            
            response += "\nQual horário prefere? (Digite o número)"
            return response
            
        except Exception as e:
            logger.error(f"Erro ao verificar disponibilidade: {str(e)}")
            return "Erro ao verificar disponibilidade. Tente novamente."
    
    def _handle_create_appointment(self, tool_input: Dict, phone: str, db: Session) -> str:
        """Tool: create_appointment"""
        try:
            # Extrair dados
            patient_name = tool_input.get("patient_name")
            patient_phone = tool_input.get("patient_phone", phone)
            patient_birth_date = tool_input.get("patient_birth_date")
            appointment_date_str = tool_input.get("appointment_date")
            appointment_time_str = tool_input.get("appointment_time")
            
            # Validar dados obrigatórios
            if not all([patient_name, patient_birth_date, appointment_date_str, appointment_time_str]):
                return "Dados incompletos para criar agendamento."
            
            # Converter datas
            appointment_date = parse_date_br(appointment_date_str)
            if not appointment_date:
                return "Data inválida. Use DD/MM/AAAA."
            
            try:
                appointment_time = datetime.strptime(appointment_time_str, "%H:%M").time()
            except ValueError:
                return "Horário inválido. Use HH:MM."
            
            # Verificar se horário está disponível
            is_valid, error_msg = appointment_rules.is_valid_appointment_date(
                datetime.combine(appointment_date, appointment_time)
            )
            if not is_valid:
                return f"Horário inválido: {error_msg}"
            
            # Verificar conflitos
            existing = db.query(Appointment).filter(
                Appointment.appointment_date == appointment_date,
                Appointment.appointment_time == appointment_time,
                Appointment.status == AppointmentStatus.AGENDADA
            ).first()
            
            if existing:
                return "Este horário já está ocupado. Escolha outro horário."
            
            # Criar consulta
            appointment = Appointment(
                patient_name=patient_name,
                patient_phone=normalize_phone(patient_phone),
                patient_birth_date=patient_birth_date,
                appointment_date=appointment_date,
                appointment_time=appointment_time,
                status=AppointmentStatus.AGENDADA,
                duration_minutes=30
            )
            
            db.add(appointment)
            db.commit()
            
            return f"""✅ Consulta agendada com sucesso!

📅 Dia: {appointment_date.strftime('%d/%m/%Y')}
🕐 Horário: {appointment_time.strftime('%H:%M')}
👤 Paciente: {patient_name}
📱 Telefone: {normalize_phone(patient_phone)}

Te enviamos um lembrete 1 dia antes! 😊"""
            
        except Exception as e:
            logger.error(f"Erro ao criar agendamento: {str(e)}")
            return "Erro ao criar agendamento. Tente novamente."
    
    def _handle_search_appointments(self, tool_input: Dict, db: Session) -> str:
        """Tool: search_appointments"""
        try:
            patient_name = tool_input.get("patient_name")
            patient_birth_date = tool_input.get("patient_birth_date")
            
            if not patient_name:
                return "Nome do paciente não fornecido."
            
            # Buscar consultas
            query = db.query(Appointment).filter(
                Appointment.patient_name.ilike(f"%{patient_name}%"),
                Appointment.status == AppointmentStatus.AGENDADA
            )
            
            if patient_birth_date:
                query = query.filter(Appointment.patient_birth_date == patient_birth_date)
            
            appointments = query.all()
            
            if not appointments:
                return "Nenhuma consulta agendada encontrada. Verifique os dados fornecidos."
            
            response = f"Encontrei {len(appointments)} consulta(s) agendada(s):\n\n"
            for i, apt in enumerate(appointments, 1):
                response += f"{i} - {apt.appointment_date.strftime('%d/%m/%Y')} às {apt.appointment_time.strftime('%H:%M')}\n"
            
            response += "\nQual consulta deseja cancelar? (Digite o número)"
            return response
            
        except Exception as e:
            logger.error(f"Erro ao buscar consultas: {str(e)}")
            return "Erro ao buscar consultas. Tente novamente."
    
    def _handle_cancel_appointment(self, tool_input: Dict, db: Session) -> str:
        """Tool: cancel_appointment"""
        try:
            appointment_id = tool_input.get("appointment_id")
            reason = tool_input.get("reason", "Cancelado pelo paciente")
            
            if not appointment_id:
                return "ID da consulta não fornecido."
            
            # Buscar consulta
            appointment = db.query(Appointment).filter(
                Appointment.id == appointment_id,
                Appointment.status == AppointmentStatus.AGENDADA
            ).first()
            
            if not appointment:
                return "Consulta não encontrada ou já cancelada."
            
            # Cancelar consulta
            appointment.status = AppointmentStatus.CANCELADA
            appointment.cancelled_at = now_brazil()
            appointment.cancelled_reason = reason
            appointment.updated_at = now_brazil()
            
            db.commit()
            
            return f"""✅ Consulta cancelada com sucesso!

📅 Era para: {appointment.appointment_date.strftime('%d/%m/%Y')} às {appointment.appointment_time.strftime('%H:%M')}
👤 Paciente: {appointment.patient_name}

Se precisar reagendar, é só me avisar! 😊"""
            
        except Exception as e:
            logger.error(f"Erro ao cancelar consulta: {str(e)}")
            return "Erro ao cancelar consulta. Tente novamente."


# Instância global do agente
ai_agent = ClaudeToolAgent()
