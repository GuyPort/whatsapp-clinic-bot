"""
Agente de IA com Claude SDK + Tools para agendamento de consultas.
Versão completa com menu estruturado e gerenciamento de contexto.
Corrigido: persistência de contexto + loop de processamento de tools.
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import json
import logging
from anthropic import Anthropic

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.simple_config import settings
from app.models import Appointment, AppointmentStatus, ConversationContext
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

5. **FLUXO CRÍTICO - Após receber horário:**
   a) Execute validate_and_check_availability com data e hora
   b) Leia o resultado da tool:
      - Se contém "disponível" → Execute IMEDIATAMENTE create_appointment
      - Se contém "não está disponível" → Explique e peça outro horário
      - Se contém "fora do horário" → Explique e peça outro horário
   c) NUNCA termine sem executar create_appointment quando disponível
   d) SEMPRE retorne uma mensagem amigável ao usuário após criar agendamento

REGRA IMPORTANTE: Você DEVE executar múltiplas tools em sequência quando necessário.
NÃO retorne "end_turn" após validate_and_check_availability se o horário está disponível!

ENCERRAMENTO DE CONVERSAS:
Após QUALQUER tarefa concluída (agendamento criado, cancelamento realizado, dúvida respondida):
- SEMPRE perguntar: "Posso te ajudar com mais alguma coisa?"
- Se SIM ou usuário fizer nova pergunta: continuar com contexto
- Se NÃO ou "não preciso de mais nada": executar tool 'end_conversation'

ATENDIMENTO HUMANO:
Se o usuário pedir para "falar com alguém", "atendente", "secretária", "humano", etc:
- Execute IMEDIATAMENTE a tool 'request_human_assistance'
- NÃO pergunte confirmação, execute direto

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
            },
            {
                "name": "request_human_assistance",
                "description": "Transferir atendimento para humano quando solicitado",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "end_conversation",
                "description": "Encerrar conversa e limpar contexto quando usuário não precisa de mais nada",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        ]

    def _extract_appointment_data_from_messages(self, messages: list) -> dict:
        """
        Extrai dados de agendamento do histórico de mensagens.
        Percorre as últimas mensagens para encontrar:
        - Nome do paciente
        - Data de nascimento
        - Data da consulta
        - Horário da consulta
        """
        data = {
            "patient_name": None,
            "patient_birth_date": None,
            "appointment_date": None,
            "appointment_time": None
        }
        
        logger.info(f"🔍 Extraindo dados de {len(messages)} mensagens")
        
        # Percorrer mensagens do mais recente para o mais antigo
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            
            # Pular mensagens do bot
            if msg.get("role") != "user":
                continue
            
            content = msg.get("content", "").strip()
            
            # Extrair horário (formato HH:MM)
            if not data["appointment_time"] and ":" in content:
                import re
                time_match = re.match(r'^(\d{1,2}):(\d{2})$', content)
                if time_match:
                    data["appointment_time"] = content
                    continue
            
            # Extrair data (formato DD/MM/AAAA)
            if "/" in content:
                import re
                date_match = re.match(r'^(\d{2})/(\d{2})/(\d{4})$', content)
                if date_match:
                    # Verificar se é data de nascimento ou data da consulta
                    day, month, year = date_match.groups()
                    year_int = int(year)
                    
                    # Se ano < 2010, provavelmente é data de nascimento
                    if year_int < 2010 and not data["patient_birth_date"]:
                        data["patient_birth_date"] = content
                    # Senão, é data da consulta
                    elif not data["appointment_date"]:
                        data["appointment_date"] = content
                    continue
            
            # Extrair nome (primeira mensagem que não é número e não tem formatação específica)
            if not data["patient_name"] and len(content) > 3 and not content.isdigit():
                # Verificar se não é o "Olá!" inicial ou opção do menu
                if content.lower() not in ["olá", "olá!", "oi", "oi!", "1", "2", "3"]:
                    data["patient_name"] = content
        
        logger.info(f"✅ Nome extraído: {data['patient_name']}")
        logger.info(f"✅ Data nascimento: {data['patient_birth_date']}")
        logger.info(f"✅ Data consulta: {data['appointment_date']}")
        logger.info(f"✅ Horário: {data['appointment_time']}")
        
        return data

    def process_message(self, message: str, phone: str, db: Session) -> str:
        """Processa uma mensagem do usuário e retorna a resposta com contexto persistente"""
        try:
            # 1. Carregar contexto do banco
            context = db.query(ConversationContext).filter_by(phone=phone).first()
            if not context:
                # Primeira mensagem deste usuário, criar contexto novo
                context = ConversationContext(
                    phone=phone,
                    messages=[],
                    status="active"
                )
                db.add(context)
                logger.info(f"🆕 Novo contexto criado para {phone}")
            else:
                logger.info(f"📱 Contexto carregado para {phone}: {len(context.messages)} mensagens")
            
            # 2. Verificar timeout de inatividade (30 minutos)
            if context.last_activity:
                inactivity = datetime.utcnow() - context.last_activity
                if inactivity > timedelta(minutes=30):
                    # Contexto expirou - limpar e avisar
                    logger.info(f"⏰ Contexto expirado por inatividade para {phone}")
                    context.messages = []
                    context.flow_data = {}
                    context.status = "expired"
                    flag_modified(context, 'messages')
                    flag_modified(context, 'flow_data')
                    
                    # Adicionar mensagem de aviso ao início
                    context.messages.append({
                        "role": "assistant",
                        "content": "Olá! Como você ficou um tempo sem responder, encerramos a sessão anterior. Vamos recomeçar! 😊\n\nComo posso te ajudar hoje?\n1 Marcar consulta\n2 Remarcar/Cancelar consulta\n3 Tirar dúvidas",
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    flag_modified(context, 'messages')
                    context.last_activity = datetime.utcnow()
                    db.commit()
                    return context.messages[-1]["content"]
            
            # 3. Adicionar mensagem do usuário ao histórico
            context.messages.append({
                "role": "user",
                "content": message,
                "timestamp": datetime.utcnow().isoformat()
            })
            flag_modified(context, 'messages')
            
            # 4. Preparar mensagens para Claude (histórico completo)
            claude_messages = []
            for msg in context.messages:
                claude_messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })
            
            # 5. Fazer chamada para o Claude com histórico completo
            logger.info(f"🤖 Enviando {len(claude_messages)} mensagens para Claude")
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=2000,
                temperature=0.1,
                system=self.system_prompt,
                messages=claude_messages,  # ✅ HISTÓRICO COMPLETO!
                tools=self.tools
            )
            
            # 6. Processar resposta do Claude
            if response.content:
                content = response.content[0]
                
                if content.type == "text":
                    bot_response = content.text
                elif content.type == "tool_use":
                    # Loop para processar múltiplas tools em sequência
                    max_iterations = 5  # Limite de segurança para evitar loops infinitos
                    iteration = 0
                    current_response = response
                    
                    while iteration < max_iterations:
                        iteration += 1
                        
                        # Verificar se há content na resposta
                        if not current_response.content or len(current_response.content) == 0:
                            logger.warning(f"⚠️ Iteration {iteration}: Claude retornou resposta vazia")
                            # Se há tool_result anterior, usar como fallback
                            if 'tool_result' in locals():
                                # Se tool_result indica disponibilidade, tentar criar agendamento automaticamente
                                if "disponível" in tool_result.lower() and "validate_and_check_availability" in str(locals()):
                                    # Extrair dados das mensagens e criar agendamento diretamente
                                    logger.warning("⚠️ Claude não criou agendamento, fazendo fallback automático")
                                    try:
                                        # Extrair dados do histórico de mensagens
                                        appointment_data = self._extract_appointment_data_from_messages(context.messages)
                                        
                                        # Adicionar telefone do paciente (disponível no contexto phone)
                                        appointment_data["patient_phone"] = phone
                                        
                                        logger.info(f"📋 Dados extraídos: {appointment_data}")
                                        
                                        # Validar se todos os dados foram extraídos
                                        if all(appointment_data.values()):
                                            appointment_result = self._handle_create_appointment(appointment_data, db)
                                            bot_response = f"Perfeito! {appointment_result}"
                                        else:
                                            logger.error(f"❌ Dados incompletos extraídos: {appointment_data}")
                                            bot_response = "Desculpe, não consegui extrair todos os dados necessários. Vamos tentar novamente?"
                                    except Exception as e:
                                        logger.error(f"Erro no fallback automático: {e}", exc_info=True)
                                        bot_response = tool_result
                                else:
                                    bot_response = tool_result
                            else:
                                bot_response = "Desculpe, não consegui processar sua solicitação completamente."
                            break
                        
                        content = current_response.content[0]
                        
                        if content.type == "text":
                            # Claude retornou texto final, sair do loop
                            bot_response = content.text
                            break
                        elif content.type == "tool_use":
                            # Executar tool
                            tool_result = self._execute_tool(content.name, content.input, db, phone)
                            
                            # Verificação especial para validate_and_check_availability
                            if content.name == "validate_and_check_availability":
                                if "disponível" in tool_result.lower() and "não" not in tool_result.lower():
                                    # Horário disponível, adicionar hint para Claude criar agendamento
                                    tool_result += "\n\n[SYSTEM: Execute create_appointment agora com os dados coletados: nome, data_nascimento, data_consulta, horario_consulta]"
                            
                            logger.info(f"🔧 Iteration {iteration}: Tool {content.name} result: {tool_result[:200] if len(tool_result) > 200 else tool_result}")
                            
                            # Fazer follow-up com o resultado
                            current_response = self.client.messages.create(
                                model="claude-3-5-sonnet-20241022",
                                max_tokens=2000,
                                temperature=0.1,
                                system=self.system_prompt,
                                messages=claude_messages + [
                                    {"role": "assistant", "content": current_response.content},
                                    {
                                        "role": "user",
                                        "content": [
                                            {
                                                "type": "tool_result",
                                                "tool_use_id": content.id,
                                                "content": tool_result
                                            }
                                        ]
                                    }
                                ]
                            )
                            logger.info(f"📋 Response content length: {len(current_response.content) if current_response.content else 0}")
                            logger.info(f"📋 Response stop_reason: {current_response.stop_reason}")
                            
                            # Continuar loop para processar próxima resposta
                        else:
                            # Tipo desconhecido, sair do loop
                            logger.warning(f"⚠️ Tipo de conteúdo desconhecido: {content.type}")
                            bot_response = tool_result if 'tool_result' in locals() else "Desculpe, não consegui processar sua mensagem."
                            break
                    
                    # Se atingiu o limite de iterações sem retornar texto
                    if iteration >= max_iterations:
                        logger.error(f"❌ Limite de iterações atingido ({max_iterations})")
                        if 'tool_result' in locals():
                            logger.info(f"📤 Usando último tool_result como resposta")
                            bot_response = tool_result
                        else:
                            bot_response = "Desculpe, houve um problema ao processar sua solicitação. Tente novamente."
                else:
                    bot_response = "Desculpe, não consegui processar sua mensagem. Tente novamente."
            else:
                bot_response = "Desculpe, não consegui processar sua mensagem. Tente novamente."
            
            # 7. Salvar resposta do Claude no histórico
            context.messages.append({
                "role": "assistant",
                "content": bot_response,
                "timestamp": datetime.utcnow().isoformat()
            })
            flag_modified(context, 'messages')
            
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
                logger.warning(f"❌ Tool não reconhecida: {tool_name}")
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
            
            # 2. Validar horário de funcionamento
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
            
            # 3. Verificar se horário está dentro do funcionamento
            try:
                hora_consulta = datetime.strptime(time_str, '%H:%M').time()
                hora_inicio, hora_fim = horario_dia.split('-')
                hora_inicio = datetime.strptime(hora_inicio, '%H:%M').time()
                hora_fim = datetime.strptime(hora_fim, '%H:%M').time()
                
                if not (hora_inicio <= hora_consulta <= hora_fim):
                    logger.warning(f"❌ Horário {time_str} fora do funcionamento")
                    return f"❌ Horário inválido! A clínica funciona das {hora_inicio.strftime('%H:%M')} às {hora_fim.strftime('%H:%M')} aos {weekday_pt}s.\n" + \
                           f"Por favor, escolha um horário entre {hora_inicio.strftime('%H:%M')} e {hora_fim.strftime('%H:%M')}."
                           
            except ValueError:
                logger.warning(f"❌ Formato de horário inválido: {time_str}")
                return "Formato de horário inválido. Use HH:MM (ex: 14:30)."
            
            # 4. Verificar disponibilidade no banco de dados
            appointment_datetime = datetime.combine(appointment_date.date(), hora_consulta)
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 45)
            interval = self.clinic_info.get('regras_agendamento', {}).get('intervalo_entre_consultas_minutos', 15)
            
            # Buscar consultas conflitantes
            day_start = appointment_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            
            existing_appointments = db.query(Appointment).filter(
                Appointment.appointment_date >= day_start.date(),
                Appointment.appointment_date < day_end.date(),
                Appointment.status == AppointmentStatus.AGENDADA
            ).all()
            
            # Verificar conflitos
            slot_end = appointment_datetime + timedelta(minutes=duracao)
            
            for appointment in existing_appointments:
                app_start = datetime.combine(appointment.appointment_date, appointment.appointment_time)
                app_end = app_start + timedelta(minutes=appointment.duration_minutes + interval)
                
                # Verificar sobreposição
                if not (slot_end <= app_start or appointment_datetime >= app_end):
                    logger.warning(f"❌ Conflito encontrado: {appointment.patient_name} das {app_start.strftime('%H:%M')} às {app_end.strftime('%H:%M')}")
                    return f"❌ Horário {time_str} já está ocupado. Por favor, escolha outro horário."
            
            logger.info(f"✅ Horário {time_str} disponível!")
            return f"✅ Horário {time_str} disponível! Pode prosseguir com o agendamento."
            
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
            available_slots = appointment_rules.get_available_slots(appointment_datetime.date(), duracao, db)
            
            if appointment_datetime.time() not in [slot.time() for slot in available_slots]:
                return f"❌ Horário {appointment_time} não está disponível. Use a tool check_availability para ver horários disponíveis."
            
            # Criar agendamento
            appointment = Appointment(
                patient_name=patient_name,
                patient_phone=normalized_phone,
                patient_birth_date=patient_birth_date,  # Manter como string
                appointment_date=appointment_datetime.date(),
                appointment_time=appointment_datetime.time(),
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
            
            # Buscar contexto pelo phone
            context = db.query(ConversationContext).filter_by(phone=phone).first()
            if not context:
                context = ConversationContext(phone=phone)
                db.add(context)
            
            # Pausar por 2 horas
            context.status = "paused_human"
            context.paused_until = datetime.utcnow() + timedelta(hours=2)
            context.messages = []  # Limpar contexto
            context.flow_data = {}
            flag_modified(context, 'messages')
            flag_modified(context, 'flow_data')
            context.last_activity = datetime.utcnow()
            db.commit()
            
            logger.info(f"⏸️ Bot pausado para {phone} até {context.paused_until}")
            return "Claro! Vou transferir você para nossa equipe. Um momento! 🙋"
            
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


# Instância global do agente
ai_agent = ClaudeToolAgent()
