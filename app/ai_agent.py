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
        
        return f"""Você é um assistente virtual de uma clínica médica no Brasil. Seu nome é Andressa.

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
   OBJETIVO: Agendar uma consulta para o paciente e enviar notificação para a clínica.
   
   DIRETRIZES:
   - Perguntar: "Que dia e horário você tem disponibilidade?"
   - VALIDAR se a data é futura
   - Se data VÁLIDA: confirmar e enviar notificação
   - Se data INVÁLIDA: explicar o problema e pedir nova data
   - Sempre confirmar antes de marcar: "Posso confirmar este agendamento para você?"
   - Após confirmar: enviar notificação para a clínica
   - Perguntar: "Posso ajudar com mais alguma coisa?"
   
   IMPORTANTE: NÃO perguntar sobre tipo de consulta, convênio ou valores. Apenas agendar a consulta.

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
        
        elif context.state == ConversationState.COLETANDO_NOME:
            return await self._handle_coletando_nome(context, message, db)
        
        elif context.state == ConversationState.COLETANDO_NASCIMENTO:
            return await self._handle_coletando_nascimento(context, patient, message, db)
        
        elif context.state == ConversationState.MENU_PRINCIPAL:
            return await self._handle_menu_principal(context, patient, message, db)
        
        elif context.state == ConversationState.MARCAR_CONSULTA:
            return await self._handle_marcar_consulta(context, patient, message, db)
        
        elif context.state == ConversationState.PROCESSANDO_AGENDAMENTO:
            return await self._handle_processando_agendamento(context, patient, message, db)
        
        elif context.state == ConversationState.REMARCAR_CANCELAR:
            return await self._handle_remarcar_cancelar(context, patient, message, db)
        
        elif context.state == ConversationState.TIRAR_DUVIDAS:
            return await self._handle_tirar_duvidas(context, patient, message, db)
        
        elif context.state == ConversationState.CONFIRMANDO:
            return await self._handle_confirmation(context, patient, message, db)
        
        elif context.state == ConversationState.FINALIZANDO:
            return await self._handle_finalizando(context, patient, message, db)
        
        elif context.state == ConversationState.CONVERSA_ENCERRADA:
            return await self._handle_conversa_encerrada(context, patient, message, db)
        
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
            
            # Detectar confirmação de agendamento
            if self._is_booking_confirmation(assistant_message):
                logger.info("🎯 Confirmação de agendamento detectada - enviando notificação...")
                # A confirmação será processada pelo fluxo normal de estados
            
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
        return "Perfeito! Que dia e horário você tem disponibilidade?"
    
    
    async def _handle_day_input(
        self,
        context: ConversationContext,
        patient: Patient,
        message: str,
        db: Session
    ) -> str:
        """Processa entrada do dia desejado"""
        context_data = json.loads(context.context_data or "{}")
        duration = 30  # Duração padrão de 30 minutos
        
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
                
                return (
                    f"Perfeito! Vou agendar sua consulta para "
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
        logger.info(f"🔄 Processando confirmação: '{message}'")
        message_lower = message.lower()
        
        if 'sim' in message_lower or 'confirmo' in message_lower or 'ok' in message_lower:
            # Criar agendamento
            context_data = json.loads(context.context_data or "{}")
            appointment_date_str = context_data.get('appointment_date')
            appointment_time_str = context_data.get('appointment_time')
            requested_date = context_data.get('requested_date')
            requested_time = context_data.get('requested_time')
            
            # Debug: mostrar o que está sendo processado
            logger.info(f"🔍 Dados do contexto na confirmação: {context_data}")
            logger.info(f"🔍 appointment_date_str: {appointment_date_str}")
            logger.info(f"🔍 appointment_time_str: {appointment_time_str}")
            logger.info(f"🔍 requested_date: {requested_date}")
            logger.info(f"🔍 requested_time: {requested_time}")
            
            if not appointment_date_str or not appointment_time_str:
                context.state = ConversationState.IDLE
                return "Desculpe, houve um erro. Por favor, comece o agendamento novamente."
            
            # Converter para datetime com timezone correto
            try:
                appointment_date = datetime.fromisoformat(appointment_date_str).date()
                # Converter string de tempo para time object
                appointment_time = datetime.strptime(appointment_time_str, '%H:%M:%S').time()
                appointment_datetime = datetime.combine(appointment_date, appointment_time)
                logger.info(f"✅ Datetime convertido: {appointment_datetime}")
            except Exception as e:
                logger.error(f"❌ Erro ao converter datetime: {str(e)}")
                context.state = ConversationState.IDLE
                return "Desculpe, houve um erro. Por favor, comece o agendamento novamente."
            
            # Enviar notificação via WhatsApp
            notification_message = f"""🩺 NOVA CONSULTA AGENDADA

👤 Paciente: {patient.name}
📅 Data de nascimento: {patient.birth_date}
📆 Data da consulta: {requested_date}
⏰ Horário: {requested_time}
📞 Telefone: {patient.phone}

Agendado via WhatsApp Bot"""
            
            # Enviar notificação para o número da clínica
            try:
                logger.info("🔄 Tentando enviar notificação para +55 24 99853-9136...")
                from app.whatsapp_service import WhatsAppService
                whatsapp_service = WhatsAppService()
                
                logger.info(f"📱 Enviando mensagem: {notification_message[:100]}...")
                notification_sent = await whatsapp_service.send_message(
                    phone="5524998539136",
                    message=notification_message
                )
                
                if notification_sent:
                    logger.info("✅ Notificação enviada com sucesso para +55 24 99853-9136")
                else:
                    logger.error("❌ Falha ao enviar notificação - retornou False")
            except Exception as e:
                logger.error(f"❌ Erro ao enviar notificação: {str(e)}")
                logger.error(f"❌ Tipo do erro: {type(e).__name__}")
            
            # Resetar contexto
            context.state = ConversationState.IDLE
            context.context_data = "{}"
            
            return (
                f"✅ Consulta agendada com sucesso!\n\n"
                f"📅 Data: {requested_date} às {requested_time}\n"
                f"⏱️ Duração: 30 minutos\n\n"
                f"📱 Enviamos uma notificação para a clínica com seus dados.\n"
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
            message += f"{i}. Consulta - {format_datetime_br(apt.appointment_date)}\n"
        
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
        context.state = ConversationState.COLETANDO_NOME
        db.commit()
        
        return """Olá! Bem-vindo(a) ao Consultório Dra. Rose! 👋

Sou a Andressa, sua assistente virtual. Para te ajudar melhor, preciso de algumas informações:

📝 Qual é o seu nome completo?"""
    
    async def _handle_coletando_nome(
        self,
        context: ConversationContext,
        message: str,
        db: Session
    ) -> str:
        """Coleta nome completo"""
        try:
            # Extrair nome da mensagem
            name = message.strip()
            if len(name) < 2:
                return "Por favor, digite seu nome completo:"
            
            # Salvar nome no contexto
            context_data = json.loads(context.context_data or "{}")
            context_data['name'] = name
            context.context_data = json.dumps(context_data, ensure_ascii=False)
            
            # Ir para próximo estado
            context.state = ConversationState.COLETANDO_NASCIMENTO
            db.commit()
            
            return f"Prazer em conhecê-lo(a), {name}! 😊\n\n📅 Agora preciso da sua data de nascimento (formato DD/MM/AAAA):"
            
        except Exception as e:
            logger.error(f"Erro ao coletar nome: {str(e)}")
            return "Desculpe, ocorreu um erro. Vamos tentar novamente. Qual é o seu nome completo?"
    
    async def _handle_coletando_nascimento(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Coleta data de nascimento"""
        try:
            context_data = json.loads(context.context_data or "{}")
            name = context_data.get('name', '')
            
            # Validar data
            try:
                birth_date = datetime.strptime(message.strip(), "%d/%m/%Y").date()
                
                # Criar ou atualizar paciente
                if not patient:
                    patient = Patient(
                        name=name,
                        phone=context.phone,
                        birth_date=birth_date
                    )
                    db.add(patient)
                else:
                    patient.name = name
                    patient.birth_date = birth_date
                
                db.commit()
                
                # Ir para menu principal
                context.state = ConversationState.MENU_PRINCIPAL
                db.commit()
                
                return f"{name}, como posso te ajudar hoje?\n\n1️⃣ Marcar consulta\n2️⃣ Remarcar/Cancelar consulta\n3️⃣ Tirar dúvidas"
            
            except ValueError:
                return "Formato inválido. Por favor, digite sua data de nascimento no formato DD/MM/AAAA (ex: 15/03/1990):"
            
        except Exception as e:
            logger.error(f"Erro ao coletar nascimento: {str(e)}")
            return "Desculpe, ocorreu um erro. Vamos tentar novamente. Qual é a sua data de nascimento?"
    
    async def _handle_menu_principal(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Processa seleção do menu principal"""
        message_lower = message.lower().strip()
        
        # Verificar se é uma seleção válida com mais variações
        # Opção 1 - Marcar consulta
        if any(word in message_lower for word in ['1', 'um', 'primeiro', 'primeira', 'marcar', 'consulta', 'agendar', 'agendamento']):
            return await self._handle_marcar_consulta(context, patient, message, db)
        
        # Opção 2 - Remarcar/Cancelar
        elif any(word in message_lower for word in ['2', 'dois', 'segundo', 'segunda', 'remarcar', 'cancelar', 'alterar', 'mudar']):
            context.state = ConversationState.REMARCAR_CANCELAR
            db.commit()
            return "Vou te ajudar com remarcação ou cancelamento. 🔄\n\nPrimeiro, vou buscar suas consultas agendadas..."
        
        # Opção 3 - Tirar dúvidas
        elif any(word in message_lower for word in ['3', 'três', 'tres', 'terceiro', 'terceira', 'dúvida', 'duvida', 'dúvidas', 'duvidas', 'pergunta', 'perguntas', 'informação', 'informações', 'saber', 'quero saber']):
            context.state = ConversationState.TIRAR_DUVIDAS
            db.commit()
            return "Claro! Estou aqui para tirar suas dúvidas. 🤔\n\nO que você gostaria de saber sobre nossa clínica?"
        
        else:
            # Se não for uma seleção válida, insistir na pergunta com instrução clara
            context_data = json.loads(context.context_data or "{}")
            name = context_data.get('name', '')
            return f"{name}, por favor escolha uma das opções:\n\n1️⃣ Marcar consulta\n2️⃣ Remarcar/Cancelar consulta\n3️⃣ Tirar dúvidas\n\nDigite a opção que você deseja escrevendo o número correspondente (1, 2 ou 3)."
    
    async def _handle_marcar_consulta(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Inicia processo de marcação de consulta"""
        # Ir para próximo estado
        context.state = ConversationState.PROCESSANDO_AGENDAMENTO
        db.commit()
        
        return """Que dia e horário você tem disponibilidade?

Por favor, escreva no formato: DD/MM/AAAA às HH:MM
Exemplo: 25/10/2025 às 14:30"""
    
    async def _handle_processando_agendamento(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Processa data e horário fornecidos pelo usuário"""
        try:
            # Extrair data e horário da mensagem
            message_clean = message.strip()
            
            # Procurar padrão DD/MM/AAAA às HH:MM
            import re
            pattern = r'(\d{2}/\d{2}/\d{4})\s*às\s*(\d{2}:\d{2})'
            match = re.search(pattern, message_clean)
            
            if not match:
                return "Formato inválido. Por favor, use o formato: DD/MM/AAAA às HH:MM\nExemplo: 25/10/2025 às 14:30"
            
            date_str, time_str = match.groups()
            
            # Validar data
            try:
                appointment_date = datetime.strptime(date_str, "%d/%m/%Y").date()
                appointment_time = datetime.strptime(time_str, "%H:%M").time()
            except ValueError:
                return "Data ou horário inválido. Por favor, use o formato: DD/MM/AAAA às HH:MM"
            
            # Validação simplificada - apenas verificar se data é válida
            if appointment_date < datetime.now().date():
                return "Por favor, escolha uma data futura."
            
            # Salvar no contexto
            context_data = json.loads(context.context_data or "{}")
            context_data['requested_date'] = date_str
            context_data['requested_time'] = time_str
            context_data['appointment_date'] = appointment_date.isoformat()
            context_data['appointment_time'] = appointment_time.isoformat()
            context.context_data = json.dumps(context_data, ensure_ascii=False)
            
            # Debug: mostrar o que está sendo salvo
            logger.info(f"🔍 Dados salvos no contexto: {context_data}")
            
            # Ir para confirmação
            context.state = ConversationState.CONFIRMANDO
            db.commit()
            
            return f"Perfeito! O horário {date_str} às {time_str} está disponível. Posso confirmar este agendamento para você?"
            
        except Exception as e:
            logger.error(f"Erro ao processar data/horário: {str(e)}")
            return "Desculpe, ocorreu um erro. Por favor, tente novamente com o formato: DD/MM/AAAA às HH:MM"
    
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
                context.state = ConversationState.CONVERSA_ENCERRADA
                db.commit()
                return f"Foi um prazer te atender! 😊\n\nTe esperamos no dia {confirmed_date} às {confirmed_time}. Tenha um ótimo dia!"
            else:
                context.state = ConversationState.CONVERSA_ENCERRADA
                db.commit()
                return "Foi um prazer te atender! 😊\n\nQualquer dúvida, é só chamar. Tenha um ótimo dia!"
        
        else:
            return "Posso ajudar com mais alguma coisa? (Sim/Não)"
    
    # ==================== MÉTODOS REMOVIDOS - NÃO MAIS NECESSÁRIOS ====================
    
    # ==================== MÉTODOS PARA FLUXO DE CONSULTA ====================
    
    def _is_booking_confirmation(self, message: str) -> bool:
        """Detecta se a IA confirmou um agendamento"""
        message_lower = message.lower()
        confirmation_phrases = [
            "posso confirmar este agendamento",
            "posso confirmar",
            "confirmar este agendamento",
            "agendamento confirmado",
            "consulta confirmada"
        ]
        return any(phrase in message_lower for phrase in confirmation_phrases)
    
    
    async def _handle_conversa_encerrada(
        self,
        context: ConversationContext,
        patient: Optional[Patient],
        message: str,
        db: Session
    ) -> str:
        """Reinicia o ciclo completo quando a conversa foi encerrada"""
        # Resetar contexto para início
        context.state = ConversationState.BOAS_VINDAS
        context.context_data = "{}"
        db.commit()
        
        return """Olá! Bem-vindo(a) ao Consultório Dra. Rose! 👋

Sou a Andressa, sua assistente virtual. Para te ajudar melhor, preciso de algumas informações:

📝 Qual é o seu nome completo?"""


# Instância global
ai_agent = AIAgent()

