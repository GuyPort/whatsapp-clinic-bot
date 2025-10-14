"""
Agente de IA usando Claude para processar mensagens e gerenciar conversas.
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import json
import logging
from anthropic import Anthropic

from sqlalchemy.orm import Session

from app.simple_config import settings
from app.models import (
    Patient, Appointment, ConversationContext,
    AppointmentStatus, ConversationState
)
from app.utils import (
    load_clinic_info, normalize_phone, extract_name_from_message,
    extract_date_from_message, parse_date_br, is_valid_birth_date,
    format_datetime_br, detect_frustration_keywords,
    detect_inappropriate_language, now_brazil, format_currency,
    parse_weekday_from_message
)
from app.appointment_rules import appointment_rules
from app.calendar_service import calendar_service

logger = logging.getLogger(__name__)


class AIAgent:
    """Agente de IA para gerenciar conversas e agendamentos"""
    
    def __init__(self):
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self.model = "claude-3-5-sonnet-20241022"
        self.clinic_info = load_clinic_info()
    
    def reload_clinic_info(self):
        """Recarrega informações da clínica"""
        self.clinic_info = load_clinic_info()
        appointment_rules.reload_clinic_info()
    
    def _build_system_prompt(self) -> str:
        """Constrói o system prompt com informações da clínica"""
        clinic_info_str = json.dumps(self.clinic_info, indent=2, ensure_ascii=False)
        
        return f"""Você é um assistente virtual de uma clínica médica no Brasil. Seu nome é CliniBot.

INFORMAÇÕES DA CLÍNICA:
{clinic_info_str}

SUAS RESPONSABILIDADES:
1. Responder dúvidas sobre a clínica (valores, horários, endereço, tipos de consulta, convênios)
2. Auxiliar no agendamento de consultas
3. Auxiliar no cancelamento e remarcação de consultas
4. Escalar para atendimento humano quando necessário

REGRAS IMPORTANTES:
- Sempre seja cordial, respeitoso e profissional
- Use português brasileiro
- NUNCA dê orientação médica ou diagnósticos
- NUNCA responda perguntas que não sejam sobre a clínica ou agendamentos
- Para agendar, cancelar ou remarcar, você SEMPRE precisa do nome completo e data de nascimento do paciente
- Se o paciente perguntar algo fora do escopo (política, piadas, etc), educadamente redirecione para o assunto da clínica
- Mantenha respostas curtas e diretas (máximo 3-4 linhas)
- Use linguagem natural e amigável, evite ser muito formal

QUANDO ESCALAR PARA HUMANO:
- Paciente solicita explicitamente falar com humano
- Frustração persistente ou linguagem inapropriada
- Solicitações que você não pode resolver
- Emergências médicas

PROCESSO DE AGENDAMENTO:
1. Perguntar que tipo de consulta deseja
2. Perguntar nome completo
3. Perguntar data de nascimento (formato DD/MM/AAAA)
4. Perguntar qual dia tem disponibilidade (dia da semana ou data específica)
5. Oferecer 3 horários disponíveis
6. Confirmar o agendamento

PROCESSO DE CANCELAMENTO/REMARCAÇÃO:
1. Perguntar nome completo e data de nascimento
2. Buscar consultas agendadas
3. Confirmar qual consulta
4. Executar ação solicitada

Responda sempre de forma natural, como um atendente humano profissional faria."""
    
    async def process_message(
        self,
        phone: str,
        message_text: str,
        db: Session
    ) -> str:
        """
        Processa uma mensagem recebida e retorna a resposta.
        
        Args:
            phone: Número de telefone do remetente
            message_text: Texto da mensagem
            db: Sessão do banco de dados
            
        Returns:
            Resposta a ser enviada
        """
        try:
            # Normalizar telefone
            phone = normalize_phone(phone)
            
            # Verificar se deve escalar para humano
            if self._should_escalate(message_text):
                return self._handle_escalation(phone, db)
            
            # Buscar ou criar contexto da conversa
            context = self._get_or_create_context(phone, db)
            
            # Buscar ou criar paciente
            patient = db.query(Patient).filter(Patient.phone == phone).first()
            
            # Atualizar contexto
            context.last_message_at = now_brazil()
            context.message_count += 1
            
            # Processar baseado no estado
            response = await self._process_by_state(
                context, patient, message_text, db
            )
            
            db.commit()
            
            return response
            
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {str(e)}", exc_info=True)
            return "Desculpe, ocorreu um erro ao processar sua mensagem. Por favor, tente novamente."
    
    def _should_escalate(self, message: str) -> bool:
        """Verifica se deve escalar para humano"""
        message_lower = message.lower()
        
        # Solicitação explícita
        explicit_keywords = ['falar com', 'atendente', 'humano', 'pessoa']
        if any(keyword in message_lower for keyword in explicit_keywords):
            return True
        
        # Frustração ou linguagem inadequada
        if detect_frustration_keywords(message) or detect_inappropriate_language(message):
            return True
        
        return False
    
    def _handle_escalation(self, phone: str, db: Session) -> str:
        """Trata escalação para humano"""
        context = self._get_or_create_context(phone, db)
        context.state = ConversationState.ESCALATED
        db.commit()
        
        clinic_name = self.clinic_info.get('nome_clinica', 'nossa clínica')
        contact = self.clinic_info.get('telefone_contato', '')
        
        message = f"Entendo! Vou transferir você para nossa equipe de atendimento.\n\n"
        
        # Horário de atendimento
        horarios = self.clinic_info.get('horario_funcionamento', {})
        message += f"📞 Telefone: {contact}\n\n"
        message += "Horário de atendimento:\n"
        message += f"Seg-Sex: {horarios.get('segunda', 'N/A')}\n"
        message += f"Sábado: {horarios.get('sabado', 'N/A')}\n"
        
        return message
    
    def _get_or_create_context(self, phone: str, db: Session) -> ConversationContext:
        """Busca ou cria contexto de conversa"""
        context = db.query(ConversationContext).filter(
            ConversationContext.phone == phone
        ).first()
        
        if not context:
            context = ConversationContext(
                phone=phone,
                state=ConversationState.IDLE,
                context_data="{}",
                message_count=0
            )
            db.add(context)
            db.flush()
        
        # Reset se última interação foi há mais de 1 hora
        if context.last_message_at:
            time_diff = now_brazil() - context.last_message_at
            if time_diff > timedelta(hours=1):
                context.state = ConversationState.IDLE
                context.context_data = "{}"
        
        return context
    
    async def _process_by_state(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Processa mensagem baseado no estado da conversa"""
        
        # Se está em processo de agendamento estruturado
        if context.state == ConversationState.ASKING_NAME:
            return await self._handle_name_input(context, message, db)
        
        elif context.state == ConversationState.ASKING_BIRTH_DATE:
            return await self._handle_birth_date_input(context, patient, message, db)
        
        elif context.state == ConversationState.ASKING_CONSULT_TYPE:
            return await self._handle_consult_type_input(context, patient, message, db)
        
        elif context.state == ConversationState.ASKING_DAY:
            return await self._handle_day_input(context, patient, message, db)
        
        elif context.state == ConversationState.SHOWING_TIMES:
            return await self._handle_time_selection(context, patient, message, db)
        
        elif context.state == ConversationState.CONFIRMING:
            return await self._handle_confirmation(context, patient, message, db)
        
        elif context.state == ConversationState.RESCHEDULING:
            return await self._handle_rescheduling(context, patient, message, db)
        
        elif context.state == ConversationState.CANCELLING:
            return await self._handle_cancelling(context, patient, message, db)
        
        # Estado IDLE ou ESCALATED: processar com Claude
        else:
            return await self._handle_general_conversation(context, patient, message, db)
    
    async def _handle_general_conversation(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Processa conversa geral usando Claude"""
        
        # Preparar histórico
        conversation_history = []
        
        # Adicionar contexto se existir
        try:
            context_data = json.loads(context.context_data or "{}")
            if 'history' in context_data:
                conversation_history = context_data['history'][-10:]  # Últimas 10 mensagens
        except:
            context_data = {}
        
        # Adicionar mensagem atual
        conversation_history.append({
            "role": "user",
            "content": message
        })
        
        # Chamar Claude
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=500,
                system=self._build_system_prompt(),
                messages=conversation_history
            )
            
            assistant_message = response.content[0].text
            
            # Salvar histórico
            conversation_history.append({
                "role": "assistant",
                "content": assistant_message
            })
            context_data['history'] = conversation_history[-10:]
            context.context_data = json.dumps(context_data, ensure_ascii=False)
            
            # Detectar intenção de agendamento
            if self._is_booking_intent(message, assistant_message):
                # Iniciar processo de agendamento
                if patient:
                    context.state = ConversationState.ASKING_CONSULT_TYPE
                    return assistant_message + "\n\nQue tipo de consulta você deseja agendar?"
                else:
                    context.state = ConversationState.ASKING_NAME
                    return assistant_message + "\n\nPara agendar, preciso de algumas informações. Qual é seu nome completo?"
            
            # Detectar intenção de cancelamento/remarcação
            if self._is_modification_intent(message):
                if patient:
                    context.state = ConversationState.RESCHEDULING
                    context_data['action'] = 'reschedule' if 'remar' in message.lower() else 'cancel'
                    context.context_data = json.dumps(context_data, ensure_ascii=False)
                    return await self._handle_modification_start(context, patient, db)
                else:
                    context.state = ConversationState.ASKING_NAME
                    context_data['next_state'] = ConversationState.RESCHEDULING
                    context_data['action'] = 'reschedule' if 'remar' in message.lower() else 'cancel'
                    context.context_data = json.dumps(context_data, ensure_ascii=False)
                    return "Para isso, preciso confirmar sua identidade. Qual é seu nome completo?"
            
            return assistant_message
            
        except Exception as e:
            logger.error(f"Erro ao chamar Claude: {str(e)}")
            return "Desculpe, estou com dificuldades técnicas. Por favor, tente novamente em instantes."
    
    def _is_booking_intent(self, user_message: str, bot_response: str) -> bool:
        """Detecta se usuário quer agendar"""
        keywords = ['agendar', 'marcar', 'consulta', 'horário', 'horario']
        message_lower = user_message.lower()
        return any(keyword in message_lower for keyword in keywords)
    
    def _is_modification_intent(self, message: str) -> bool:
        """Detecta se usuário quer cancelar ou remarcar"""
        keywords = ['cancelar', 'desmarcar', 'remarcar', 'mudar horário', 'mudar horario', 'trocar']
        message_lower = message.lower()
        return any(keyword in message_lower for keyword in keywords)
    
    async def _handle_name_input(self, context: ConversationContext, message: str, db: Session) -> str:
        """Processa entrada do nome"""
        name = extract_name_from_message(message)
        
        if not name:
            name = message.strip().title()
        
        if len(name) < 3:
            return "Por favor, me informe seu nome completo."
        
        # Salvar nome no contexto
        context_data = json.loads(context.context_data or "{}")
        context_data['temp_name'] = name
        context.context_data = json.dumps(context_data, ensure_ascii=False)
        context.state = ConversationState.ASKING_BIRTH_DATE
        
        return f"Obrigado, {name.split()[0]}! Agora preciso da sua data de nascimento (formato DD/MM/AAAA)."
    
    async def _handle_birth_date_input(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Processa entrada da data de nascimento"""
        date_str = extract_date_from_message(message)
        
        if not date_str:
            return "Por favor, me informe sua data de nascimento no formato DD/MM/AAAA (exemplo: 15/03/1990)."
        
        if not is_valid_birth_date(date_str):
            return "Data de nascimento inválida. Por favor, informe no formato DD/MM/AAAA."
        
        context_data = json.loads(context.context_data or "{}")
        temp_name = context_data.get('temp_name', '')
        
        # Criar ou atualizar paciente
        if not patient:
            patient = Patient(
                phone=context.phone,
                name=temp_name,
                birth_date=date_str
            )
            db.add(patient)
            db.flush()
            context.patient_id = patient.id
        
        # Verificar se tem próximo estado
        next_state = context_data.get('next_state')
        if next_state:
            if next_state == ConversationState.RESCHEDULING:
                context.state = ConversationState.RESCHEDULING
                return await self._handle_modification_start(context, patient, db)
        
        # Continuar para agendamento
        context.state = ConversationState.ASKING_CONSULT_TYPE
        return "Perfeito! Que tipo de consulta você deseja agendar?"
    
    async def _handle_consult_type_input(
        self,
        context: ConversationContext,
        patient: Patient,
        message: str,
        db: Session
    ) -> str:
        """Processa escolha do tipo de consulta"""
        consult_types = appointment_rules.get_consultation_types()
        
        # Tentar identificar tipo
        selected_type = None
        message_lower = message.lower()
        
        for ctype in consult_types:
            if ctype['tipo'].lower() in message_lower:
                selected_type = ctype
                break
        
        # Se não identificou, mostrar opções
        if not selected_type:
            options = "\n".join([f"• {ct['tipo']}" for ct in consult_types])
            return f"Temos os seguintes tipos de consulta:\n\n{options}\n\nQual você deseja?"
        
        # Salvar tipo escolhido
        context_data = json.loads(context.context_data or "{}")
        context_data['selected_consultation_type'] = selected_type
        context.context_data = json.dumps(context_data, ensure_ascii=False)
        context.state = ConversationState.ASKING_DAY
        
        return f"Ótimo! {selected_type['tipo']} - {format_currency(selected_type['valor_particular'])}\n\nQue dia seria melhor para você?"
    
    async def _handle_day_input(
        self,
        context: ConversationContext,
        patient: Patient,
        message: str,
        db: Session
    ) -> str:
        """Processa entrada do dia desejado"""
        context_data = json.loads(context.context_data or "{}")
        consultation_type = context_data.get('selected_consultation_type', {})
        duration = consultation_type.get('duracao_minutos', 30)
        
        # Tentar extrair data ou dia da semana
        target_date = None
        
        # Verificar se tem data explícita
        date_str = extract_date_from_message(message)
        if date_str:
            parsed_date = parse_date_br(date_str)
            if parsed_date:
                target_date = parsed_date
        
        # Se não, tentar dia da semana
        if not target_date:
            weekday = parse_weekday_from_message(message)
            if weekday is not None:
                # Encontrar próxima ocorrência desse dia
                today = now_brazil()
                days_ahead = weekday - today.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                
                # Adicionar dias mínimos de antecedência
                min_days = appointment_rules.get_min_days_advance()
                if days_ahead < min_days:
                    days_ahead += 7
                
                target_date = today + timedelta(days=days_ahead)
        
        if not target_date:
            return "Não entendi a data. Pode me informar o dia da semana (ex: quinta-feira) ou uma data específica (ex: 25/10/2025)?"
        
        # Buscar horários disponíveis
        available_slots = appointment_rules.get_available_slots(
            target_date, duration, db, limit=3
        )
        
        if not available_slots:
            return appointment_rules.format_available_slots_message([])
        
        # Salvar slots no contexto
        context_data['available_slots'] = [slot.isoformat() for slot in available_slots]
        context_data['selected_date'] = target_date.isoformat()
        context.context_data = json.dumps(context_data, ensure_ascii=False)
        context.state = ConversationState.SHOWING_TIMES
        
        return appointment_rules.format_available_slots_message(available_slots)
    
    async def _handle_time_selection(
        self,
        context: ConversationContext,
        patient: Patient,
        message: str,
        db: Session
    ) -> str:
        """Processa seleção do horário"""
        context_data = json.loads(context.context_data or "{}")
        available_slots_str = context_data.get('available_slots', [])
        
        if not available_slots_str:
            context.state = ConversationState.ASKING_DAY
            return "Desculpe, houve um erro. Por favor, me informe novamente o dia desejado."
        
        # Tentar extrair número da escolha
        message_clean = message.strip()
        try:
            choice = int(message_clean)
            if 1 <= choice <= len(available_slots_str):
                selected_slot_str = available_slots_str[choice - 1]
                selected_slot = datetime.fromisoformat(selected_slot_str)
                
                # Salvar seleção
                context_data['selected_slot'] = selected_slot_str
                context.context_data = json.dumps(context_data, ensure_ascii=False)
                context.state = ConversationState.CONFIRMING
                
                consultation_type = context_data.get('selected_consultation_type', {})
                
                return (
                    f"Perfeito! Vou agendar sua {consultation_type.get('tipo', 'consulta')} para "
                    f"{format_datetime_br(selected_slot)}.\n\n"
                    f"Confirma o agendamento? (Sim/Não)"
                )
            else:
                return "Por favor, escolha um número válido da lista."
        except ValueError:
            return "Por favor, responda com o número do horário desejado (1, 2 ou 3)."
    
    async def _handle_confirmation(
        self,
        context: ConversationContext,
        patient: Patient,
        message: str,
        db: Session
    ) -> str:
        """Processa confirmação do agendamento"""
        message_lower = message.lower()
        
        if 'sim' in message_lower or 'confirmo' in message_lower or 'ok' in message_lower:
            # Criar agendamento
            context_data = json.loads(context.context_data or "{}")
            selected_slot_str = context_data.get('selected_slot')
            consultation_type = context_data.get('selected_consultation_type', {})
            
            if not selected_slot_str:
                context.state = ConversationState.IDLE
                return "Desculpe, houve um erro. Por favor, comece o agendamento novamente."
            
            selected_slot = datetime.fromisoformat(selected_slot_str)
            
            # Criar no Google Calendar
            google_event_id = None
            if calendar_service.is_available():
                google_event_id = calendar_service.create_event(
                    title=f"Consulta - {patient.name}",
                    start_datetime=selected_slot,
                    duration_minutes=consultation_type.get('duracao_minutos', 30),
                    description=f"Paciente: {patient.name}\nTelefone: {patient.phone}\nTipo: {consultation_type.get('tipo', 'Consulta')}"
                )
            
            # Criar no banco
            appointment = Appointment(
                patient_id=patient.id,
                appointment_date=selected_slot,
                duration_minutes=consultation_type.get('duracao_minutos', 30),
                consultation_type=consultation_type.get('tipo', 'Consulta'),
                value=consultation_type.get('valor_particular'),
                status=AppointmentStatus.SCHEDULED,
                google_event_id=google_event_id
            )
            db.add(appointment)
            db.commit()
            
            # Resetar contexto
            context.state = ConversationState.IDLE
            context.context_data = "{}"
            
            clinic_info = load_clinic_info()
            address = clinic_info.get('endereco', '')
            
            return (
                f"✅ Consulta agendada com sucesso!\n\n"
                f"📅 Data: {format_datetime_br(selected_slot)}\n"
                f"⏱️ Duração: {consultation_type.get('duracao_minutos')} minutos\n"
                f"💰 Valor: {format_currency(consultation_type.get('valor_particular'))}\n"
                f"📍 Endereço: {address}\n\n"
                f"Lembramos que cancelamentos devem ser feitos com 24h de antecedência.\n"
                f"Até lá! 😊"
            )
        else:
            # Cancelar processo
            context.state = ConversationState.IDLE
            context.context_data = "{}"
            return "Agendamento cancelado. Se precisar de algo, estou à disposição!"
    
    async def _handle_modification_start(
        self,
        context: ConversationContext,
        patient: Patient,
        db: Session
    ) -> str:
        """Inicia processo de modificação de consulta"""
        # Buscar consultas futuras do paciente
        future_appointments = db.query(Appointment).filter(
            Appointment.patient_id == patient.id,
            Appointment.appointment_date > now_brazil(),
            Appointment.status == AppointmentStatus.SCHEDULED
        ).order_by(Appointment.appointment_date).all()
        
        if not future_appointments:
            context.state = ConversationState.IDLE
            return "Você não possui consultas agendadas para modificar."
        
        # Mostrar consultas
        message = "Suas consultas agendadas:\n\n"
        for i, apt in enumerate(future_appointments, 1):
            message += f"{i}. {apt.consultation_type} - {format_datetime_br(apt.appointment_date)}\n"
        
        context_data = json.loads(context.context_data or "{}")
        action = context_data.get('action', 'cancel')
        
        if action == 'reschedule':
            message += "\nQual consulta você deseja remarcar? (responda com o número)"
        else:
            message += "\nQual consulta você deseja cancelar? (responda com o número)"
        
        # Salvar IDs das consultas
        context_data['appointment_ids'] = [apt.id for apt in future_appointments]
        context.context_data = json.dumps(context_data, ensure_ascii=False)
        
        return message
    
    async def _handle_rescheduling(
        self,
        context: ConversationContext,
        patient: Patient,
        message: str,
        db: Session
    ) -> str:
        """Processa remarcação"""
        context_data = json.loads(context.context_data or "{}")
        appointment_ids = context_data.get('appointment_ids', [])
        
        if not appointment_ids:
            context.state = ConversationState.IDLE
            return "Erro ao processar. Por favor, tente novamente."
        
        # Extrair escolha
        try:
            choice = int(message.strip())
            if 1 <= choice <= len(appointment_ids):
                appointment_id = appointment_ids[choice - 1]
                appointment = db.query(Appointment).get(appointment_id)
                
                if not appointment:
                    context.state = ConversationState.IDLE
                    return "Consulta não encontrada."
                
                action = context_data.get('action', 'cancel')
                
                if action == 'cancel':
                    # Cancelar
                    if appointment.google_event_id and calendar_service.is_available():
                        calendar_service.delete_event(appointment.google_event_id)
                    
                    appointment.status = AppointmentStatus.CANCELLED
                    appointment.cancellation_reason = "Cancelado pelo paciente"
                    db.commit()
                    
                    context.state = ConversationState.IDLE
                    context.context_data = "{}"
                    
                    return f"✅ Consulta de {format_datetime_br(appointment.appointment_date)} cancelada com sucesso."
                
                else:
                    # Remarcar - iniciar novo agendamento
                    context_data['rescheduling_appointment_id'] = appointment_id
                    context.context_data = json.dumps(context_data, ensure_ascii=False)
                    context.state = ConversationState.ASKING_DAY
                    
                    return "Para qual dia você gostaria de remarcar?"
            else:
                return "Por favor, escolha um número válido da lista."
        except ValueError:
            return "Por favor, responda com o número da consulta."
    
    async def _handle_cancelling(
        self,
        context: ConversationContext,
        patient: Patient,
        message: str,
        db: Session
    ) -> str:
        """Mesmo que rescheduling, já tratado acima"""
        return await self._handle_rescheduling(context, patient, message, db)


# Instância global
ai_agent = AIAgent()

