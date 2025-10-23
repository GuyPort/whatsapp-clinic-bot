"""
Agente Claude para bot da clínica.
Versão completa com menu estruturado e gerenciamento de contexto.
"""
import logging
from typing import Dict, List
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from anthropic import Anthropic
import json
import os

from app.database import get_db
from app.models import Appointment, ConversationContext, PausedContact, AppointmentStatus
from app.utils import (
    load_clinic_info, normalize_phone, parse_date_br, 
    now_brazil, get_brazil_timezone, round_up_to_next_5_minutes
)
from app import appointment_rules

logger = logging.getLogger(__name__)

class ClaudeToolAgent:
    """Agente Claude com tools para gerenciar consultas da clínica"""
    
    def __init__(self):
        self.clinic_info = load_clinic_info()
        self.anthropic = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        
    def _create_system_prompt(self) -> str:
        """Cria o prompt do sistema com informações da clínica"""
        clinic_name = self.clinic_info.get('nome_clinica', 'Clínica')
        endereco = self.clinic_info.get('endereco', 'Endereço não informado')
        duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
        
        # Formatar horários de funcionamento
        horarios = self.clinic_info.get('horario_funcionamento', {})
        horarios_str = ""
        for dia, horario in horarios.items():
            if horario != "FECHADO":
                horarios_str += f"• {dia.capitalize()}: {horario}\n"
        
        return f"""Você é a Beatriz, secretária da {clinic_name}.

INFORMAÇÕES DA CLÍNICA:
📍 Endereço: {endereco}
⏰ Horários de funcionamento:
{horarios_str}
⏱️ Duração das consultas: {duracao} minutos
📞 Telefone: {self.clinic_info.get('telefone', 'Não informado')}

MENU PRINCIPAL:
Quando o paciente enviar qualquer mensagem, SEMPRE responda com este menu:

"Olá! Eu sou a Beatriz, secretária do {clinic_name}! 😊
Como posso te ajudar hoje?

1️⃣ Marcar consulta
2️⃣ Remarcar/Cancelar consulta  
3️⃣ Receitas

Digite o número da opção desejada."

FLUXO DE AGENDAMENTO (SEQUENCIAL):
Quando o paciente escolher "1" ou "1️⃣", siga EXATAMENTE este fluxo:

1. "Perfeito! Vamos marcar sua consulta. 😊
   Primeiro, me informe seu nome completo:"

2. Após receber o nome:
   "Obrigado! Agora me informe sua data de nascimento (DD/MM/AAAA):"

3. Após receber a data de nascimento:
   "Qual tipo de consulta você deseja?
   
   1️⃣ Clínica Geral - R$ 300
   2️⃣ Geriatria Clínica e Preventiva - R$ 300
   3️⃣ Atendimento Domiciliar ao Paciente Idoso - R$ 500
   
   Digite o número da opção desejada."

4. Após receber o tipo de consulta:
   "Perfeito! Agora me informe o dia que gostaria de marcar a consulta (DD/MM/AAAA):"

5. Após receber a data desejada:
   "Ótimo! E que horário você prefere? (HH:MM - ex: 14:30):"

6. **FLUXO CRÍTICO - Após receber horário:**
   a) Execute validate_and_check_availability com data e hora
   b) Leia o resultado da tool:
      - Se contém "disponível" → A tool já vai retornar uma mensagem pedindo confirmação
      - Se contém "não está disponível" → Explique e peça outro horário
      - Se contém "fora do horário" → Explique e peça outro horário
   c) NÃO execute create_appointment imediatamente após validar disponibilidade
   d) Apenas repasse a mensagem de confirmação que a tool retornou
   e) O sistema detectará automaticamente quando usuário confirmar

IMPORTANTE - FLUXO DE CONFIRMAÇÃO:
1. Após validar disponibilidade com validate_and_check_availability:
   - NÃO execute create_appointment imediatamente
   - A tool já vai retornar uma mensagem pedindo confirmação
   - Apenas repasse essa mensagem ao usuário
2. O sistema vai detectar automaticamente quando usuário confirmar
3. Você só deve executar create_appointment se o usuário:
   - Fornecer TODOS os dados novamente explicitamente
   - OU se já tiver confirmado previamente (verá no histórico)

REGRA IMPORTANTE: O fluxo de confirmação é automático. Não interfira!

CICLO DE ATENDIMENTO CONTÍNUO:
1. Após QUALQUER tarefa concluída (agendamento, cancelamento, resposta a dúvida):
   - SEMPRE perguntar: "Posso te ajudar com mais alguma coisa?"
   
2. Se usuário responder "sim" ou fizer nova pergunta:
   - Responder adequadamente usando as tools necessárias
   - Voltar ao passo 1 (perguntar novamente se pode ajudar)
   
3. Se usuário responder "não", "só isso", "obrigado", etc:
   - Execute tool 'end_conversation' para encerrar contexto
   - Enviar mensagem de despedida

IMPORTANTE - PERGUNTAS SOBRE A CLÍNICA:
Quando usuário perguntar QUALQUER COISA sobre a clínica (horários, dias de funcionamento, endereço, telefone, especialidades, etc):
- Execute IMEDIATAMENTE 'get_clinic_info'
- Responda com as informações formatadas
- SEMPRE perguntar: "Posso te ajudar com mais alguma coisa?"
- NUNCA diga "vou verificar" sem executar a tool imediatamente!

ENCERRAMENTO DE CONVERSAS:
Após QUALQUER tarefa concluída (agendamento criado, cancelamento realizado, dúvida respondida):
- SEMPRE perguntar: "Posso te ajudar com mais alguma coisa?"
- Se SIM ou usuário fizer nova pergunta: continuar com contexto
- Se NÃO ou "não preciso de mais nada": executar tool 'end_conversation'

ATENDIMENTO HUMANO:
Se o usuário pedir para "falar com a doutora", "falar com a médica", "falar com alguém da equipe", "humano", "falar com alguém", "atendente", etc:
- Execute IMEDIATAMENTE a tool 'request_human_assistance'
- NÃO pergunte confirmação, execute direto
- Lembre-se: VOCÊ É a Beatriz, secretária da clínica

REGRAS IMPORTANTES:
- SEMPRE peça UMA informação por vez
- NUNCA peça nome, data de nascimento, data e horário na mesma mensagem
- Use as tools disponíveis para validar horários e disponibilidade
- NUNCA mostre mensagens de confirmação antes de executar tools
- Execute tools automaticamente quando necessário
- Seja sempre educada e prestativa
- Confirme os dados antes de finalizar o agendamento

FERRAMENTAS DISPONÍVEIS:
- get_clinic_info: Obter informações da clínica
- validate_business_hours: Validar se horário está dentro do funcionamento
- validate_and_check_availability: Validar horário específico (funcionamento + disponibilidade)
- create_appointment: Criar novo agendamento
- search_appointments: Buscar agendamentos existentes
- cancel_appointment: Cancelar agendamento
- request_human_assistance: Transferir para atendimento humano
- end_conversation: Encerrar conversa quando usuário não precisa de mais nada

Lembre-se: Seja sempre educada, prestativa e siga o fluxo sequencial!"""

    def _define_tools(self) -> List[Dict]:
        """Define as tools disponíveis para o Claude"""
        return [
            {
                "name": "get_clinic_info",
                "description": "Obter TODAS as informações da clínica (nome, endereço, telefone, horários de funcionamento, dias fechados, especialidades). Use esta tool para responder QUALQUER pergunta sobre a clínica.",
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
                "name": "validate_and_check_availability",
                "description": "Validar se um horário específico está disponível (funcionamento + conflitos)",
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
                        "consultation_type": {
                            "type": "string",
                            "description": "Tipo de consulta: clinica_geral, geriatria ou domiciliar"
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
                            "description": "Telefone do paciente"
                        },
                        "name": {
                            "type": "string",
                            "description": "Nome do paciente"
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
                            "type": "string",
                            "description": "ID do agendamento a ser cancelado"
                        },
                        "reason": {
                            "type": "string",
                            "description": "Motivo do cancelamento"
                        }
                    },
                    "required": ["appointment_id", "reason"]
                }
            },
            {
                "name": "request_human_assistance",
                "description": "Transferir para atendimento humano",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "end_conversation",
                "description": "Encerrar conversa e limpar contexto",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        ]

    def _detect_confirmation_intent(self, message: str) -> str:
        """Detecta intenção de confirmação (positiva/negativa) de forma flexível"""
        message_lower = message.lower().strip()
        
        # Palavras-chave positivas
        positive_keywords = [
            "sim", "pode", "confirma", "confirmar", "claro", "ok", "okay",
            "perfeito", "isso", "certo", "exato", "vamos", "agendar",
            "marcar", "beleza", "aceito", "tá bom", "ta bom", "show",
            "positivo", "concordo", "fechado", "fechou"
        ]
        
        # Palavras-chave negativas
        negative_keywords = [
            "não", "nao", "nunca", "jamais", "mudar", "alterar", "trocar",
            "outro", "outra", "diferente", "modificar", "cancelar",
            "desistir", "quero mudar", "prefiro", "melhor não"
        ]
        
        # Verificar positivas
        for keyword in positive_keywords:
            if keyword in message_lower:
                return "positive"
        
        # Verificar negativas
        for keyword in negative_keywords:
            if keyword in message_lower:
                return "negative"
        
        return "unclear"

    def _extract_appointment_data_from_messages(self, messages: list) -> dict:
        """Extrai dados de agendamento do histórico de mensagens.
        Percorre as últimas mensagens para encontrar nome, nascimento, data e horário.
        Retorna sempre um dict; em erro, retorna {}.
        """
        try:
            data = {
                "patient_name": None,
                "patient_birth_date": None,
                "appointment_date": None,
                "appointment_time": None,
                "consultation_type": None
            }
            logger.info(f"🔍 Extraindo dados de {len(messages)} mensagens")
            import re
            
            for i in range(len(messages) - 1, -1, -1):
                msg = messages[i]
                if msg.get("role") != "user":
                    continue
                content = (msg.get("content") or "").strip()
                
                # 1. EXTRAÇÃO DE HORÁRIOS - Buscar em qualquer parte da mensagem
                if not data["appointment_time"]:
                    time_pattern = r'(\d{1,2}):(\d{2})'
                    time_match = re.search(time_pattern, content)
                    if time_match:
                        hour, minute = time_match.groups()
                        data["appointment_time"] = f"{hour.zfill(2)}:{minute}"
                        continue
                
                # 2. EXTRAÇÃO DE TIPO DE CONSULTA - Detectar escolha (1, 2, 3)
                if not data["consultation_type"]:
                    # Verificar se é uma escolha de tipo de consulta
                    if content.strip() in ["1", "1️⃣"]:
                        data["consultation_type"] = "clinica_geral"
                        continue
                    elif content.strip() in ["2", "2️⃣"]:
                        data["consultation_type"] = "geriatria"
                        continue
                    elif content.strip() in ["3", "3️⃣"]:
                        data["consultation_type"] = "domiciliar"
                        continue
                
                # 3. EXTRAÇÃO DE DATAS - Buscar em qualquer parte da mensagem
                date_pattern = r'(\d{1,2})/(\d{1,2})/(\d{4})'
                date_matches = re.findall(date_pattern, content)
                for match in date_matches:
                    day, month, year = match
                    full_date = f"{day.zfill(2)}/{month.zfill(2)}/{year}"
                    y = int(year)
                    if y < 2010 and not data["patient_birth_date"]:
                        data["patient_birth_date"] = full_date
                    elif y >= 2010 and not data["appointment_date"]:
                        data["appointment_date"] = full_date
                
                # 4. EXTRAÇÃO DE NOMES - Remover prefixos comuns
                if not data["patient_name"]:
                    # Prefixos comuns que devem ser removidos
                    name_prefixes = [
                        r'meu nome [eé] ',
                        r'eu sou ',
                        r'me chamo ',
                        r'eu me chamo ',
                        r'sou o ',
                        r'sou a '
                    ]
                    
                    # Limpar conteúdo removendo prefixos
                    cleaned_content = content
                    for prefix in name_prefixes:
                        cleaned_content = re.sub(prefix, '', cleaned_content, flags=re.IGNORECASE)
                    
                    # Remover pontuação final e espaços extras
                    cleaned_content = re.sub(r'[!.?,;]+$', '', cleaned_content).strip()
                    
                    # Lista de frases que NÃO são nomes
                    invalid_name_phrases = [
                        "por favor", "pode verificar", "tá bom", "está bem", 
                        "confirma", "confirmado", "sim por favor", "pode ser",
                        "perfeito", "obrigado", "obrigada", "valeu", "verificar",
                        "confirmar", "pode", "sim", "não", "nao"
                    ]
                    
                    # Verificar se contém frases inválidas
                    contains_invalid_phrase = any(phrase in cleaned_content.lower() for phrase in invalid_name_phrases)
                    
                    # Verificar se é um nome válido
                    has_letters = re.search(r"[A-Za-zÀ-ÿ]", cleaned_content) is not None
                    has_bad_symbols = re.search(r"[:=/]", cleaned_content) is not None
                    is_only_digits = re.fullmatch(r"\d+", cleaned_content) is not None
                    is_menu_or_greeting = cleaned_content.lower() in ["olá", "olá!", "oi", "oi!", "1", "2", "3"]
                    
                    if has_letters and not has_bad_symbols and not is_only_digits and not is_menu_or_greeting and len(cleaned_content) > 1 and not contains_invalid_phrase:
                        data["patient_name"] = cleaned_content
            
            logger.info(f"📋 Extração concluída: {data}")
            return data
            
        except Exception as e:
            logger.error(f"Erro ao extrair dados do histórico: {e}", exc_info=True)
            return {}

    def process_message(self, message: str, phone: str) -> str:
        """Processa uma mensagem do usuário e retorna resposta do bot"""
        try:
            logger.info(f"📱 Processando mensagem de {phone}: {message}")
            
            # 1. Verificar se contato está pausado
            with get_db() as db:
                paused_contact = db.query(PausedContact).filter_by(phone=phone).first()
                if paused_contact and paused_contact.paused_until > datetime.utcnow():
                    logger.info(f"⏸️ Contato {phone} está pausado até {paused_contact.paused_until}")
                    return "Olá! No momento você está sendo atendido por nossa equipe. Aguarde um instante! 😊"
                
                # 2. Carregar ou criar contexto
                context = db.query(ConversationContext).filter_by(phone=phone).first()
                if not context:
                    context = ConversationContext(phone=phone, messages=[])
                    db.add(context)
                    logger.info(f"🆕 Novo contexto criado para {phone}")
                
                # 3. Verificar se deve encerrar conversa
                if self._should_end_context(message):
                    return self._handle_end_conversation({}, db, phone)
                
                # 4. Verificar se há confirmação pendente ANTES de processar com Claude
                if context.flow_data and context.flow_data.get("pending_confirmation"):
                    intent = self._detect_confirmation_intent(message)
                    
                    if intent == "positive":
                        # Usuário confirmou! Executar agendamento
                        logger.info(f"✅ Usuário {phone} confirmou agendamento")
                        
                        # Usar dados do flow_data (NÃO re-extrair do histórico)
                        data = context.flow_data or {}
                        
                        # Se faltar dados, extrair do histórico APENAS UMA VEZ
                        if not data.get("patient_name") or not data.get("patient_birth_date") or not data.get("consultation_type"):
                            logger.info(f"🔍 Dados incompletos no flow_data, extraindo do histórico: {data}")
                            extracted = self._extract_appointment_data_from_messages(context.messages)
                            data["patient_name"] = data.get("patient_name") or extracted.get("patient_name")
                            data["patient_birth_date"] = data.get("patient_birth_date") or extracted.get("patient_birth_date")
                            data["consultation_type"] = data.get("consultation_type") or extracted.get("consultation_type")
                            logger.info(f"🔍 Dados após extração: {data}")
                        
                        # Criar agendamento
                        result = self._handle_create_appointment({
                            "patient_name": data.get("patient_name"),
                            "patient_birth_date": data.get("patient_birth_date"),
                            "consultation_type": data.get("consultation_type"),
                            "appointment_date": data.get("appointment_date"),
                            "appointment_time": data.get("appointment_time"),
                            "patient_phone": phone
                        }, db, phone)
                        
                        # Limpar pending_confirmation
                        if not context.flow_data:
                            context.flow_data = {}
                        context.flow_data["pending_confirmation"] = False
                        
                        # Adicionar mensagens ao histórico
                        context.messages.append({"role": "user", "content": message, "timestamp": datetime.utcnow().isoformat()})
                        context.messages.append({"role": "assistant", "content": result, "timestamp": datetime.utcnow().isoformat()})
                        
                        # Atualizar contexto
                        context.last_activity = datetime.utcnow()
                        db.commit()
                        
                        logger.info(f"💾 Contexto salvo para {phone}: {len(context.messages)} mensagens")
                        return result
                    
                    elif intent == "negative":
                        logger.info(f"❌ Usuário {phone} não confirmou, pedindo alteração")
                        if not context.flow_data:
                            context.flow_data = {}
                        context.flow_data["pending_confirmation"] = False
                        db.commit()
                        
                        response = "Sem problemas! O que você gostaria de mudar?\n\n" \
                                   "1️⃣ Data\n" \
                                   "2️⃣ Horário\n" \
                                   "3️⃣ Ambos"
                        
                        # Adicionar mensagens ao histórico
                        context.messages.append({"role": "user", "content": message, "timestamp": datetime.utcnow().isoformat()})
                        context.messages.append({"role": "assistant", "content": response, "timestamp": datetime.utcnow().isoformat()})
                        
                        # Atualizar contexto
                        context.last_activity = datetime.utcnow()
                        db.commit()
                        
                        logger.info(f"💾 Contexto salvo para {phone}: {len(context.messages)} mensagens")
                        return response
                    
                    logger.info(f"⚠️ Intenção não clara, processando com Claude")

                # 5. Adicionar mensagem do usuário ao histórico
                context.messages.append({"role": "user", "content": message, "timestamp": datetime.utcnow().isoformat()})
                
                # 6. Enviar para Claude
                response = self.anthropic.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    max_tokens=4000,
                    system=self._create_system_prompt(),
                    messages=context.messages,
                    tools=self._define_tools(),
                    tool_choice="auto"
                )
                
                logger.info(f"🤖 Enviando {len(context.messages)} mensagens para Claude")
                
                # 7. Processar resposta do Claude
                bot_response = ""
                current_response = response
                max_iterations = 5
                iteration = 0
                
                while iteration < max_iterations:
                    iteration += 1
                    
                    if current_response.stop_reason == "tool_use":
                        # Claude quer usar uma tool
                        tool_use = current_response.content[0]
                        tool_name = tool_use.name
                        tool_input = tool_use.input
                        
                        logger.info(f"🔧 Executando tool: {tool_name} com input: {tool_input}")
                        
                        # Executar tool
                        tool_result = self._execute_tool(tool_name, tool_input, db, phone)
                        
                        # Adicionar tool use e result ao contexto
                        context.messages.append({
                            "role": "assistant", 
                            "content": [{"type": "tool_use", "name": tool_name, "input": tool_input}],
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        context.messages.append({
                            "role": "user", 
                            "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": tool_result}],
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        
                        # Continuar conversa com Claude
                        current_response = self.anthropic.messages.create(
                            model="claude-3-5-sonnet-20241022",
                            max_tokens=4000,
                            system=self._create_system_prompt(),
                            messages=context.messages,
                            tools=self._define_tools(),
                            tool_choice="auto"
                        )
                        
                        logger.info(f"🔧 Iteration {iteration}: Tool {tool_name} result: {tool_result}")
                        
                        if current_response.stop_reason == "end_turn":
                            # Claude terminou, pegar resposta final
                            if current_response.content and current_response.content[0].type == "text":
                                bot_response = current_response.content[0].text
                            break
                        elif current_response.stop_reason == "tool_use":
                            # Claude quer usar outra tool, continuar loop
                            continue
                        else:
                            # Outro motivo de parada
                            bot_response = "Desculpe, não consegui processar sua solicitação completamente."
                            break
                    else:
                        # Claude retornou texto direto
                        if current_response.content and current_response.content[0].type == "text":
                            bot_response = current_response.content[0].text
                        else:
                            bot_response = "Desculpe, não consegui processar sua solicitação."
                        break
                
                # 7.5. Persistir dados incrementalmente no flow_data
                if not context.flow_data:
                    context.flow_data = {}
                
                extracted = self._extract_appointment_data_from_messages(context.messages)
                
                if extracted.get("patient_name") and not context.flow_data.get("patient_name"):
                    context.flow_data["patient_name"] = extracted["patient_name"]
                    logger.info(f"💾 Nome salvo no flow_data: {extracted['patient_name']}")
                
                if extracted.get("patient_birth_date") and not context.flow_data.get("patient_birth_date"):
                    context.flow_data["patient_birth_date"] = extracted["patient_birth_date"]
                    logger.info(f"💾 Data nascimento salva no flow_data: {extracted['patient_birth_date']}")
                
                if extracted.get("appointment_date") and not context.flow_data.get("appointment_date"):
                    context.flow_data["appointment_date"] = extracted["appointment_date"]
                    logger.info(f"💾 Data consulta salva no flow_data: {extracted['appointment_date']}")
                
                if extracted.get("appointment_time") and not context.flow_data.get("appointment_time"):
                    context.flow_data["appointment_time"] = extracted["appointment_time"]
                    logger.info(f"💾 Horário consulta salvo no flow_data: {extracted['appointment_time']}")
                
                if extracted.get("consultation_type") and not context.flow_data.get("consultation_type"):
                    context.flow_data["consultation_type"] = extracted["consultation_type"]
                    logger.info(f"💾 Tipo consulta salvo no flow_data: {extracted['consultation_type']}")
                
                # 8. Atualizar contexto no banco
                context.last_activity = datetime.utcnow()
                db.commit()
                
                logger.info(f"💾 Contexto salvo para {phone}: {len(context.messages)} mensagens")
                return bot_response
                
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {str(e)}")
            return "Desculpe, ocorreu um erro. Tente novamente em alguns instantes."

    def _execute_tool(self, tool_name: str, tool_input: Dict, db: Session, phone: str = None) -> str:
        """Executa uma tool específica"""
        try:
            logger.info(f"🔧 Executando tool: {tool_name} com input: {tool_input}")

            if tool_name == "get_clinic_info":
                return self._handle_get_clinic_info(tool_input)
            elif tool_name == "validate_business_hours":
                return self._handle_validate_business_hours(tool_input)
            elif tool_name == "validate_and_check_availability":
                return self._handle_validate_and_check_availability(tool_input, db)
            elif tool_name == "create_appointment":
                return self._handle_create_appointment(tool_input, db)
            elif tool_name == "search_appointments":
                return self._handle_search_appointments(tool_input, db)
            elif tool_name == "cancel_appointment":
                return self._handle_cancel_appointment(tool_input, db)
            elif tool_name == "request_human_assistance":
                return self._handle_request_human_assistance(tool_input, db, phone)
            elif tool_name == "end_conversation":
                return self._handle_end_conversation(tool_input, db, phone)
            else:
                return f"Tool '{tool_name}' não reconhecida."
        except Exception as e:
            logger.error(f"Erro ao executar tool {tool_name}: {str(e)}")
            return f"Erro ao executar {tool_name}: {str(e)}"

    def _handle_get_clinic_info(self, tool_input: Dict) -> str:
        """Tool: get_clinic_info - Retorna informações da clínica formatadas de forma completa"""
        try:
            # Retornar TODAS as informações da clínica formatadas
            response = ""
            
            # Nome da clínica
            response += f"🏥 **{self.clinic_info.get('nome_clinica', 'Clínica')}**\n\n"
            
            # Endereço
            response += f"📍 **Endereço:**\n{self.clinic_info.get('endereco', 'Não informado')}\n\n"
            
            # Telefone
            response += f"📞 **Telefone:**\n{self.clinic_info.get('telefone', 'Não informado')}\n\n"
            
            # Horários de funcionamento
            response += "📅 **Horários de Funcionamento:**\n"
            horarios = self.clinic_info.get('horario_funcionamento', {})
            dias_ordenados = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
            
            for dia in dias_ordenados:
                if dia in horarios:
                    horario = horarios[dia]
                    dia_formatado = dia.replace('terca', 'terça').replace('sabado', 'sábado')
                    if horario != "FECHADO":
                        response += f"• {dia_formatado.capitalize()}: {horario}\n"
                    else:
                        response += f"• {dia_formatado.capitalize()}: FECHADO\n"
            
            # Dias especiais fechados
            dias_fechados = self.clinic_info.get('dias_fechados', [])
            if dias_fechados:
                response += f"\n🚫 **Dias Especiais Fechados (Feriados/Férias):**\n"
                for dia in dias_fechados:
                    response += f"• {dia}\n"
            
            # Informações adicionais
            info_adicionais = self.clinic_info.get('informacoes_adicionais', {})
            if info_adicionais:
                response += f"\n💡 **Informações Adicionais:**\n"
                if 'duracao_consulta' in info_adicionais:
                    response += f"• Duração da consulta: {info_adicionais['duracao_consulta']}\n"
                if 'especialidades' in info_adicionais:
                    response += f"• Especialidades: {info_adicionais['especialidades']}\n"
            
            return response
            
        except Exception as e:
            logger.error(f"Erro ao obter info da clínica: {str(e)}")
            return f"Erro ao buscar informações: {str(e)}"

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
            
            # Verificar dia da semana
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
            
            horarios = self.clinic_info.get('horario_funcionamento', {})
            horario_dia = horarios.get(weekday_pt, "FECHADO")
            
            if horario_dia == "FECHADO":
                return f"❌ A clínica não funciona aos {weekday_pt}s. Horários de funcionamento:\n" + \
                       self._format_business_hours()
            
            # Verificar se horário está dentro do funcionamento
            try:
                time_obj = datetime.strptime(time_str, '%H:%M').time()
                start_time, end_time = horario_dia.split('-')
                start_obj = datetime.strptime(start_time, '%H:%M').time()
                end_obj = datetime.strptime(end_time, '%H:%M').time()
                
                if start_obj <= time_obj <= end_obj:
                    return f"✅ Horário {time_str} está dentro do funcionamento da clínica."
                else:
                    return f"❌ Horário {time_str} está fora do funcionamento. Horário de funcionamento: {horario_dia}"
                    
            except ValueError:
                return "Formato de horário inválido. Use HH:MM."
                
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
    
    def _is_clinic_open_now(self) -> tuple[bool, str]:
        """
        Verifica se a clínica está aberta AGORA.
        
        Returns:
            tuple: (is_open: bool, message: str)
        """
        try:
            # Obter data/hora atual do Brasil
            now_br = now_brazil()
            date_str = now_br.strftime('%d/%m/%Y')
            time_str = now_br.strftime('%H:%M')
            
            # Verificar se está em dias_fechados
            dias_fechados = self.clinic_info.get('dias_fechados', [])
            if date_str in dias_fechados:
                return False, f"❌ A clínica está fechada hoje ({date_str}) por motivo especial."
            
            # Obter dia da semana
            weekday = now_br.strftime('%A').lower()
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
            
            # Verificar horário de funcionamento
            horarios = self.clinic_info.get('horario_funcionamento', {})
            horario_dia = horarios.get(weekday_pt, "FECHADO")
            
            if horario_dia == "FECHADO":
                return False, f"❌ A clínica não funciona aos {weekday_pt}s."
            
            # Verificar se está dentro do horário
            try:
                time_obj = datetime.strptime(time_str, '%H:%M').time()
                start_time, end_time = horario_dia.split('-')
                start_obj = datetime.strptime(start_time, '%H:%M').time()
                end_obj = datetime.strptime(end_time, '%H:%M').time()
                
                if start_obj <= time_obj <= end_obj:
                    return True, f"✅ A clínica está aberta agora ({time_str})."
                else:
                    return False, f"❌ A clínica está fechada agora ({time_str}). Funciona das {start_time} às {end_time}."
                    
            except ValueError:
                return False, "❌ Erro ao verificar horário de funcionamento."
                
        except Exception as e:
            logger.error(f"Erro ao verificar se clínica está aberta: {str(e)}")
            return False, f"❌ Erro ao verificar horário: {str(e)}"

    def _handle_validate_and_check_availability(self, tool_input: Dict, db: Session) -> str:
        """Tool: validate_and_check_availability - Valida horário de funcionamento + disponibilidade"""
        try:
            logger.info(f"🔍 Tool validate_and_check_availability chamada com input: {tool_input}")
            
            date_str = tool_input.get("date")
            time_str = tool_input.get("time")
            
            if not date_str or not time_str:
                logger.warning("❌ Data ou horário não fornecidos")
                return "Data e horário são obrigatórios."
            
            logger.info(f"📅 Validando: {date_str} às {time_str}")
            
            # 1. Converter data
            appointment_date = parse_date_br(date_str)
            if not appointment_date:
                logger.warning(f"❌ Data inválida: {date_str}")
                return "Data inválida. Use o formato DD/MM/AAAA."
            
            # 2. Verificar se está em dias_fechados
            dias_fechados = self.clinic_info.get('dias_fechados', [])
            if date_str in dias_fechados:
                logger.warning(f"❌ Clínica fechada em {date_str} (dia especial)")
                return f"❌ A clínica estará fechada em {date_str} por motivo especial (feriado/férias).\n" + \
                       "Por favor, escolha outra data."
            
            # 3. Validar horário de funcionamento
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
            
            horarios = self.clinic_info.get('horario_funcionamento', {})
            horario_dia = horarios.get(weekday_pt, "FECHADO")
            
            if horario_dia == "FECHADO":
                logger.warning(f"❌ Clínica fechada aos {weekday_pt}s")
                return f"❌ A clínica não funciona aos {weekday_pt}s. Horários de funcionamento:\n" + \
                       self._format_business_hours()
            
            # 4. Verificar se horário está dentro do funcionamento
            try:
                time_obj = datetime.strptime(time_str, '%H:%M').time()
                start_time, end_time = horario_dia.split('-')
                start_obj = datetime.strptime(start_time, '%H:%M').time()
                end_obj = datetime.strptime(end_time, '%H:%M').time()
                
                if not (start_obj <= time_obj <= end_obj):
                    logger.warning(f"❌ Horário {time_str} fora do funcionamento")
                    return f"❌ Horário {time_str} está fora do funcionamento. Horário de funcionamento: {horario_dia}"
                    
            except ValueError:
                logger.warning(f"❌ Formato de horário inválido: {time_str}")
                return "Formato de horário inválido. Use HH:MM."
            
            # 5. Verificar disponibilidade do horário
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
            
            # Combinar data e horário
            hora_consulta = datetime.combine(appointment_date.date(), time_obj)
            
            # Arredondar para múltiplo de 5 minutos
            hora_consulta = round_up_to_next_5_minutes(hora_consulta)
            
            # Localizar no timezone do Brasil
            tz = get_brazil_timezone()
            if hora_consulta.tzinfo is None:
                hora_consulta = tz.localize(hora_consulta)
            
            # Verificar disponibilidade
            is_available = appointment_rules.check_slot_availability(hora_consulta, duracao, db)
            
            if is_available:
                ajuste_msg = ""
                if hora_consulta.strftime('%H:%M') != time_str:
                    ajuste_msg = f" (ajustado para {hora_consulta.strftime('%H:%M')})"
                logger.info(f"✅ Horário {hora_consulta.strftime('%H:%M')} disponível!{ajuste_msg}")

                phone = tool_input.get("patient_phone")
                if phone:
                    context = db.query(ConversationContext).filter_by(phone=phone).first()
                    if context:
                        # Extrair dados do histórico ANTES de salvar no flow_data
                        extracted = self._extract_appointment_data_from_messages(context.messages)
                        
                        # Preservar dados já existentes, adicionar novos
                        if not context.flow_data:
                            context.flow_data = {}
                        context.flow_data.update({
                            "patient_name": context.flow_data.get("patient_name") or extracted.get("patient_name"),
                            "patient_birth_date": context.flow_data.get("patient_birth_date") or extracted.get("patient_birth_date"),
                            "consultation_type": context.flow_data.get("consultation_type") or extracted.get("consultation_type"),
                            "appointment_date": date_str,
                            "appointment_time": hora_consulta.strftime('%H:%M'),
                            "pending_confirmation": True
                        })
                        db.commit()
                        logger.info(f"💾 Dados salvos no flow_data para confirmação: {context.flow_data}")

                return f"✅ Horário {hora_consulta.strftime('%H:%M')} disponível!{ajuste_msg}\n\n" \
                       f"📋 *Resumo da sua consulta:*\n" \
                       f"📅 Data: {date_str}\n" \
                       f"⏰ Horário: {hora_consulta.strftime('%H:%M')}\n\n" \
                       f"Posso confirmar sua consulta?"
            else:
                logger.warning(f"❌ Horário {time_str} não disponível (conflito)")
                return f"❌ Horário {time_str} não está disponível. Já existe uma consulta neste horário.\n" + \
                       "Por favor, escolha outro horário."
            
        except Exception as e:
            logger.error(f"Erro ao validar disponibilidade: {str(e)}")
            return f"Erro ao validar disponibilidade: {str(e)}"
    
    def _handle_check_availability(self, tool_input: Dict, db: Session) -> str:
        """Tool: check_availability"""
        try:
            logger.info(f"🔍 Tool check_availability chamada com input: {tool_input}")
            
            date_str = tool_input.get("date")
            if not date_str:
                logger.warning("❌ Data não fornecida na tool check_availability")
                return "Data é obrigatória."
            
            logger.info(f"📅 Verificando disponibilidade para data: {date_str}")
            
            # Converter data
            appointment_date = parse_date_br(date_str)
            if not appointment_date:
                logger.warning(f"❌ Data inválida: {date_str}")
                return "Data inválida. Use o formato DD/MM/AAAA."
            
            logger.info(f"📅 Data convertida: {appointment_date}")
            
            # Obter horários disponíveis
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 45)
            logger.info(f"⏱️ Duração da consulta: {duracao} minutos")
            
            available_slots = appointment_rules.get_available_slots(appointment_date, duracao, db)
            logger.info(f"📋 Slots encontrados: {len(available_slots)}")
            
            if not available_slots:
                logger.warning(f"❌ Nenhum horário disponível para {appointment_date.strftime('%d/%m/%Y')}")
                return f"❌ Não há horários disponíveis para {appointment_date.strftime('%d/%m/%Y')}.\n" + \
                       "Por favor, escolha outra data."
            
            response = f"✅ Horários disponíveis para {appointment_date.strftime('%d/%m/%Y')}:\n\n"
            for i, slot in enumerate(available_slots, 1):
                response += f"{i}. {slot.strftime('%H:%M')}\n"
            
            response += f"\n⏱️ Duração: {duracao} minutos\n"
            response += "Escolha um horário e me informe o número da opção desejada."
            
            logger.info(f"✅ Resposta da tool: {response}")
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
            consultation_type = tool_input.get("consultation_type")
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
            
            # Combinar data e horário (com arredondamento para múltiplo de 5 min)
            try:
                time_obj_original = datetime.strptime(appointment_time, '%H:%M').time()
                temp_dt = datetime.combine(appointment_datetime.date(), time_obj_original)
                rounded_dt = round_up_to_next_5_minutes(temp_dt)
                
                # Localizar no timezone do Brasil para garantir data correta
                tz = get_brazil_timezone()
                if rounded_dt.tzinfo is None:
                    appointment_datetime = tz.localize(rounded_dt)
                else:
                    appointment_datetime = rounded_dt
                
                # Localizar no timezone do Brasil para validação
                if appointment_datetime.tzinfo is None:
                    appointment_datetime_local = tz.localize(appointment_datetime)
                else:
                    appointment_datetime_local = appointment_datetime
                    
            except ValueError:
                return "Formato de horário inválido. Use HH:MM."
            
            # Verificar se horário está disponível
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
            is_available = appointment_rules.check_slot_availability(appointment_datetime_local, duracao, db)
            
            if not is_available:
                return f"❌ Horário {appointment_time} não está disponível. Use a tool check_availability para ver horários disponíveis."
            
            # Buscar informações do tipo de consulta
            tipos_consulta = self.clinic_info.get('tipos_consulta', {})
            tipo_info = tipos_consulta.get(consultation_type or 'clinica_geral', {})
            tipo_nome = tipo_info.get('nome', 'Clínica Geral')
            tipo_valor = tipo_info.get('valor', 300)
            
            # Criar agendamento - SALVAR COMO STRING YYYYMMDD para evitar problemas de timezone
            appointment_datetime_formatted = str(appointment_datetime.strftime('%Y%m%d'))  # "20251022" - GARANTIR STRING
            
            appointment = Appointment(
                patient_name=patient_name,
                patient_phone=normalized_phone,
                patient_birth_date=patient_birth_date,  # Manter como string
                appointment_date=appointment_datetime_formatted,  # "20251022" - STRING EXPLÍCITA
                appointment_time=appointment_time,  # Salvar como string HH:MM
                duration_minutes=duracao,
                consultation_type=consultation_type or 'clinica_geral',
                status=AppointmentStatus.AGENDADA,
                notes=notes
            )
            
            db.add(appointment)
            db.commit()
            
            return f"✅ **Agendamento realizado com sucesso!**\n\n" + \
                   f"👤 **Paciente:** {patient_name}\n" + \
                   f"🩺 **Tipo:** {tipo_nome}\n" + \
                   f"💰 **Valor:** R$ {tipo_valor}\n" + \
                   f"📅 **Data:** {appointment_datetime.strftime('%d/%m/%Y')}\n" + \
                   f"⏰ **Horário:** {appointment_datetime.strftime('%H:%M')}\n" + \
                   f"⏱️ **Duração:** {duracao} minutos\n" + \
                   f"📞 **Telefone:** {normalized_phone}\n\n" + \
                   "Obrigado por escolher nossa clínica! 😊\n\n" + \
                   "Posso te ajudar com mais alguma coisa?"
                   
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
            
            appointments = query.order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc()).all()
            
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
                response += f"   📅 {apt.appointment_date.strftime('%d/%m/%Y às')} {apt.appointment_time.strftime('%H:%M')}\n"
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
                   f"📅 **Data:** {appointment.appointment_date.strftime('%d/%m/%Y às')} {appointment.appointment_time.strftime('%H:%M')}\n" + \
                   f"📝 **Motivo:** {reason}\n\n" + \
                   "Se precisar reagendar, estarei aqui para ajudar! 😊"
                   
        except Exception as e:
            logger.error(f"Erro ao cancelar agendamento: {str(e)}")
            db.rollback()
            return f"Erro ao cancelar agendamento: {str(e)}"

    def _handle_request_human_assistance(self, tool_input: Dict, db: Session, phone: str) -> str:
        """Tool: request_human_assistance - Pausar bot para atendimento humano"""
        try:
            logger.info(f"🛑 Tool request_human_assistance chamada para {phone}")
            
            # 1. Verificar se a clínica está aberta AGORA
            is_open, message = self._is_clinic_open_now()
            
            if not is_open:
                # Clínica fechada - NÃO criar pausa, bot continua ativo
                logger.info(f"🏥 Clínica fechada para {phone}: {message}")
                return "No momento não temos atendentes disponíveis. Mas posso te ajudar! Como posso te auxiliar?"
            
            # 2. Clínica aberta - prosseguir com transferência
            logger.info(f"🏥 Clínica aberta para {phone}: {message}")
            
            # 3. Deletar contexto existente completamente
            existing_context = db.query(ConversationContext).filter_by(phone=phone).first()
            if existing_context:
                db.delete(existing_context)
                logger.info(f"🗑️ Contexto deletado para {phone}")
            
            # 4. Remover qualquer pausa anterior (se existir)
            existing_pause = db.query(PausedContact).filter_by(phone=phone).first()
            if existing_pause:
                db.delete(existing_pause)
                logger.info(f"🗑️ Pausa anterior removida para {phone}")
            
            # 5. Criar nova pausa por 1 minuto (para teste)
            paused_until = datetime.utcnow() + timedelta(hours=2)
            paused_contact = PausedContact(
                phone=phone,
                paused_until=paused_until,
                reason="user_requested_human_assistance"
            )
            db.add(paused_contact)
            db.commit()
            
            logger.info(f"⏸️ Bot pausado para {phone} até {paused_until}")
            return "Claro! Vou encaminhar você para um de nossos atendentes agora! Para acelerar o processo, já pode nos contar como podemos te ajudar! 😊"
            
        except Exception as e:
            logger.error(f"Erro ao pausar bot para humano: {str(e)}")
            db.rollback()
            return f"Erro ao transferir para humano: {str(e)}"

    def _handle_end_conversation(self, tool_input: Dict, db: Session, phone: str) -> str:
        """Tool: end_conversation - Encerrar conversa e limpar contexto"""
        try:
            logger.info(f"🔚 Tool end_conversation chamada para {phone}")
            
            # Buscar e deletar contexto
            context = db.query(ConversationContext).filter_by(phone=phone).first()
            if context:
                db.delete(context)
                db.commit()
                logger.info(f"🗑️ Contexto deletado para {phone}")
            
            return "Foi um prazer atendê-lo(a)! Até logo! 😊"
            
        except Exception as e:
            logger.error(f"Erro ao encerrar conversa: {str(e)}")
            db.rollback()
            return f"Erro ao encerrar conversa: {str(e)}"
    
    def reload_clinic_info(self):
        """Recarrega informações da clínica do arquivo JSON"""
        logger.info("🔄 Recarregando informações da clínica...")
        self.clinic_info = load_clinic_info()
        logger.info("✅ Informações da clínica recarregadas!")


# Instância global do agente
ai_agent = ClaudeToolAgent()