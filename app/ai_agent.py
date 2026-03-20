"""
Agente de IA com Claude SDK para tirar dúvidas sobre a clínica.
Versão simplificada: responde dúvidas e redireciona ações para páginas web.
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import json
import logging
import pytz

from anthropic import Anthropic
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.simple_config import settings
from app.models import Appointment, AppointmentStatus, ConversationContext, PausedContact
from app.utils import load_clinic_info, normalize_phone, now_brazil, get_brazil_timezone

logger = logging.getLogger(__name__)


def format_closed_days(dias_fechados: List[str]) -> str:
    """Agrupa dias consecutivos e formata bonito"""
    if not dias_fechados:
        return ""

    dates = []
    for d in dias_fechados:
        try:
            dates.append(datetime.strptime(d, '%d/%m/%Y'))
        except Exception:
            continue

    dates.sort()

    groups = []
    current_group = [dates[0]]

    for i in range(1, len(dates)):
        if (dates[i] - current_group[-1]).days == 1:
            current_group.append(dates[i])
        else:
            groups.append(current_group)
            current_group = [dates[i]]
    groups.append(current_group)

    result = ""
    for group in groups:
        if len(group) == 1:
            result += f"• {group[0].strftime('%d/%m/%Y')}\n"
        else:
            result += f"• {group[0].strftime('%d/%m')} a {group[-1].strftime('%d/%m/%Y')}\n"

    return result


class ClaudeToolAgent:
    """Agente de IA para tirar dúvidas e redirecionar ações para páginas web"""

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
        horarios = self.clinic_info.get('horario_atendimento', self.clinic_info.get('horario_funcionamento', {}))

        horarios_str = ""
        for dia, horario in horarios.items():
            if horario != "FECHADO":
                horarios_str += f"• {dia.capitalize()}: {horario}\n"

        duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 45)
        secretaria = self.clinic_info.get('informacoes_adicionais', {}).get('secretaria', 'Beatriz')

        tipos_consulta = self.clinic_info.get('tipos_consulta', {})
        convenios = self.clinic_info.get('convenios_aceitos', {})
        info_adicionais = self.clinic_info.get('informacoes_adicionais', {})

        valores_str = ""
        for tipo, dados in tipos_consulta.items():
            nome = dados.get('nome', tipo)
            valor = dados.get('valor_particular', dados.get('valor', 0))
            valores_str += f"  • {nome}: R$ {valor} (valor particular)\n"
        valores_str += "  • Convênios (CABERGS/IPE): valor conforme categoria do plano\n"

        convenios_list = []
        for cod, dados in convenios.items():
            if cod.lower() != 'particular':
                convenios_list.append(dados.get('nome', cod))
        convenios_str = ", ".join(convenios_list) if convenios_list else "Nenhum"

        formas_pagamento = info_adicionais.get('formas_pagamento', [])
        pagamento_str = ", ".join(formas_pagamento) if formas_pagamento else "Não informado"

        cadeira_rodas = "Sim" if info_adicionais.get('cadeira_rodas_disponivel', False) else "Não"
        politica_cancelamento = info_adicionais.get('politica_cancelamento', 'Não informado')

        return f"""Você é a assistente virtual do {clinic_name}. Você tira dúvidas sobre a clínica de forma natural e conversacional.

[INFORMAÇÕES DA CLÍNICA]

📍 LOCALIZAÇÃO:
  • Nome: {clinic_name}
  • Endereço: {endereco}
  • Telefone: {self.clinic_info.get('telefone', 'Não informado')}

🕒 HORÁRIOS DE FUNCIONAMENTO:
{horarios_str}⏱️ Duração das consultas: {duracao} minutos

💰 VALORES DAS CONSULTAS:
{valores_str}
💳 CONVÊNIOS ACEITOS: {convenios_str}

💵 FORMAS DE PAGAMENTO: {pagamento_str}

ℹ️ OUTRAS INFORMAÇÕES:
  • Cadeira de rodas disponível: {cadeira_rodas}
  • Política de cancelamento: {politica_cancelamento}
  • Secretária: {secretaria}

[OBJETIVO]

Responder dúvidas dos pacientes sobre a clínica de forma natural e acolhedora.
Quando o paciente quiser realizar uma AÇÃO (marcar consulta, remarcar, cancelar, pedir receita, visita domiciliar), envie o link correspondente.

[LINKS PARA AÇÕES]

  • Marcar consulta (incluindo domiciliar): __LINK_AGENDAR__
  • Solicitar receita: __LINK_RECEITA__

[COMO RESPONDER]

DÚVIDAS (responda direto):
- Horários, preços, convênios, endereço, formas de pagamento, cadeira de rodas, etc.
- Responda de forma NATURAL e CONVERSACIONAL, como uma secretária real faria
- NÃO use blocos formatados ou templates - responda de forma fluida
- NÃO precisa chamar a tool get_clinic_info para perguntas simples - você já tem todas as informações acima
- VALORES: Só informe valores quando o usuário PERGUNTAR especificamente
- Se não souber responder algo específico, diga educadamente que vai verificar com a doutora
- DISPONIBILIDADE DE DATAS: NÃO informe datas específicas de fechamento (feriados, férias). Diga que o paciente pode ver as datas disponíveis pelo link de agendamento. Pode haver exceções nos dias normais de atendimento.

AÇÕES:
- Se o paciente quiser marcar consulta (incluindo domiciliar) → mande o link de agendamento com uma explicação acolhedora. Exemplo: "Claro! É só tocar no link abaixo para agendar sua consulta. Lá você escolhe o dia e horário que preferir 😊\n\n👉 __LINK_AGENDAR__"
- Se quiser solicitar receita → mande o link de receita com uma explicação acolhedora. Exemplo: "Claro! É só tocar no link abaixo para pedir sua receita. Lá você coloca o nome dos medicamentos que precisa 😊\n\n👉 __LINK_RECEITA__"
- SEMPRE coloque o emoji 👉 antes do link para facilitar a visualização
- Se quiser remarcar ou cancelar → use a tool request_human_assistance para transferir para a secretária
- NÃO colete dados do paciente (nome, nascimento, etc.)
- NÃO faça agendamento por aqui
- Seja breve e direto

FALAR COM HUMANO:
- Se o paciente pedir para falar com a secretária ou atendente → use a tool request_human_assistance
- Execute imediatamente sem perguntar confirmação

STATUS DE RECEITA:
- Se o paciente perguntar se a receita está pronta → diga que quando estiver pronta, a clínica entra em contato. Se for urgente, ligue para a Dra. Rose: (51) 99954-6355
- NÃO diga que vai verificar ou retornar com novidades — você não tem acesso ao status da receita

EMERGÊNCIA:
- Em caso de emergência, oriente a ligar para a Dra. Rose: (51) 99954-6355

[CICLO DE ATENDIMENTO]

Após responder qualquer dúvida ou enviar um link:
- Pergunte: "Posso te ajudar com mais alguma coisa?"
- Se o usuário responder negativamente (não, obrigado, tchau) → use a tool end_conversation
- Se responder positivamente ou fizer nova pergunta → continue ajudando

[REGRAS]

- NÃO apresente menu de opções numeradas
- NÃO colete dados do paciente (nome, data de nascimento, CPF, etc.)
- NÃO faça agendamento, cancelamento ou criação de receita por aqui
- NÃO dê conselhos médicos ou diagnósticos
- Seja natural, acolhedora e conversacional
- Adapte-se ao estilo do usuário (formal ou informal)
- Mantenha respostas concisas e úteis"""

    def _get_system_prompt_for(self, phone: str) -> str:
        """Retorna o system prompt com links personalizados pro telefone do paciente."""
        links = self.clinic_info.get('links', {})
        base_agendar = links.get('agendar', '')
        base_receita = links.get('receita', '')

        sep = '&' if '?' in base_agendar else '?'
        link_agendar = f"{base_agendar}{sep}tel={phone}" if phone else base_agendar
        sep = '&' if '?' in base_receita else '?'
        link_receita = f"{base_receita}{sep}tel={phone}" if phone else base_receita

        return (
            self.system_prompt
            .replace('__LINK_AGENDAR__', link_agendar)
            .replace('__LINK_RECEITA__', link_receita)
        )

    def _define_tools(self) -> List[Dict]:
        """Define as tools disponíveis para o Claude"""
        return [
            {
                "name": "get_clinic_info",
                "description": "GERALMENTE NÃO PRECISA CHAMAR - você já tem as informações da clínica no system prompt. Use APENAS para obter lista completa de dias fechados (closed_days). Para preços, horários, endereço, convênios - responda direto com as informações que você já tem.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "request_human_assistance",
                "description": "Transferir atendimento para a SECRETÁRIA quando solicitado explicitamente. Use APENAS quando o usuário solicitar claramente falar com secretária ou atendente humano (ex: 'quero falar com a secretária', 'preciso de atendente', 'pode transferir'). NÃO use para saudações casuais ou menções à doutora. Execute imediatamente sem perguntar confirmação.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "end_conversation",
                "description": "Encerrar conversa e limpar contexto quando o usuário indicar claramente que não precisa de mais nada (ex: 'não', 'não preciso', 'não obrigado', 'só isso', 'tchau', 'até logo'). Use APENAS após perguntar 'Posso te ajudar com mais alguma coisa?' e receber resposta negativa.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        ]

    def _handle_secretary_pause(self, db: Session, phone: Optional[str]) -> None:
        """Pausa silenciosamente o contato por 24 horas quando secretária envia /pause"""
        if not phone:
            return

        try:
            logger.info(f"⏸️ Pausa manual da secretária aplicada para {phone}")

            existing_context = db.query(ConversationContext).filter_by(phone=phone).first()
            if existing_context:
                db.delete(existing_context)
                logger.info(f"🗑️ Contexto deletado para {phone} (pausa manual da secretária)")

            existing_pause = db.query(PausedContact).filter_by(phone=phone).first()
            if existing_pause:
                db.delete(existing_pause)
                logger.info(f"🗑️ Pausa anterior removida para {phone} (pausa manual da secretária)")

            paused_until = datetime.utcnow() + timedelta(hours=24)
            paused_contact = PausedContact(
                phone=phone,
                paused_until=paused_until,
                reason="secretary_manual_pause"
            )
            db.add(paused_contact)
            db.commit()

            logger.info(f"⏸️ Contato {phone} pausado pela secretária até {paused_until}")
        except Exception as exc:
            logger.error(f"❌ Erro ao aplicar pausa manual da secretária: {exc}")
            db.rollback()

    def _is_clinic_open_now(self) -> tuple:
        """Verifica se a clínica está aberta no momento atual"""
        try:
            now = now_brazil()
            weekday = now.weekday()

            dias_semana = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
            dia_atual = dias_semana[weekday]

            horarios = self.clinic_info.get('horario_atendimento', self.clinic_info.get('horario_funcionamento', {}))
            horario_dia = horarios.get(dia_atual, 'FECHADO')

            if horario_dia == 'FECHADO':
                return False, f"Clínica fechada às {dia_atual}s"

            try:
                inicio_str, fim_str = horario_dia.split('-')
                hora_inicio = datetime.strptime(inicio_str.strip(), '%H:%M').time()
                hora_fim = datetime.strptime(fim_str.strip(), '%H:%M').time()

                hora_atual = now.time()
                if hora_inicio <= hora_atual <= hora_fim:
                    return True, f"Clínica aberta ({horario_dia})"
                else:
                    return False, f"Fora do horário ({horario_dia})"
            except Exception:
                return False, "Horário não determinado"

        except Exception as e:
            logger.error(f"Erro ao verificar horário: {e}")
            return False, "Erro ao verificar"

    def _format_clinic_hours(self) -> str:
        """Formata horários de funcionamento"""
        horarios = self.clinic_info.get('horario_atendimento', self.clinic_info.get('horario_funcionamento', {}))
        result = ""
        for dia, horario in horarios.items():
            result += f"• {dia.capitalize()}: {horario}\n"
        return result

    def _format_closed_days(self) -> str:
        """Formata dias fechados"""
        dias_fechados = self.clinic_info.get('dias_fechados', [])
        return format_closed_days(dias_fechados) if dias_fechados else "Nenhum dia especial fechado."

    def _format_consultation_prices(self) -> str:
        """Formata preços das consultas"""
        tipos = self.clinic_info.get('tipos_consulta', {})
        result = ""
        for tipo, dados in tipos.items():
            nome = dados.get('nome', tipo)
            valor = dados.get('valor_particular', dados.get('valor', 0))
            result += f"• {nome}: R$ {valor} (particular)\n"
        result += "• Convênios (CABERGS/IPE): valor conforme categoria do plano\n"
        return result

    def _format_insurance_list(self) -> str:
        """Formata lista de convênios"""
        convenios = self.clinic_info.get('convenios_aceitos', {})
        items = []
        for cod, dados in convenios.items():
            if cod.lower() != 'particular':
                items.append(f"• {dados.get('nome', cod)}")
        return "\n".join(items) if items else "Convênios não informados."

    def process_message(self, message: str, phone: str, db: Session) -> str:
        """Processa uma mensagem do usuário e retorna a resposta"""
        try:
            # 1. Carregar ou criar contexto
            context = db.query(ConversationContext).filter_by(phone=phone).first()
            if not context:
                context = ConversationContext(
                    phone=phone,
                    messages=[],
                    status="active"
                )
                db.add(context)
                logger.info(f"🆕 Novo contexto criado para {phone}")
            else:
                logger.info(f"📱 Contexto carregado para {phone}: {len(context.messages)} mensagens")

            # 2. Verificar se deve encerrar contexto por despedida
            normalized_phone = normalize_phone(phone)
            if self._should_end_context(context, message):
                logger.info(f"🔚 Encerrando contexto para {phone} por despedida")
                db.delete(context)
                db.commit()
                return "Foi um prazer atender você! Até logo!"

            # 4. Adicionar mensagem ao histórico
            context.messages.append({
                "role": "user",
                "content": message,
                "timestamp": datetime.utcnow().isoformat()
            })
            flag_modified(context, 'messages')

            # 5. Preparar mensagens para Claude
            claude_messages = []
            for msg in context.messages:
                claude_messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })

            # 6. Chamar Claude
            logger.info(f"🤖 Enviando {len(claude_messages)} mensagens para Claude")
            system_prompt = self._get_system_prompt_for(normalized_phone)
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                temperature=0.3,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"}
                }],
                messages=claude_messages,
                tools=self.tools
            )

            # 7. Processar resposta
            bot_response = self._process_claude_response(response, claude_messages, db, phone)

            # 8. Salvar resposta no histórico
            context.messages.append({
                "role": "assistant",
                "content": bot_response,
                "timestamp": datetime.utcnow().isoformat()
            })
            flag_modified(context, 'messages')
            context.last_activity = datetime.utcnow()
            db.commit()

            logger.info(f"💾 Contexto salvo para {phone}: {len(context.messages)} mensagens")
            return bot_response

        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {str(e)}")
            return "Desculpe, ocorreu um erro. Tente novamente em alguns instantes."

    def _process_claude_response(self, response, claude_messages: list, db: Session, phone: str) -> str:
        """Processa a resposta do Claude, incluindo chamadas de tools"""
        if not response.content:
            return "Desculpe, não consegui processar sua mensagem. Tente novamente."

        content = response.content[0]

        if content.type == "text":
            return content.text

        if content.type == "tool_use":
            max_iterations = 3
            current_response = response

            for iteration in range(max_iterations):
                if not current_response.content:
                    break

                content = current_response.content[0]

                if content.type == "text":
                    return content.text

                if content.type == "tool_use":
                    tool_result = self._execute_tool(content.name, content.input, db, phone)

                    # end_conversation retorna imediatamente
                    if content.name == "end_conversation":
                        return tool_result

                    # Continuar conversa com resultado da tool
                    current_response = self.client.messages.create(
                        model="claude-sonnet-4-20250514",
                        max_tokens=1500,
                        temperature=0.3,
                        system=[{
                            "type": "text",
                            "text": self.system_prompt,
                            "cache_control": {"type": "ephemeral"}
                        }],
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
                else:
                    break

            # Fallback: verificar última resposta
            if current_response.content and current_response.content[0].type == "text":
                return current_response.content[0].text

            return tool_result if 'tool_result' in dir() else "Desculpe, não consegui processar sua mensagem."

        return "Desculpe, não consegui processar sua mensagem. Tente novamente."

    def _execute_tool(self, tool_name: str, tool_input: Dict, db: Session, phone: str = None) -> str:
        """Executa uma tool específica"""
        try:
            logger.info(f"🔧 Executando tool: {tool_name} com input: {tool_input}")

            if tool_name == "get_clinic_info":
                return self._handle_get_clinic_info(tool_input, db, phone)
            elif tool_name == "request_human_assistance":
                return self._handle_request_human_assistance(tool_input, db, phone)
            elif tool_name == "end_conversation":
                return self._handle_end_conversation(tool_input, db, phone)

            logger.warning(f"❌ Tool não reconhecida: {tool_name}")
            return "Desculpe, ocorreu um problema técnico. Por favor, tente novamente."
        except Exception as e:
            logger.error(f"Erro ao executar tool {tool_name}: {str(e)}")
            return "Desculpe, ocorreu um erro ao processar sua solicitação."

    def _handle_get_clinic_info(self, tool_input: Dict, db: Session, phone: Optional[str]) -> str:
        """Tool: get_clinic_info - Retorna informações da clínica"""
        try:
            nome_clinica = self.clinic_info.get('nome_clinica', 'Clínica')
            endereco = self.clinic_info.get('endereco', 'Não informado')
            telefone = self.clinic_info.get('telefone', 'Não informado')

            resposta = [
                f"🏥 {nome_clinica}",
                "",
                f"📍 Endereço: {endereco}",
                f"📞 Telefone: {telefone}",
                "",
                "🕒 Horários de funcionamento:",
                self._format_clinic_hours(),
            ]

            dias_fechados = self.clinic_info.get('dias_fechados', [])
            if dias_fechados:
                resposta.extend([
                    "🚫 Dias especiais sem atendimento:",
                    self._format_closed_days()
                ])

            return "\n".join(resposta)

        except Exception as e:
            logger.error(f"Erro ao obter info da clínica: {str(e)}")
            return f"Erro ao buscar informações: {str(e)}"

    def _handle_request_human_assistance(self, tool_input: Dict, db: Session, phone: str) -> str:
        """Tool: request_human_assistance - Pausar bot para atendimento humano"""
        try:
            logger.info(f"🛑 Tool request_human_assistance chamada para {phone}")

            # Pausar sempre, independente do horário
            existing_context = db.query(ConversationContext).filter_by(phone=phone).first()
            if existing_context:
                db.delete(existing_context)

            existing_pause = db.query(PausedContact).filter_by(phone=phone).first()
            if existing_pause:
                db.delete(existing_pause)

            paused_until = datetime.utcnow() + timedelta(hours=24)
            paused_contact = PausedContact(
                phone=phone,
                paused_until=paused_until,
                reason="user_requested_human_assistance"
            )
            db.add(paused_contact)
            db.commit()

            logger.info(f"⏸️ Bot pausado para {phone} até {paused_until}")

            # Mensagem diferente conforme horário
            is_open, message = self._is_clinic_open_now()

            if is_open:
                return ("Vou transferir você para nossa secretária Beatriz agora! "
                        "Para agilizar, já pode nos contar como podemos te ajudar.\n\n"
                        "Em caso de emergência, ligue para a Dra. Rose: (51) 99954-6355")
            else:
                return ("Vou transferir você para nossa secretária Beatriz. "
                        "Neste momento estamos fora do horário de atendimento, "
                        "mas ela vai te responder assim que possível.\n\n"
                        "Em caso de emergência, ligue para a Dra. Rose: (51) 99954-6355")

        except Exception as e:
            logger.error(f"Erro ao pausar bot para humano: {str(e)}")
            db.rollback()
            return f"Erro ao transferir para humano: {str(e)}"

    def _handle_end_conversation(self, tool_input: Dict, db: Session, phone: str) -> str:
        """Tool: end_conversation - Encerrar conversa e limpar contexto"""
        try:
            logger.info(f"🔚 Tool end_conversation chamada para {phone}")

            context = db.query(ConversationContext).filter_by(phone=phone).first()
            if context:
                db.delete(context)
                db.commit()
                logger.info(f"🗑️ Contexto deletado para {phone}")

            return "Foi um prazer atender você! Até logo!"

        except Exception as e:
            logger.error(f"Erro ao encerrar conversa: {str(e)}")
            db.rollback()
            return f"Erro ao encerrar conversa: {str(e)}"

    def _detect_confirmation_intent(self, message: str) -> str:
        """Detecta intenção de confirmação (positive/negative/unclear)"""
        normalized = message.strip().lower()

        positive_keywords = [
            "sim", "s", "yes", "pode", "confirma", "confirmo", "ok",
            "claro", "perfeito", "certo", "positivo", "vou", "irei",
            "estarei", "vou sim", "com certeza", "beleza", "bora",
            "pode confirmar", "tá bom", "ta bom", "tudo bem"
        ]

        negative_keywords = [
            "não", "nao", "n", "no", "cancela", "cancelar",
            "desmarcar", "remarcar", "mudar", "trocar", "outro",
            "não posso", "nao posso", "não vou", "nao vou",
            "não consigo", "nao consigo", "infelizmente"
        ]

        for keyword in positive_keywords:
            if normalized == keyword or normalized.startswith(keyword + " ") or normalized.startswith(keyword + ","):
                return "positive"

        for keyword in negative_keywords:
            if normalized == keyword or normalized.startswith(keyword + " ") or normalized.startswith(keyword + ","):
                return "negative"

        return "unclear"

    def _should_end_context(self, context: ConversationContext, last_user_message: str) -> bool:
        """Verifica se deve encerrar o contexto baseado na última mensagem"""
        if not context.messages:
            return False

        # Verificar se última mensagem do assistente perguntou "mais alguma coisa?"
        last_assistant = None
        for msg in reversed(context.messages):
            if msg.get("role") == "assistant":
                last_assistant = msg.get("content", "").lower()
                break

        if not last_assistant:
            return False

        farewell_patterns = ["mais alguma coisa", "posso ajudar com mais", "precisa de mais"]
        has_farewell_question = any(pattern in last_assistant for pattern in farewell_patterns)

        if has_farewell_question:
            normalized = last_user_message.strip().lower()
            negative_responses = [
                "não", "nao", "n", "no", "nada", "só isso", "so isso",
                "não preciso", "nao preciso", "tchau", "até logo", "ate logo",
                "obrigado", "obrigada", "valeu", "brigado", "brigada",
                "não obrigado", "nao obrigado", "não obrigada", "nao obrigada",
                "era só isso", "era so isso", "é só isso", "e so isso",
                "não, obrigado", "não, obrigada"
            ]
            return any(normalized == resp or normalized.startswith(resp) for resp in negative_responses)

        return False

    def reload_clinic_info(self):
        """Recarrega informações da clínica do arquivo JSON"""
        logger.info("🔄 Recarregando informações da clínica...")
        self.clinic_info = load_clinic_info()
        self.system_prompt = self._create_system_prompt()
        logger.info("✅ Informações da clínica recarregadas!")


# Instância global do agente
ai_agent = ClaudeToolAgent()
