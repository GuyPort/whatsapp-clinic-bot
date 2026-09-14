"""
Agente de IA com Claude SDK para tirar dúvidas sobre a clínica.
Versão simplificada: responde dúvidas e redireciona ações para páginas web.
"""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable, Dict, Any, List
import logging
import re

from anthropic import Anthropic

from app.simple_config import settings
from app.conversation_state import (
    AgentIntent, AgentResult, AgentResponseInvalid, AgentToolUnavailable,
    AgentUnavailable, ConversationDomainError, ConversationSnapshot,
    FailureReason, InvalidCanonicalContact, ToolOutcome,
)
from app.utils import (
    AuditEvent, ConversationAuditLogger, get_brazil_timezone, load_clinic_info,
    new_audit_correlation_id,
)

logger = logging.getLogger(__name__)
conversation_audit = ConversationAuditLogger(logger)


def _emit_audit(event: AuditEvent, **fields: object) -> None:
    conversation_audit.emit(event, correlation_id=new_audit_correlation_id(), **fields)


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

    def __init__(self, client: Any = None, clinic_info: dict | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.client = client if client is not None else Anthropic(api_key=settings.anthropic_api_key)
        self.clinic_info = clinic_info if clinic_info is not None else load_clinic_info()
        self.clock = clock if clock is not None else datetime.utcnow
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
            obs = dados.get('observacao', '')
            if obs:
                valores_str += f"  • {nome}: R$ {valor} ({obs})\n"
            else:
                valores_str += f"  • {nome}: R$ {valor} (valor particular)\n"
        valores_str += "  • Convênios (CABERGS/IPE): valor conforme categoria do plano (exceto domiciliar)\n"

        convenios_list = []
        for cod, dados in convenios.items():
            if cod.lower() != 'particular':
                convenios_list.append(dados.get('nome', cod))
        convenios_str = ", ".join(convenios_list) if convenios_list else "Nenhum"

        formas_pagamento = info_adicionais.get('formas_pagamento', [])
        pagamento_str = ", ".join(formas_pagamento) if formas_pagamento else "Não informado"

        cadeira_rodas = "Sim" if info_adicionais.get('cadeira_rodas_disponivel', False) else "Não"
        politica_cancelamento = info_adicionais.get('politica_cancelamento', 'Não informado')
        valor_receita = info_adicionais.get('valor_receita', 'Não informado')

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
  • Valor da receita (solicitação avulsa): {valor_receita}

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

    def _is_clinic_open_now(self) -> tuple:
        """Verifica se a clínica está aberta no momento atual"""
        try:
            now = self.clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            now = now.astimezone(self.timezone)
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

        except ConversationDomainError:
            raise
        except Exception:
            raise AgentToolUnavailable(FailureReason.TOOL_UNAVAILABLE) from None

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

    def prepare_result(self, message: str, phone: str, snapshot: ConversationSnapshot,
                       *, authorize: Callable[[], None] | None = None) -> AgentResult:
        """Produz texto e intenção sem consultar ou alterar estado persistente."""
        if not isinstance(phone, str) or re.fullmatch(r"[1-9][0-9]{9,14}", phone) is None:
            raise InvalidCanonicalContact(FailureReason.INVALID_CANONICAL_CONTACT)
        if not isinstance(snapshot, ConversationSnapshot):
            raise AgentResponseInvalid(FailureReason.INVALID_AGENT_SNAPSHOT)
        if snapshot.phone != phone:
            raise InvalidCanonicalContact(FailureReason.INVALID_CANONICAL_CONTACT)

        try:
            # Neither the provider nor the returned delta shares mutable history
            # with the snapshot owned by the conversation authority.
            history = deepcopy(snapshot.messages)
            flow_data = deepcopy(snapshot.flow_data)
            if self._should_end_context(snapshot, message):
                return AgentResult("Foi um prazer atender você! Até logo!", [], None, {},
                                   AgentIntent.CLOSE_CONTEXT)

            history.append({
                "role": "user",
                "content": message,
                "timestamp": self.clock().isoformat(),
            })
            claude_messages = [
                {"role": item["role"], "content": deepcopy(item["content"])}
                for item in history
            ]
            system_prompt = self._get_system_prompt_for(phone)
            response = self._call_claude(claude_messages, system_prompt, authorize=authorize)
            outcome = self._process_claude_response(response, claude_messages, phone, system_prompt,
                                                    authorize=authorize)
            if outcome.intent is not None:
                return AgentResult(outcome.content, [], None, {}, outcome.intent)

            history.append({
                "role": "assistant",
                "content": outcome.content,
                "timestamp": self.clock().isoformat(),
            })
            return AgentResult(outcome.content, history, snapshot.current_flow, flow_data,
                               AgentIntent.SAVE_CONTEXT)
        except ConversationDomainError:
            raise
        except Exception:
            raise AgentUnavailable(FailureReason.AGENT_UNAVAILABLE) from None

    def _call_claude(self, messages: list, system_prompt: str, *, authorize=None):
        """Mantém a mesma configuração e os links canônicos em cada rodada."""
        _emit_audit(AuditEvent.PROCESSING, outcome="agent_started", attempt_state="provider_call")
        try:
            if authorize is not None:
                authorize()  # Optional only for isolated, authority-free adapter callers.
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                temperature=0.3,
                thinking={"type": "disabled"},
                extra_body={"output_config": {"effort": "low"}},
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=deepcopy(messages),
                tools=deepcopy(self.tools),
            )
        except ConversationDomainError:
            _emit_audit(AuditEvent.PROCESSING, outcome="dependency_unavailable",
                        attempt_state="provider_failed")
            raise
        except Exception:
            _emit_audit(AuditEvent.PROCESSING, outcome="dependency_unavailable",
                        attempt_state="provider_failed")
            raise AgentUnavailable(FailureReason.AGENT_UNAVAILABLE) from None
        _emit_audit(AuditEvent.PROCESSING, outcome="agent_completed", attempt_state="provider_returned")
        return response

    def _process_claude_response(self, response, claude_messages: list, phone: str,
                                 system_prompt: str, *, authorize=None) -> ToolOutcome:
        """Resolve até três rodadas de tools, sempre conservando a intenção."""
        for iteration in range(4):
            blocks = getattr(response, "content", None)
            if not isinstance(blocks, list) or not blocks:
                raise AgentResponseInvalid(FailureReason.INVALID_AGENT_RESPONSE)

            texts = []
            tool_blocks = []
            assistant_blocks = []
            for block in blocks:
                if getattr(block, "type", None) == "text" and isinstance(block.text, str):
                    texts.append(block.text)
                    assistant_blocks.append({"type": "text", "text": block.text})
                elif (getattr(block, "type", None) == "tool_use"
                      and isinstance(block.id, str) and block.id
                      and isinstance(block.name, str)):
                    tool_blocks.append(block)
                    assistant_blocks.append({
                        "type": "tool_use", "id": block.id,
                        "name": block.name, "input": deepcopy(block.input),
                    })
                else:
                    raise AgentResponseInvalid(FailureReason.INVALID_AGENT_RESPONSE)

            if not tool_blocks:
                text = "\n".join(texts)
                if not text.strip():
                    raise AgentResponseInvalid(FailureReason.INVALID_AGENT_RESPONSE)
                return ToolOutcome(text)
            if iteration == 3:
                raise AgentResponseInvalid(FailureReason.TOOL_ITERATION_LIMIT)
            if len({block.id for block in tool_blocks}) != len(tool_blocks):
                raise AgentResponseInvalid(FailureReason.INVALID_AGENT_RESPONSE)

            outcomes = [self._execute_tool(block.name, block.input, phone) for block in tool_blocks]
            intents = {outcome.intent for outcome in outcomes if outcome.intent is not None}
            if len(intents) > 1:
                raise AgentResponseInvalid(FailureReason.INVALID_AGENT_RESPONSE)
            if intents:
                return next(outcome for outcome in outcomes if outcome.intent is not None)

            claude_messages.extend([
                {"role": "assistant", "content": assistant_blocks},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": block.id, "content": outcome.content}
                    for block, outcome in zip(tool_blocks, outcomes)
                ]},
            ])
            response = self._call_claude(claude_messages, system_prompt, authorize=authorize)

        raise AgentResponseInvalid(FailureReason.TOOL_ITERATION_LIMIT)

    def _execute_tool(self, tool_name: str, tool_input: Dict, phone: str) -> ToolOutcome:
        """Executa somente cálculo local e retorna uma intenção tipada."""
        try:
            if not isinstance(phone, str) or re.fullmatch(r"[1-9][0-9]{9,14}", phone) is None:
                raise InvalidCanonicalContact(FailureReason.INVALID_CANONICAL_CONTACT)
            if not isinstance(tool_input, dict) or tool_input:
                raise AgentToolUnavailable(FailureReason.TOOL_UNAVAILABLE)
            if tool_name == "get_clinic_info":
                return self._handle_get_clinic_info()
            if tool_name == "request_human_assistance":
                return self._handle_request_human_assistance()
            if tool_name == "end_conversation":
                return self._handle_end_conversation()
            raise AgentToolUnavailable(FailureReason.TOOL_UNAVAILABLE)
        except ConversationDomainError:
            raise
        except Exception:
            raise AgentToolUnavailable(FailureReason.TOOL_UNAVAILABLE) from None

    def _handle_get_clinic_info(self) -> ToolOutcome:
        """Tool: get_clinic_info — informações sem transição de estado."""
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
                self._format_closed_days(),
            ])
        return ToolOutcome("\n".join(resposta))

    def _handle_request_human_assistance(self) -> ToolOutcome:
        """Tool: request_human_assistance — solicita pausa à autoridade central."""
        is_open, _ = self._is_clinic_open_now()
        if is_open:
            text = ("Vou transferir você para nossa secretária Beatriz agora! "
                    "Para agilizar, já pode nos contar como podemos te ajudar.\n\n"
                    "Em caso de emergência, ligue para a Dra. Rose: (51) 99954-6355")
        else:
            text = ("Vou transferir você para nossa secretária Beatriz. "
                    "Neste momento estamos fora do horário de atendimento, "
                    "mas ela vai te responder assim que possível.\n\n"
                    "Em caso de emergência, ligue para a Dra. Rose: (51) 99954-6355")
        return ToolOutcome(text, AgentIntent.PAUSE_FOR_SECRETARY)

    def _handle_end_conversation(self) -> ToolOutcome:
        """Tool: end_conversation — solicita encerramento à autoridade central."""
        return ToolOutcome("Foi um prazer atender você! Até logo!", AgentIntent.CLOSE_CONTEXT)

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

    def _should_end_context(self, context: ConversationSnapshot, last_user_message: str) -> bool:
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
