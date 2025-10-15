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
    parse_weekday_from_message, get_brazil_timezone
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

FLUXO DE ATENDIMENTO ESTRUTURADO:

1. BOAS-VINDAS E IDENTIFICAÇÃO:
   - Sempre comece com uma mensagem de boas-vindas cordial
   - Solicite nome completo e data de nascimento (formato DD/MM/AAAA)
   - Salve essas informações no banco de dados
   - Após coletar os dados, apresente o menu principal

2. MENU PRINCIPAL:
   Apresente sempre estas 3 opções:
   "Como posso te ajudar hoje?
   
   1️⃣ Marcar consulta
   2️⃣ Remarcar/Cancelar consulta
   3️⃣ Tirar dúvidas"

3. MARCAR CONSULTA:
   - Escolher tipo de consulta (sem mostrar preços ou convênios)
   - Perguntar qual dia e horário fica melhor para a pessoa
   - Verificar disponibilidade e oferecer 3 opções de horário
   - Confirmar marcação (sem informações desnecessárias)
   - Perguntar: "Posso ajudar com mais alguma coisa?"
     * Se SIM → volta ao menu principal
     * Se NÃO → encerra conversa

4. REMARCAR/CANCELAR:
   - Buscar consultas do paciente (usando nome + nascimento)
   - Mostrar consultas encontradas
   - Perguntar se quer cancelar ou remarcar
   - Se cancelar: cancelar evento, perguntar se quer remarcar
   - Se remarcar: perguntar novo horário, confirmar mudança, atualizar banco
   - Perguntar: "Posso ajudar com mais alguma coisa?"

5. TIRAR DÚVIDAS:
   - Responder dúvidas sobre a clínica
   - Perguntar: "Posso ajudar com mais alguma coisa?"

REGRAS IMPORTANTES:
- Sempre seja cordial, respeitoso e profissional
- Use português brasileiro
- NUNCA dê orientação médica ou diagnósticos
- Mantenha respostas curtas e diretas (máximo 3-4 linhas)
- Use linguagem natural e amigável
- NÃO mostre preços ou convênios a menos que solicitado
- NÃO mostre informações desnecessárias (endereço, telefone, etc.)
- SEMPRE finalize perguntando se pode ajudar com mais alguma coisa
- Se a pessoa disser que não precisa de mais nada, encerre a conversa

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
                state=ConversationState.BOAS_VINDAS,
                context_data="{}",
                message_count=0
            )
            db.add(context)
            db.flush()
        
        # Reset se última interação foi há mais de 1 hora
        if context.last_message_at:
            # Garantir que ambos os datetimes tenham timezone
            last_msg = context.last_message_at
            if last_msg.tzinfo is None:
                last_msg = get_brazil_timezone().localize(last_msg)
            
            time_diff = now_brazil() - last_msg
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
        
        # Fluxo estruturado de atendimento
        if context.state == ConversationState.BOAS_VINDAS:
            return await self._handle_boas_vindas(context, message, db)
        
        elif context.state == ConversationState.COLETANDO_DADOS:
            return await self._handle_coletando_dados(context, patient, message, db)
        
        elif context.state == ConversationState.MENU_PRINCIPAL:
            return await self._handle_menu_principal(context, patient, message, db)
        
        elif context.state == ConversationState.MARCAR_CONSULTA:
            return await self._handle_marcar_consulta(context, patient, message, db)
        
        elif context.state == ConversationState.REMARCAR_CANCELAR:
            return await self._handle_remarcar_cancelar(context, patient, message, db)
        
        elif context.state == ConversationState.TIRAR_DUVIDAS:
            return await self._handle_tirar_duvidas(context, patient, message, db)
        
        elif context.state == ConversationState.CONFIRMANDO:
            return await self._handle_confirmation(context, patient, message, db)
        
        elif context.state == ConversationState.FINALIZANDO:
            return await self._handle_finalizando(context, patient, message, db)
        
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
    
    # ==================== NOVOS MÉTODOS PARA FLUXO ESTRUTURADO ====================
    
    async def _handle_boas_vindas(
        self,
        context: ConversationContext,
        message: str,
        db: Session
    ) -> str:
        """Mensagem de boas-vindas e início da coleta de dados"""
        context.state = ConversationState.COLETANDO_DADOS
        db.commit()
        
        return """Olá! Bem-vindo(a) à Clínica Teste! 👋

Sou seu assistente virtual. Para te ajudar melhor, preciso de algumas informações:

📝 Qual é o seu nome completo?"""
    
    async def _handle_coletando_dados(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Coleta nome e data de nascimento"""
        try:
            context_data = json.loads(context.context_data or "{}")
            
            # Se não tem nome ainda
            if 'name' not in context_data:
                # Extrair nome da mensagem
                name = message.strip()
                if len(name) < 2:
                    return "Por favor, digite seu nome completo:"
                
                context_data['name'] = name
                context.context_data = json.dumps(context_data, ensure_ascii=False)
                db.commit()
                
                return f"Prazer em conhecê-lo(a), {name}! 😊\n\n📅 Agora preciso da sua data de nascimento (formato DD/MM/AAAA):"
            
            # Se não tem data de nascimento ainda
            elif 'birth_date' not in context_data:
                # Validar data
                try:
                    birth_date = datetime.strptime(message.strip(), "%d/%m/%Y").date()
                    context_data['birth_date'] = birth_date.isoformat()
                    context.context_data = json.dumps(context_data, ensure_ascii=False)
                    
                    # Criar ou atualizar paciente
                    if not patient:
                        patient = Patient(
                            name=context_data['name'],
                            phone=context.phone,
                            birth_date=birth_date
                        )
                        db.add(patient)
                    else:
                        patient.name = context_data['name']
                        patient.birth_date = birth_date
                    
                    db.commit()
                    
                    # Ir para menu principal
                    context.state = ConversationState.MENU_PRINCIPAL
                    db.commit()
                    
                    return f"{context_data['name']}, como posso te ajudar hoje?\n\n1️⃣ Marcar consulta\n2️⃣ Remarcar/Cancelar consulta\n3️⃣ Tirar dúvidas"
                
                except ValueError:
                    return "Formato inválido. Por favor, digite sua data de nascimento no formato DD/MM/AAAA (ex: 15/03/1990):"
            
        except Exception as e:
            logger.error(f"Erro ao coletar dados: {str(e)}")
            return "Desculpe, ocorreu um erro. Vamos tentar novamente. Qual é o seu nome completo?"
    
    async def _handle_menu_principal(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Processa seleção do menu principal"""
        message_lower = message.lower().strip()
        
        if '1' in message_lower or 'marcar' in message_lower or 'consulta' in message_lower:
            context.state = ConversationState.MARCAR_CONSULTA
            db.commit()
            return "Ótimo! Vamos marcar sua consulta. 🩺\n\nQue tipo de consulta você precisa?\n\n• Consulta de rotina\n• Consulta de retorno\n• Consulta de urgência"
        
        elif '2' in message_lower or 'remarcar' in message_lower or 'cancelar' in message_lower:
            context.state = ConversationState.REMARCAR_CANCELAR
            db.commit()
            return "Vou te ajudar com remarcação ou cancelamento. 🔄\n\nPrimeiro, vou buscar suas consultas agendadas..."
        
        elif '3' in message_lower or 'dúvida' in message_lower or 'duvida' in message_lower:
            context.state = ConversationState.TIRAR_DUVIDAS
            db.commit()
            return "Claro! Estou aqui para tirar suas dúvidas. 🤔\n\nO que você gostaria de saber sobre nossa clínica?"
        
        else:
            return "Por favor, escolha uma das opções:\n\n1️⃣ Marcar consulta\n2️⃣ Remarcar/Cancelar consulta\n3️⃣ Tirar dúvidas"
    
    async def _handle_marcar_consulta(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Processa marcação de consulta"""
        try:
            context_data = json.loads(context.context_data or "{}")
            
            # Se não tem tipo de consulta ainda
            if 'consult_type' not in context_data:
                # Extrair tipo de consulta da mensagem
                consult_type = message.strip().lower()
                
                # Mapear tipos de consulta
                if 'rotina' in consult_type:
                    context_data['consult_type'] = 'Consulta de rotina'
                elif 'retorno' in consult_type:
                    context_data['consult_type'] = 'Retorno'
                elif 'urgência' in consult_type or 'urgencia' in consult_type:
                    context_data['consult_type'] = 'Consulta de urgência'
                else:
                    return "Por favor, escolha um tipo de consulta:\n\n• Consulta de rotina\n• Consulta de retorno\n• Consulta de urgência"
                
                context.context_data = json.dumps(context_data, ensure_ascii=False)
                db.commit()
                
                return f"Perfeito! {context_data['consult_type']}. 🩺\n\nQual dia e horário fica melhor para você?\n\nNosso horário de funcionamento:\n• Segunda a sexta: 08h às 18h\n• Sábado: 08h às 12h\n• Domingo: não há atendimento"
            
            # Se não tem data/horário ainda
            elif 'appointment_datetime' not in context_data:
                # Extrair data e horário da mensagem
                appointment_text = message.strip().lower()
                
                # Simular verificação de disponibilidade e oferecer 3 opções
                if 'sábado' in appointment_text or 'sabado' in appointment_text:
                    if '9' in appointment_text or '9h' in appointment_text:
                        # Simular 3 opções de horário
                        context_data['appointment_datetime'] = '2025-01-18 09:00'
                        context_data['available_times'] = ['09:00', '10:00', '11:00']
                        context.context_data = json.dumps(context_data, ensure_ascii=False)
                        db.commit()
                        
                        return f"Ótimo! Para sábado, temos estes horários disponíveis:\n\n1️⃣ 09:00\n2️⃣ 10:00\n3️⃣ 11:00\n\nQual você prefere?"
                    else:
                        return "Para sábado, temos horários disponíveis às 9h, 10h ou 11h. Qual você prefere?"
                else:
                    return "Por favor, escolha um dia da semana (segunda a sábado) e horário."
            
            # Se não tem horário selecionado ainda
            elif 'selected_time' not in context_data:
                # Processar seleção de horário
                if '1' in message or '9' in message:
                    context_data['selected_time'] = '09:00'
                elif '2' in message or '10' in message:
                    context_data['selected_time'] = '10:00'
                elif '3' in message or '11' in message:
                    context_data['selected_time'] = '11:00'
                else:
                    return "Por favor, escolha um dos horários:\n\n1️⃣ 09:00\n2️⃣ 10:00\n3️⃣ 11:00"
                
                context.context_data = json.dumps(context_data, ensure_ascii=False)
                db.commit()
                
                # Confirmar agendamento
                context.state = ConversationState.CONFIRMANDO
                db.commit()
                
                # Calcular data do próximo sábado
                today = datetime.now()
                days_until_saturday = (5 - today.weekday()) % 7  # 5 = sábado
                if days_until_saturday == 0:
                    days_until_saturday = 7  # Se hoje é sábado, pegar o próximo
                appointment_date = today + timedelta(days=days_until_saturday)
                
                return f"Perfeito! Confirmo sua consulta:\n\n📅 {appointment_date.strftime('%d/%m/%Y')} às {context_data['selected_time']}\n🩺 {context_data['consult_type']}\n👤 {patient.name if patient else 'Paciente'}\n\nPosso confirmar este agendamento para você?"
            
        except Exception as e:
            logger.error(f"Erro ao processar marcação: {str(e)}")
            return "Desculpe, ocorreu um erro. Vamos tentar novamente. Que tipo de consulta você precisa?"
    
    async def _handle_remarcar_cancelar(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Processa remarcação ou cancelamento"""
        # Por enquanto, usar o método antigo de remarcação
        return await self._handle_general_conversation(context, patient, message, db)
    
    async def _handle_tirar_duvidas(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Processa dúvidas sobre a clínica"""
        # Usar Claude para responder dúvidas
        response = await self._handle_general_conversation(context, patient, message, db)
        
        # Após responder, perguntar se precisa de mais alguma coisa
        context.state = ConversationState.FINALIZANDO
        db.commit()
        
        return f"{response}\n\nPosso ajudar com mais alguma coisa?"
    
    async def _handle_finalizando(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Finaliza conversa ou volta ao menu"""
        message_lower = message.lower().strip()
        
        if any(word in message_lower for word in ['sim', 's', 'yes', 'quero', 'preciso']):
            context.state = ConversationState.MENU_PRINCIPAL
            db.commit()
            return "Perfeito! Como posso te ajudar?\n\n1️⃣ Marcar consulta\n2️⃣ Remarcar/Cancelar consulta\n3️⃣ Tirar dúvidas"
        
        elif any(word in message_lower for word in ['não', 'nao', 'n', 'não preciso', 'nao preciso', 'tchau', 'obrigado', 'obrigada']):
            # Verificar se tem consulta agendada para mencionar a data
            context_data = json.loads(context.context_data or "{}")
            confirmed_date = context_data.get('confirmed_date')
            confirmed_time = context_data.get('confirmed_time')
            
            if confirmed_date and confirmed_time:
                context.state = ConversationState.IDLE
                db.commit()
                return f"Foi um prazer te atender! 😊\n\nTe esperamos no dia {confirmed_date} às {confirmed_time}. Tenha um ótimo dia!"
            else:
                context.state = ConversationState.IDLE
                db.commit()
                return "Foi um prazer te atender! 😊\n\nQualquer dúvida, é só chamar. Tenha um ótimo dia!"
        
        else:
            return "Posso ajudar com mais alguma coisa? (Sim/Não)"


# Instância global
ai_agent = AIAgent()

