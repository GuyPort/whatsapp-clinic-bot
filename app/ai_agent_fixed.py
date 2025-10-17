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
        
        duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 45)
        
        return f"""Você é a assistente virtual da {clinic_name}.

INFORMAÇÕES DA CLÍNICA:
📍 Endereço: {endereco}
⏰ Horários de funcionamento:
{horarios_str}
⏱️ Duração das consultas: {duracao} minutos
📞 Telefone: {self.clinic_info.get('telefone', 'Não informado')}

MENU PRINCIPAL:
Quando o paciente enviar qualquer mensagem, SEMPRE responda com este menu:

"Olá! Bem-vindo(a) à {clinic_name}! 😊
Como posso te ajudar hoje?

⿡ Marcar consulta
⿢ Remarcar/Cancelar consulta  
⿣ Tirar dúvidas

Digite o número da opção desejada."

FLUXO DE AGENDAMENTO (SEQUENCIAL):
Quando o paciente escolher "1 - Marcar consulta", siga EXATAMENTE este fluxo:

1. "Perfeito! Vamos marcar sua consulta. 😊
   Primeiro, me informe seu nome completo:"

2. Após receber o nome:
   "Obrigado! Agora me informe sua data de nascimento (DD/MM/AAAA):"

3. Após receber a data de nascimento:
   "Perfeito! Agora me informe o dia que gostaria de marcar a consulta (DD/MM/AAAA):"

4. Após receber a data desejada:
   "Ótimo! E que horário você prefere? (HH:MM - ex: 14:30):"

5. Após receber o horário:
   - Use a tool validate_business_hours para verificar se o horário está dentro do funcionamento
   - Se válido, use check_availability para verificar disponibilidade
   - Se disponível, use create_appointment para criar o agendamento
   - Se não disponível, mostre horários alternativos

REGRAS IMPORTANTES:
- SEMPRE peça UMA informação por vez
- NUNCA peça nome, data de nascimento, data e horário na mesma mensagem
- Use as tools disponíveis para validar horários e disponibilidade
- Seja sempre educada e prestativa
- Confirme os dados antes de finalizar o agendamento

FERRAMENTAS DISPONÍVEIS:
- get_clinic_info: Obter informações da clínica
- validate_business_hours: Validar se horário está dentro do funcionamento
- check_availability: Verificar horários disponíveis
- create_appointment: Criar novo agendamento
- search_appointments: Buscar agendamentos existentes
- cancel_appointment: Cancelar agendamento

Lembre-se: Seja sempre educada, prestativa e siga o fluxo sequencial!"""

    def _define_tools(self) -> List[Dict]:
        """Define as tools disponíveis para o Claude"""
        return [
            {
                "name": "get_clinic_info",
                "description": "Obter informações da clínica (horários, endereço, etc.)",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "validate_business_hours",
                "description": "Validar se um horário está dentro do funcionamento da clínica",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "Data no formato DD/MM/AAAA"
                        },
                        "time": {
                            "type": "string", 
                            "description": "Horário no formato HH:MM"
                        }
                    },
                    "required": ["date", "time"]
                }
            },
            {
                "name": "check_availability",
                "description": "Verificar horários disponíveis para agendamento em uma data específica",
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
                "description": "Criar um novo agendamento de consulta",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_name": {
                            "type": "string",
                            "description": "Nome completo do paciente"
                        },
                        "patient_phone": {
                            "type": "string",
                            "description": "Telefone do paciente"
                        },
                        "patient_birth_date": {
                            "type": "string",
                            "description": "Data de nascimento no formato DD/MM/AAAA"
                        },
                        "appointment_date": {
                            "type": "string",
                            "description": "Data da consulta no formato DD/MM/AAAA"
                        },
                        "appointment_time": {
                            "type": "string",
                            "description": "Horário da consulta no formato HH:MM"
                        },
                        "notes": {
                            "type": "string",
                            "description": "Observações adicionais (opcional)"
                        }
                    },
                    "required": ["patient_name", "patient_phone", "patient_birth_date", "appointment_date", "appointment_time"]
                }
            },
            {
                "name": "search_appointments",
                "description": "Buscar agendamentos por telefone ou nome do paciente",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "phone": {
                            "type": "string",
                            "description": "Telefone do paciente para buscar"
                        },
                        "name": {
                            "type": "string",
                            "description": "Nome do paciente para buscar"
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "cancel_appointment",
                "description": "Cancelar um agendamento existente",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {
                            "type": "integer",
                            "description": "ID do agendamento a ser cancelado"
                        },
                        "reason": {
                            "type": "string",
                            "description": "Motivo do cancelamento"
                        }
                    },
                    "required": ["appointment_id", "reason"]
                }
            }
        ]

    def process_message(self, message: str, phone: str, db: Session) -> str:
        """Processa uma mensagem do usuário e retorna a resposta"""
        try:
            # Preparar mensagem para o Claude
            user_message = f"Telefone do paciente: {phone}\n\nMensagem: {message}"
            
            # Fazer chamada para o Claude
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=2000,
                temperature=0.1,
                system=self.system_prompt,
                messages=[{"role": "user", "content": user_message}],
                tools=self.tools
            )
            
            # Processar resposta
            if response.content:
                content = response.content[0]
                
                if content.type == "text":
                    return content.text
                elif content.type == "tool_use":
                    # Executar tool
                    tool_result = self._execute_tool(content.name, content.input, db)
                    
                    # Fazer follow-up com o resultado
                    follow_up = self.client.messages.create(
                        model="claude-3-5-sonnet-20241022",
                        max_tokens=1000,
                        temperature=0.1,
                        system=self.system_prompt,
                        messages=[
                            {"role": "user", "content": user_message},
                            {"role": "assistant", "content": response.content},
                            {"role": "user", "content": f"Resultado da tool {content.name}: {tool_result}"}
                        ]
                    )
                    
                    if follow_up.content and follow_up.content[0].type == "text":
                        return follow_up.content[0].text
                    else:
                        return tool_result
                        
            return "Desculpe, não consegui processar sua mensagem. Tente novamente."
            
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {str(e)}")
            return "Desculpe, ocorreu um erro. Tente novamente em alguns instantes."

    def _execute_tool(self, tool_name: str, tool_input: Dict, db: Session) -> str:
        """Executa uma tool específica"""
        try:
            if tool_name == "get_clinic_info":
                return self._handle_get_clinic_info(tool_input)
            elif tool_name == "validate_business_hours":
                return self._handle_validate_business_hours(tool_input)
            elif tool_name == "check_availability":
                return self._handle_check_availability(tool_input, db)
            elif tool_name == "create_appointment":
                return self._handle_create_appointment(tool_input, db)
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
        try:
            clinic_name = self.clinic_info.get('nome_clinica', 'Clínica')
            endereco = self.clinic_info.get('endereco', 'Endereço não informado')
            telefone = self.clinic_info.get('telefone', 'Não informado')
            
            response = f"🏥 **{clinic_name}**\n\n"
            response += f"📍 **Endereço:** {endereco}\n"
            response += f"📞 **Telefone:** {telefone}\n\n"
            response += "⏰ **Horários de funcionamento:**\n"
            response += self._format_business_hours()
            
            return response
        except Exception as e:
            logger.error(f"Erro ao obter informações da clínica: {str(e)}")
            return f"Erro ao obter informações: {str(e)}"

    def _handle_validate_business_hours(self, tool_input: Dict) -> str:
        """Tool: validate_business_hours"""
        try:
            date_str = tool_input.get("date")
            time_str = tool_input.get("time")
            
            if not date_str or not time_str:
                return "Data e horário são obrigatórios."
            
            # Converter data
            appointment_date = parse_date_br(date_str)
            if not appointment_date:
                return "Data inválida. Use o formato DD/MM/AAAA."
            
            # Obter dia da semana
            weekday = appointment_date.strftime('%A').lower()
            weekday_map = {
                'monday': 'segunda',
                'tuesday': 'terca', 
                'wednesday': 'quarta',
                'thursday': 'quinta',
                'friday': 'sexta',
                'saturday': 'sabado',
                'sunday': 'domingo'
            }
            weekday_pt = weekday_map.get(weekday, weekday)
            
            # Verificar horários de funcionamento
            horarios = self.clinic_info.get('horario_funcionamento', {})
            horario_dia = horarios.get(weekday_pt, "FECHADO")
            
            if horario_dia == "FECHADO":
                return f"❌ A clínica não funciona aos {weekday_pt}s. Horários de funcionamento:\n" + \
                       self._format_business_hours()
            
            # Verificar se horário está dentro do funcionamento
            try:
                hora_consulta = datetime.strptime(time_str, '%H:%M').time()
                hora_inicio, hora_fim = horario_dia.split('-')
                hora_inicio = datetime.strptime(hora_inicio, '%H:%M').time()
                hora_fim = datetime.strptime(hora_fim, '%H:%M').time()
                
                if hora_inicio <= hora_consulta <= hora_fim:
                    return f"✅ Horário válido! A clínica funciona das {hora_inicio.strftime('%H:%M')} às {hora_fim.strftime('%H:%M')} aos {weekday_pt}s."
                else:
                    return f"❌ Horário inválido! A clínica funciona das {hora_inicio.strftime('%H:%M')} às {hora_fim.strftime('%H:%M')} aos {weekday_pt}s.\n" + \
                           f"Por favor, escolha um horário entre {hora_inicio.strftime('%H:%M')} e {hora_fim.strftime('%H:%M')}."
                           
            except ValueError:
                return "Formato de horário inválido. Use HH:MM (ex: 14:30)."
                
        except Exception as e:
            logger.error(f"Erro ao validar horário: {str(e)}")
            return f"Erro ao validar horário: {str(e)}"

    def _format_business_hours(self) -> str:
        """Formata horários de funcionamento para exibição"""
        horarios = self.clinic_info.get('horario_funcionamento', {})
        response = ""
        
        for dia, horario in horarios.items():
            if horario != "FECHADO":
                response += f"• {dia.capitalize()}: {horario}\n"
        
        return response

    def _handle_check_availability(self, tool_input: Dict, db: Session) -> str:
        """Tool: check_availability"""
        try:
            date_str = tool_input.get("date")
            if not date_str:
                return "Data é obrigatória."
            
            # Converter data
            appointment_date = parse_date_br(date_str)
            if not appointment_date:
                return "Data inválida. Use o formato DD/MM/AAAA."
            
            # Obter horários disponíveis
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 45)
            available_slots = appointment_rules.get_available_slots(appointment_date, db, duracao)
            
            if not available_slots:
                return f"❌ Não há horários disponíveis para {appointment_date.strftime('%d/%m/%Y')}.\n" + \
                       "Por favor, escolha outra data."
            
            response = f"✅ Horários disponíveis para {appointment_date.strftime('%d/%m/%Y')}:\n\n"
            for i, slot in enumerate(available_slots, 1):
                response += f"{i}. {slot.strftime('%H:%M')}\n"
            
            response += f"\n⏱️ Duração: {duracao} minutos\n"
            response += "Escolha um horário e me informe o número da opção desejada."
            
            return response
            
        except Exception as e:
            logger.error(f"Erro ao verificar disponibilidade: {str(e)}")
            return f"Erro ao verificar disponibilidade: {str(e)}"

    def _handle_create_appointment(self, tool_input: Dict, db: Session) -> str:
        """Tool: create_appointment"""
        try:
            patient_name = tool_input.get("patient_name")
            patient_phone = tool_input.get("patient_phone")
            patient_birth_date = tool_input.get("patient_birth_date")
            appointment_date = tool_input.get("appointment_date")
            appointment_time = tool_input.get("appointment_time")
            notes = tool_input.get("notes", "")
            
            if not all([patient_name, patient_phone, patient_birth_date, appointment_date, appointment_time]):
                return "Todos os campos obrigatórios devem ser preenchidos."
            
            # Normalizar telefone
            normalized_phone = normalize_phone(patient_phone)
            
            # Converter datas
            birth_date = parse_date_br(patient_birth_date)
            appointment_datetime = parse_date_br(appointment_date)
            
            if not birth_date or not appointment_datetime:
                return "Formato de data inválido. Use DD/MM/AAAA."
            
            # Combinar data e horário
            try:
                time_obj = datetime.strptime(appointment_time, '%H:%M').time()
                appointment_datetime = datetime.combine(appointment_datetime.date(), time_obj)
            except ValueError:
                return "Formato de horário inválido. Use HH:MM."
            
            # Verificar se horário está disponível
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 45)
            available_slots = appointment_rules.get_available_slots(appointment_datetime.date(), db, duracao)
            
            if appointment_datetime.time() not in [slot.time() for slot in available_slots]:
                return f"❌ Horário {appointment_time} não está disponível. Use a tool check_availability para ver horários disponíveis."
            
            # Criar agendamento
            appointment = Appointment(
                patient_name=patient_name,
                patient_phone=normalized_phone,
                patient_birth_date=birth_date,
                appointment_datetime=appointment_datetime,
                duration_minutes=duracao,
                status=AppointmentStatus.AGENDADA,
                notes=notes
            )
            
            db.add(appointment)
            db.commit()
            
            return f"✅ **Agendamento realizado com sucesso!**\n\n" + \
                   f"👤 **Paciente:** {patient_name}\n" + \
                   f"📅 **Data:** {appointment_datetime.strftime('%d/%m/%Y')}\n" + \
                   f"⏰ **Horário:** {appointment_datetime.strftime('%H:%M')}\n" + \
                   f"⏱️ **Duração:** {duracao} minutos\n" + \
                   f"📞 **Telefone:** {normalized_phone}\n\n" + \
                   "Obrigado por escolher nossa clínica! 😊"
                   
        except Exception as e:
            logger.error(f"Erro ao criar agendamento: {str(e)}")
            db.rollback()
            return f"Erro ao criar agendamento: {str(e)}"

    def _handle_search_appointments(self, tool_input: Dict, db: Session) -> str:
        """Tool: search_appointments"""
        try:
            phone = tool_input.get("phone")
            name = tool_input.get("name")
            
            if not phone and not name:
                return "Informe o telefone ou nome do paciente para buscar."
            
            query = db.query(Appointment)
            
            if phone:
                normalized_phone = normalize_phone(phone)
                query = query.filter(Appointment.patient_phone == normalized_phone)
            
            if name:
                query = query.filter(Appointment.patient_name.ilike(f"%{name}%"))
            
            appointments = query.order_by(Appointment.appointment_datetime.desc()).all()
            
            if not appointments:
                return "Nenhum agendamento encontrado."
            
            response = f"📅 **Agendamentos encontrados:**\n\n"
            
            for i, apt in enumerate(appointments, 1):
                status_emoji = {
                    AppointmentStatus.AGENDADA: "✅",
                    AppointmentStatus.CANCELADA: "❌",
                    AppointmentStatus.REALIZADA: "✅"
                }.get(apt.status, "❓")
                
                response += f"{i}. {status_emoji} **{apt.patient_name}**\n"
                response += f"   📅 {apt.appointment_datetime.strftime('%d/%m/%Y às %H:%M')}\n"
                response += f"   📞 {apt.patient_phone}\n"
                response += f"   📝 Status: {apt.status.value}\n"
                if apt.notes:
                    response += f"   💬 {apt.notes}\n"
                response += "\n"
            
            return response
            
        except Exception as e:
            logger.error(f"Erro ao buscar agendamentos: {str(e)}")
            return f"Erro ao buscar agendamentos: {str(e)}"

    def _handle_cancel_appointment(self, tool_input: Dict, db: Session) -> str:
        """Tool: cancel_appointment"""
        try:
            appointment_id = tool_input.get("appointment_id")
            reason = tool_input.get("reason")
            
            if not appointment_id or not reason:
                return "ID do agendamento e motivo são obrigatórios."
            
            appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()
            
            if not appointment:
                return "Agendamento não encontrado."
            
            if appointment.status == AppointmentStatus.CANCELADA:
                return "Este agendamento já foi cancelado."
            
            # Cancelar agendamento
            appointment.status = AppointmentStatus.CANCELADA
            appointment.cancelled_at = now_brazil()
            appointment.cancelled_reason = reason
            appointment.updated_at = now_brazil()
            
            db.commit()
            
            return f"✅ **Agendamento cancelado com sucesso!**\n\n" + \
                   f"👤 **Paciente:** {appointment.patient_name}\n" + \
                   f"📅 **Data:** {appointment.appointment_datetime.strftime('%d/%m/%Y às %H:%M')}\n" + \
                   f"📝 **Motivo:** {reason}\n\n" + \
                   "Se precisar reagendar, estarei aqui para ajudar! 😊"
                   
        except Exception as e:
            logger.error(f"Erro ao cancelar agendamento: {str(e)}")
            db.rollback()
            return f"Erro ao cancelar agendamento: {str(e)}"


# Instância global do agente
ai_agent = ClaudeToolAgent()
