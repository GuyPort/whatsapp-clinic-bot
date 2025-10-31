"""
Agente de IA com Claude SDK + Tools para agendamento de consultas.
Versão completa com menu estruturado e gerenciamento de contexto.
Corrigido: persistência de contexto + loop de processamento de tools.
"""
from datetime import datetime, timedelta, time
from typing import Optional, Dict, Any, List, Tuple
import json
import logging
import pytz
from anthropic import Anthropic

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.mutable import MutableDict, MutableList

from app.simple_config import settings
from app.models import Appointment, AppointmentStatus, ConversationContext, PausedContact
from app.utils import (
    load_clinic_info, normalize_phone, parse_date_br, 
    format_datetime_br, now_brazil, get_brazil_timezone, round_up_to_next_5_minutes
)
from app.appointment_rules import appointment_rules

logger = logging.getLogger(__name__)


def format_closed_days(dias_fechados: List[str]) -> str:
    """
    Agrupa dias consecutivos e formata bonito para apresentação ao usuário.
    
    Args:
        dias_fechados: Lista de datas no formato "DD/MM/YYYY"
        
    Returns:
        String formatada com períodos agrupados
    """
    if not dias_fechados:
        return ""
    
    # Converter para datetime e ordenar
    dates = []
    for d in dias_fechados:
        try:
            dates.append(datetime.strptime(d, '%d/%m/%Y'))
        except (ValueError, TypeError):
            continue
    
    if not dates:
        return ""
    
    dates.sort()
    
    # Agrupar consecutivos
    groups = []
    current_group = [dates[0]]
    
    for i in range(1, len(dates)):
        if (dates[i] - current_group[-1]).days == 1:
            current_group.append(dates[i])
        else:
            groups.append(current_group)
            current_group = [dates[i]]
    groups.append(current_group)
    
    # Formatar
    result = ""
    for group in groups:
        if len(group) == 1:
            result += f"• {group[0].strftime('%d/%m/%Y')}\n"
        else:
            # Se começar e terminar no mesmo mês: "DD a DD/MM/YYYY"
            if group[0].month == group[-1].month and group[0].year == group[-1].year:
                result += f"• {group[0].strftime('%d')} a {group[-1].strftime('%d/%m/%Y')}\n"
            # Se mês diferente: "DD/MM a DD/MM/YYYY"
            else:
                result += f"• {group[0].strftime('%d/%m')} a {group[-1].strftime('%d/%m/%Y')}\n"
    
    return result


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
        secretaria = self.clinic_info.get('informacoes_adicionais', {}).get('secretaria', 'Beatriz')
        
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
   
   Para começar, preciso do seu nome completo e data de nascimento.
   
   Pode enviar da forma que preferir:
   • Tudo junto: 'João Silva, 07/08/2003'
   • Separado: envie o nome primeiro, depois a data
   • Natural: 'Sou João Silva, nasci em 07/08/2003'"

2. IMPORTANTE SOBRE EXTRAÇÃO DE DADOS:
   
   Para extrair dados do paciente do histórico de mensagens, use a tool 'extract_patient_data':
   - Use esta tool quando precisar identificar o nome REAL do paciente (não frases de pedido)
   - Use quando flow_data não tiver nome válido ou estiver incompleto
   - Esta tool valida automaticamente se um texto é nome real ou frase de solicitação
   
   Se receber AMBOS (nome + data completa): extraia e confirme, depois vá para tipo de consulta
   Se receber APENAS NOME: agradeça e peça "E sua data de nascimento (DD/MM/AAAA)?"
   Se receber APENAS DATA: agradeça e peça "E seu nome completo?"
   Se NENHUM for extraído: use tool extract_patient_data para buscar no histórico ou peça novamente
   
   VALIDAÇÕES OBRIGATÓRIAS:
   - NOME: Deve ter no mínimo 2 palavras (nome + sobrenome), deve ser nome REAL (não frase como "Eu Preciso Marcar Uma Consulta")
   - DATA: Deve ser completa (dia + mês + ano) no formato DD/MM/AAAA
   - Se nome tiver apenas 1 palavra: "Para o cadastro médico, preciso do nome completo (nome e sobrenome)"
   - Se data incompleta: "Preciso da data completa (dia, mês e ano). Ex: 07/08/2003"

   ═══════════════════════════════════════════════════════════
   VALIDAÇÃO DE DATA DE NASCIMENTO - REGRA ABSOLUTA
   ═══════════════════════════════════════════════════════════
   
   SUA ÚNICA RESPONSABILIDADE:
   1. Extrair a data da mensagem do usuário
   2. Verificar se existe "erro_data" na resposta Python
   3. Comunicar o resultado
   
   AÇÃO BASEADA EM erro_data:
   
   ▶ Se erro_data NÃO EXISTE (null/vazio):
     → Data foi APROVADA pelo Python
     → Aceite IMEDIATAMENTE e continue para próxima etapa
     → NUNCA questione a data
     → NUNCA pense "essa pessoa é muito nova/velha"
     → NUNCA valide manualmente
   
   ▶ Se erro_data EXISTE:
     → Repita exatamente o erro que Python retornou
     → Peça nova data
   
   REGRA DE OURO:
   Python é a ÚNICA fonte de verdade para datas!
   Se Python aprovou, você ACEITA. Ponto final.
   
   EXEMPLO CORRETO:
   Python retorna: {{"data": "10/10/2025", "erro_data": null}}
   Você pensa: "Python aprovou, então está OK!"
   Você responde: "Perfeito! Agora me informe qual tipo de consulta..."
   
   EXEMPLO ERRADO (NUNCA FAÇA):
   Python retorna: {{"data": "10/10/2025", "erro_data": null}}
   Você pensa: "Essa pessoa tem 15 dias, não pode marcar consulta..."
   Você responde: "Preciso de data válida..." ← ERRADO!
   
   ═══════════════════════════════════════════════════════════

   NOTA: A pessoa marcando pode estar agendando para outra 
   pessoa (mãe para bebê, filho para idoso, etc). Aceite 
   QUALQUER data passada aprovada pelo Python.

⚠️ IMPORTANTE: DUAS DATAS DIFERENTES

Você acabou de coletar a DATA DE NASCIMENTO.
Agora você vai coletar informações da consulta.

Quando perguntar "qual data deseja marcar a consulta?":
- Essa será a DATA DA CONSULTA (appointment_date)
- NÃO confunda com data de nascimento (patient_birth_date)
- São campos DIFERENTES!

FLUXO:
1. ✅ Nome + data nascimento (JÁ COLETADO)
2. → Tipo de consulta
3. → Convênio  
4. → Data CONSULTA ← Aqui é appointment_date!
5. → Horário

3. Após receber a data de nascimento:
   "Perfeito! Agora me informe qual tipo de consulta você deseja:
   
   1️⃣ Clínica Geral - R$ 300
   2️⃣ Geriatria Clínica e Preventiva - R$ 300
   3️⃣ Atendimento Domiciliar ao Paciente Idoso - R$ 500
   
   Digite o número da opção desejada:"

4. Após receber o tipo (1, 2 ou 3):
   "Ótimo! Você possui convênio médico?

   Trabalhamos com os seguintes convênios:
   • CABERGS
   • IPE

   📋 Como responder:
   • Se você TEM um desses convênios → Digite o nome (CABERGS ou IPE)
   • Se você NÃO TEM convênio → Responda apenas "Não"

   Vamos prosseguir com consulta particular se você não tiver convênio."
   
   ⚠️ IMPORTANTE: Se usuário responder negativamente (não tenho, sem convênio, etc):
         - Python marcará automaticamente como "Particular"
         - Continue para próxima etapa (data da consulta)
         - NÃO encerre a conversa
         - NÃO pergunte se precisa de mais alguma coisa
   
   IMPORTANTE: CLASSIFICAÇÃO DE RESPOSTA SOBRE CONVÊNIO
   
   Ao receber resposta sobre convênio, CLASSIFIQUE a intenção:
   
   1. NEGATIVA (usuário NÃO tem convênio):
      - Exemplos: "não", "não tenho", "não possuo", "sem convênio", "nenhum", "Não, eu não possuo nenhum convênio!"
      - Ação: insurance_plan = "particular" → Continue para próxima etapa (data)
      
   2. POSITIVA ESPECÍFICA (tem convênio E especificou qual):
      - Exemplos: "CABERGS", "IPE", "tenho IPE", "possuo CABERGS", "1", "2"
      - Ação: insurance_plan = nome do convênio → Continue para próxima etapa
      
   3. POSITIVA GENÉRICA (tem convênio MAS não especificou):
      - Exemplos: "sim", "tenho", "possuo", "tenho convênio sim"
      - Ação: Perguntar: "Qual convênio você possui? CABERGS ou IPE?"
      
   4. AMBÍGUA (não está claro):
      - Exemplos: respostas confusas ou irrelevantes
      - Ação: "Não entendi. Você possui convênio médico (CABERGS ou IPE) ou não possui?"
   
   REGRA CRÍTICA: Use seu entendimento de linguagem natural para classificar a INTENÇÃO, não apenas palavras específicas!
   
   ⚠️ REGRA CRÍTICA - CONVÊNIO:
   1. Resposta "não"/"nao"/"n" → SEMPRE marcar como "Particular"
   2. Resposta "CABERGS" ou contém "cabergs" → "CABERGS"
   3. Resposta "IPE" ou contém "ipe" → "IPE"
   4. Resposta "1" → "CABERGS"
   5. Resposta "2" → "IPE"
   6. Qualquer outra negativa (não tenho, sem convênio) → "Particular"
   7. Resposta confusa → Perguntar novamente de forma clara
   8. NUNCA assumir CABERGS como padrão

5. Após registrar o convênio:
   - NÃO peça data ou horário manualmente.
   - Informe: "Perfeito! Vou verificar automaticamente os próximos horários disponíveis (respeitando 48 horas de antecedência).".
   - Aguarde a automação sugerir o próximo horário (ela enviará a mensagem automaticamente, você não precisa chamar nenhuma tool).

6. Quando a automação enviar uma sugestão de horário:
   - Reforce a pergunta somente se o paciente parecer indeciso.
   - Se o paciente responder "sim"/"ok"/"pode ser": confirme a escolha com uma resposta positiva e siga o fluxo (o sistema concluirá o agendamento automaticamente).
   - Se o paciente responder "não"/"prefiro outro": responda com empatia dizendo que você vai buscar outra opção. A automação enviará a próxima sugestão.

7. Após três recusas seguidas:
   - Peça ao paciente: "Tudo bem! Me informe uma data que funcione para você no formato DD/MM/AAAA (ex: 25/11/2025). Se quiser, indique também o horário.".
   - Assim que o paciente informar, confirme que vai tentar essa data. A automação testará o horário automaticamente e retornará com uma nova proposta.

8. Quando o paciente aceitar uma sugestão ou uma data personalizada for aprovada, o sistema criará o agendamento e você deve apenas enviar a mensagem de sucesso padrão.

CICLO DE ATENDIMENTO CONTÍNUO:
1. Após QUALQUER tarefa concluída (agendamento, cancelamento, resposta a dúvida):
   - SEMPRE perguntar: "Posso te ajudar com mais alguma coisa?"
   
2. Se usuário responder "sim" ou fizer nova pergunta:
   - Se responder apenas "sim" sem contexto claro:
     * Responder: "Claro! Como posso ajudar você hoje?" e aguardar resposta do usuário
   - Se fizer pergunta/pedido claro:
     * Responder adequadamente usando as tools necessárias
     * Após resolver, perguntar novamente: "Posso te ajudar com mais alguma coisa?"
   - Se mensagem for ambígua/confusa:
     * Perguntar: "Como posso te ajudar? Você pode me dizer o que precisa?"
   - Manter TODO o contexto histórico (nome, data nascimento, etc.) durante o ciclo
   - Voltar ao passo 1 após resolver cada pedido
   
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
- confirm_time_slot: Confirmar horário escolhido pelo paciente (usado apenas em casos extraordinários)
- create_appointment: Criar novo agendamento
- search_appointments: Buscar agendamentos existentes
- cancel_appointment: Cancelar agendamento
- request_human_assistance: Transferir para atendimento humano
- extract_patient_data: Extrair nome completo, data de nascimento e demais dados do histórico
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
                "name": "confirm_time_slot",
                "description": "Confirmar e validar o horário escolhido pelo paciente",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "Data da consulta no formato DD/MM/AAAA"
                        },
                        "time": {
                            "type": "string",
                            "description": "Horário escolhido no formato HH:MM (apenas horas inteiras)"
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
                        },
                        "consultation_type": {
                            "type": "string",
                            "description": "Tipo de consulta: clinica_geral | geriatria | domiciliar"
                        },
                        "insurance_plan": {
                            "type": "string",
                            "description": "Convênio: CABERGS | IPE | particular"
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
                "name": "extract_patient_data",
                "description": "Extrair dados do paciente do histórico de mensagens. Use esta tool quando precisar identificar nome completo real do paciente (não frases de pedido como 'Eu Preciso Marcar Uma Consulta'), data de nascimento, tipo de consulta e convênio. Esta tool valida automaticamente se um texto é um nome real ou apenas uma frase de solicitação de agendamento.",
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
        """Extrai dados básicos de agendamento do histórico de mensagens.
        Versão simplificada: apenas detecção rápida de datas, horários e escolhas numéricas.
        Para extração de nome, confiar no Claude via tool extract_patient_data.
        """
        try:
            data = {
                "patient_name": None,  # NÃO extrair aqui - deixar Claude fazer
                "patient_birth_date": None,
                "appointment_date": None,
                "appointment_time": None,
                "consultation_type": None,
                "insurance_plan": None
            }
            logger.info(f"🔍 Extraindo dados básicos de {len(messages)} mensagens (versão simplificada)")
            import re
            from datetime import datetime
            
            # Processar em ORDEM CRONOLÓGICA (primeira mensagem primeiro)
            for i in range(0, len(messages)):
                msg = messages[i]
                if msg.get("role") != "user":
                    continue
                content = (msg.get("content") or "").strip()
                
                # 1. EXTRAÇÃO DE HORÁRIOS - Só extrair se já tiver data de consulta definida
                # Isso evita capturar horários de nascimento mencionados antes da etapa de agendamento
                if not data["appointment_time"] and data["appointment_date"]:
                    time_pattern = r'(\d{1,2}):(\d{2})'
                    time_match = re.search(time_pattern, content)
                    if time_match:
                        hour, minute = time_match.groups()
                        from app.utils import normalize_time_format
                        normalized = normalize_time_format(f"{hour}:{minute}")
                        if normalized:
                            data["appointment_time"] = normalized
                
                # 2. EXTRAÇÃO BÁSICA DE DATAS - Apenas por regex simples
                # Tentar identificar se é data de nascimento (< 2010) ou consulta (>= 2010)
                if not data["patient_birth_date"] or not data["appointment_date"]:  
                    date_pattern = r'(\d{1,2})/(\d{1,2})/(\d{4})'
                    date_matches = re.findall(date_pattern, content)
                    for match in date_matches:
                        day, month, year = match
                        full_date = f"{day.zfill(2)}/{month.zfill(2)}/{year}"
                        try:
                            # Validar data
                            date_obj = datetime.strptime(full_date, '%d/%m/%Y')
                            y = int(year)
                            
                            if not data["patient_birth_date"] and y < 2010:
                                # Provavelmente data de nascimento
                                data["patient_birth_date"] = full_date
                                logger.info(f"📅 Data nascimento extraída (regex): {full_date}")
                            elif not data["appointment_date"] and y >= 2010:
                                # Provavelmente data de consulta
                                data["appointment_date"] = full_date
                                logger.info(f"📅 Data consulta extraída (regex): {full_date}")
                        except ValueError:
                            pass
                
                # 4. EXTRAÇÃO DE TIPO DE CONSULTA - SEMPRE atualizar quando escolha explícita
                # Se mensagem é só "1", "2" ou "3" (escolha explícita de tipo)
                if content in ["1", "2", "3"]:
                    type_map = {"1": "clinica_geral", "2": "geriatria", "3": "domiciliar"}
                    # Sempre atualizar (sobrescrever) quando usuário escolhe explicitamente
                    data["consultation_type"] = type_map[content]
                    logger.info(f"💾 Tipo de consulta atualizado (escolha explícita): {data['consultation_type']}")
                
                # 5. EXTRAÇÃO DE CONVÊNIO - SEMPRE atualizar quando escolha explícita
                content_lower = content.lower().strip()
                content_stripped = content.strip().lower()
                
                # Log para debug
                logger.info(f"🔍 CONVÊNIO - Mensagem do usuário: '{content}'")
                logger.info(f"🔍 CONVÊNIO - Conteúdo processado: '{content_lower}'")
                
                # NOVA LÓGICA: Detectar respostas ultra-curtas PRIMEIRO
                
                # 1. Detectar respostas negativas ultra-curtas (1-2 caracteres)
                if content_stripped in ["não", "nao", "n", "nope", "nunca"]:
                    data["insurance_plan"] = "Particular"
                    logger.info(f"💳 Convênio: Particular (resposta negativa curta: '{content_stripped}')")
                    
                # 2. Detectar convênios explícitos
                elif "cabergs" in content_lower:
                    data["insurance_plan"] = "CABERGS"
                    logger.info(f"💾 Convênio: CABERGS (menção direta)")
                    
                elif "ipe" in content_lower:
                    data["insurance_plan"] = "IPE"
                    logger.info(f"💾 Convênio: IPE (menção direta)")
                    
                # 3. Compatibilidade numérica
                elif content in ["1", "2"]:
                    insurance_map = {"1": "CABERGS", "2": "IPE"}
                    data["insurance_plan"] = insurance_map[content]
                    logger.info(f"💾 Convênio: {data['insurance_plan']} (escolha numérica)")
                    
                # 4. Detectar frases negativas completas (lista expandida)
                else:
                    negative_insurance = [
                        # Frases completas
                        "não tenho", "nao tenho", "não possuo", "nao possuo",
                        "sem convênio", "sem convenio", "não tenho convênio", "nao tenho convenio",
                        "não possuo convênio", "nao possuo convenio",
                        # Palavras-chave de negação
                        "sem plano", "não uso", "nao uso",
                        # Particular explícito
                        "particular", "prefiro particular", "quero particular", "vou particular"
                    ]
                    
                    if any(phrase in content_lower for phrase in negative_insurance):
                        data["insurance_plan"] = "Particular"
                        logger.info(f"💳 Convênio: Particular (frase negativa detectada)")
                
                # Log do resultado final
                logger.info(f"🔍 CONVÊNIO - Resultado da detecção: '{data.get('insurance_plan', 'Nenhum')}'")
            
            logger.info(f"📋 Extração concluída: {data}")
            return data
        except Exception as e:
            logger.error(f"Erro ao extrair dados do histórico: {e}", exc_info=True)
            return {}

    def _evaluate_name_quality(self, name: str) -> int:
        """Avalia qualidade de um nome (quanto maior, melhor)
        
        Retorna:
            - 0: Nome inválido ou muito fraco
            - 1-10: Pontuação baseada em:
                - Número de palavras (mais palavras = maior pontuação)
                - Tamanho mínimo das palavras
                - Presença de capitalização adequada
        """
        if not name or len(name.strip()) < 8:
            return 0
        
        # Verificar se não é frase comum
        name_lower = name.lower()
        frases_invalidas = ['tudo bem', 'tudo bom', 'ok tudo', 'beleza tudo']
        if any(frase in name_lower for frase in frases_invalidas):
            return 0
        
        palavras = name.split()
        palavras_validas = [p for p in palavras if len(p) > 2 and p.lower() not in ['de', 'da', 'do', 'dos', 'das']]
        
        # Mínimo 2 palavras válidas
        if len(palavras_validas) < 2:
            return 0
        
        # Pontuação baseada em número de palavras válidas
        # 2 palavras = 5 pontos, 3 palavras = 8 pontos, 4+ palavras = 10 pontos
        if len(palavras_validas) >= 4:
            return 10
        elif len(palavras_validas) == 3:
            return 8
        else:
            return 5

    def _extrair_nome_e_data_robusto(self, mensagem: str) -> Dict[str, Any]:
        """
        Extrai nome completo e data de nascimento de forma robusta
        
        Returns:
            {
                "nome": str | None,
                "data": str | None,
                "erro_nome": str | None,
                "erro_data": str | None
            }
        """
        import re
        from datetime import datetime
        
        # Lista de frases curtas que devem ser ignoradas (não são nomes)
        FRASES_IGNORAR = [
            "sim", "não", "nao", "tudo bem", "obrigado", "obrigada",
            "por favor", "claro", "ok", "pode", "confirma", "beleza",
            "perfeito", "certo", "exato", "isso", "show", "obrigado",
            "prazer", "impeça", "adicione", "venha", "vir", "está"
        ]
        
        # Lista de palavras ofensivas a serem ignoradas
        PALAVRAS_OFENSIVAS = [
            "puta", "pinto", "buceta", "caralho", "cacete", "porra", "merda",
            "cu", "foda", "fodas", "foder", "chupa", "viado", "veado",
            "sua mãe", "sua mãe", "filho da puta", "filha da puta"
        ]
        
        # Validar se mensagem não é apenas uma frase de confirmação
        mensagem_lower = mensagem.lower().strip()
        
        # Ignorar mensagens com palavras ofensivas
        if any(palavra in mensagem_lower for palavra in PALAVRAS_OFENSIVAS):
            logger.info(f"🔍 Ignorando mensagem com palavra ofensiva: {mensagem}")
            return {
                "nome": None,
                "data": None,
                "erro_nome": None,
                "erro_data": None
            }
        
        # Detectar especificamente "tudo bem" mesmo em frases maiores
        if "tudo bem" in mensagem_lower or "tudo bom" in mensagem_lower:
            logger.info(f"🔍 Ignorando mensagem com 'tudo bem/bom': {mensagem}")
            return {
                "nome": None,
                "data": None,
                "erro_nome": None,
                "erro_data": None
            }
        
        if any(frase in mensagem_lower for frase in FRASES_IGNORAR):
            if len(mensagem.split()) <= 2:  # Ignorar se tem 2 palavras ou menos
                logger.info(f"🔍 Ignorando mensagem curta de confirmação: {mensagem}")
                return {
                    "nome": None,
                    "data": None,
                    "erro_nome": None,
                    "erro_data": None
                }
        
        # Ignorar mensagens muito curtas (< 8 caracteres)
        if len(mensagem) < 8:
            logger.info(f"🔍 Ignorando mensagem muito curta: {mensagem}")
            return {
                "nome": None,
                "data": None,
                "erro_nome": None,
                "erro_data": None
            }
        
        resultado = {
            "nome": None,
            "data": None,
            "erro_nome": None,
            "erro_data": None
        }
        
        # ========== EXTRAÇÃO DE DATA (REGEX) ==========
        
        # Padrão 1: DD/MM/AAAA ou DD-MM-AAAA
        padrao_numerico = r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b'
        padrao_texto = r'\b(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})\b'
        
        match = re.search(padrao_numerico, mensagem)
        
        if match:
            dia, mes, ano = match.groups()
            dia = dia.zfill(2)
            mes = mes.zfill(2)
            
            # Validar formato
            try:
                data_obj = datetime.strptime(f"{dia}/{mes}/{ano}", '%d/%m/%Y')
                
                # Validar idade máxima (120 anos)
                if (datetime.now() - data_obj).days / 365.25 > 120:
                    resultado["erro_data"] = "Data de nascimento parece incorreta (mais de 120 anos)"
                else:
                    resultado["data"] = f"{dia}/{mes}/{ano}"
                    logger.info(f"✅ DATA VÁLIDA APROVADA: {dia}/{mes}/{ano} (hoje: {datetime.now().strftime('%d/%m/%Y')})")
            except ValueError:
                resultado["erro_data"] = "Data inválida. Use formato DD/MM/AAAA"
        
        # Padrão 1.5: DDMMAAAA (sem separadores) - ex: 07082003
        if not resultado["data"] and not resultado["erro_data"]:
            padrao_sem_separador = r'\b(\d{8})\b'
            match = re.search(padrao_sem_separador, mensagem)
            
            if match:
                data_str = match.group(1)
                try:
                    # Tentar parsear como DDMMAAAA
                    dia = data_str[:2]
                    mes = data_str[2:4]
                    ano = data_str[4:8]
                    
                    data_obj = datetime.strptime(f"{dia}/{mes}/{ano}", '%d/%m/%Y')
                    
                    # Validar idade máxima (120 anos)
                    if (datetime.now() - data_obj).days / 365.25 > 120:
                        resultado["erro_data"] = "Data de nascimento parece incorreta (mais de 120 anos)"
                    else:
                        resultado["data"] = f"{dia}/{mes}/{ano}"
                        logger.info(f"✅ DATA VÁLIDA APROVADA: {dia}/{mes}/{ano} (hoje: {datetime.now().strftime('%d/%m/%Y')})")
                except ValueError:
                    # Se não conseguir parsear, não é uma data válida
                    pass
        
        # Padrão 2: "7 de agosto de 2003" ou "07 de agosto de 2003"
        if not resultado["data"] and not resultado["erro_data"]:
            meses = {
                'janeiro': '01', 'jan': '01',
                'fevereiro': '02', 'fev': '02',
                'março': '03', 'mar': '03', 'marco': '03',
                'abril': '04', 'abr': '04',
                'maio': '05', 'mai': '05',
                'junho': '06', 'jun': '06',
                'julho': '07', 'jul': '07',
                'agosto': '08', 'ago': '08',
                'setembro': '09', 'set': '09',
                'outubro': '10', 'out': '10',
                'novembro': '11', 'nov': '11',
                'dezembro': '12', 'dez': '12'
            }
            
            # Padrão completo: "7 de agosto de 2003"
            match = re.search(padrao_texto, mensagem, re.IGNORECASE)
            
            if match:
                dia, mes_nome, ano = match.groups()
                mes_num = meses.get(mes_nome.lower())
                
                if mes_num:
                    dia = dia.zfill(2)
                    try:
                        data_obj = datetime.strptime(f"{dia}/{mes_num}/{ano}", '%d/%m/%Y')
                        
                        # Validar idade máxima (120 anos)
                        if (datetime.now() - data_obj).days / 365.25 > 120:
                            resultado["erro_data"] = "Data de nascimento parece incorreta (mais de 120 anos)"
                        else:
                            resultado["data"] = f"{dia}/{mes_num}/{ano}"
                            logger.info(f"✅ DATA VÁLIDA APROVADA: {dia}/{mes_num}/{ano} (hoje: {datetime.now().strftime('%d/%m/%Y')})")
                    except ValueError:
                        resultado["erro_data"] = "Data inválida"
            
            # Padrão abreviado: "7 ago 2003" ou "7/ago/2003"
            if not resultado["data"] and not resultado["erro_data"]:
                padrao_abreviado = r'\b(\d{1,2})\s+(ago|set|out|nov|dez|jan|fev|mar|abr|mai|jun|jul)\s+(\d{4})\b'
                match = re.search(padrao_abreviado, mensagem, re.IGNORECASE)
                
                if match:
                    dia, mes_abrev, ano = match.groups()
                    mes_num = meses.get(mes_abrev.lower())
                    
                    if mes_num:
                        dia = dia.zfill(2)
                        try:
                            data_obj = datetime.strptime(f"{dia}/{mes_num}/{ano}", '%d/%m/%Y')
                            
                            # Validar idade máxima (120 anos)
                            if (datetime.now() - data_obj).days / 365.25 > 120:
                                resultado["erro_data"] = "Data de nascimento parece incorreta (mais de 120 anos)"
                            else:
                                resultado["data"] = f"{dia}/{mes_num}/{ano}"
                                logger.info(f"✅ DATA VÁLIDA APROVADA: {dia}/{mes_num}/{ano} (hoje: {datetime.now().strftime('%d/%m/%Y')})")
                        except ValueError:
                            resultado["erro_data"] = "Data inválida"
        
        # ========== EXTRAÇÃO DE NOME ==========
        
        # Remover a data da mensagem para facilitar extração do nome
        mensagem_sem_data = mensagem
        if resultado["data"]:
            mensagem_sem_data = re.sub(padrao_numerico, '', mensagem_sem_data)
            mensagem_sem_data = re.sub(padrao_texto, '', mensagem_sem_data, flags=re.IGNORECASE)
        
        # Remover palavras comuns que não são nome
        palavras_ignorar = [
            'meu', 'nome', 'é', 'sou', 'me', 'chamo', 'chama', 'conhecido', 'como',
            'nasci', 'nascido', 'em', 'dia', 'data', 'nascimento', 'de', 'e', 'a', 'o',
            ',', '.', '!', '?', 'oi', 'olá', 'bom', 'dia', 'tarde', 'noite',
            # Palavras que não podem ser nomes
            'tudo', 'bem', 'tudo bem', 'beleza', 'ok', 'sim', 'não', 'nao',
            # Meses e abreviações
            'janeiro', 'jan', 'fevereiro', 'fev', 'março', 'mar', 'marco',
            'abril', 'abr', 'maio', 'mai', 'junho', 'jun', 'julho', 'jul',
            'agosto', 'ago', 'setembro', 'set', 'outubro', 'out', 'novembro', 'nov', 'dezembro', 'dez'
        ]
        
        # Extrair possível nome
        palavras = mensagem_sem_data.split()
        nome_candidato = []
        
        # Detectar se há apelido na mensagem original
        tem_apelido = any(phrase in mensagem.lower() for phrase in ['me chama', 'conhecido como', 'pode chamar', 'chama de'])
        
        for palavra in palavras:
            palavra_limpa = palavra.strip(',.!?')
            if palavra_limpa and palavra_limpa.lower() not in palavras_ignorar:
                # Verificar se é texto (não número)
                if not palavra_limpa.isdigit():
                    # Se tem apelido na mensagem, parar no primeiro nome completo encontrado
                    if tem_apelido and len(nome_candidato) >= 2:
                        break
                    nome_candidato.append(palavra_limpa)
        
        if nome_candidato:
            nome_completo = ' '.join(nome_candidato)
            
            # Validar nome
            # 1. Apenas letras, espaços, hífens, acentos
            if re.match(r"^[a-zA-ZÀ-ÿ\s\-']+$", nome_completo):
                # 2. Remover preposições e contar palavras
                preposicoes = ['de', 'da', 'do', 'dos', 'das']
                palavras_validas = [p for p in nome_completo.split() if p.lower() not in preposicoes]
                
                # Verificar se não é frase comum como "Tudo Bem"
                nome_lower = nome_completo.lower()
                frases_invalidas = ['tudo bem', 'tudo bom', 'ok tudo', 'beleza tudo']
                if any(frase in nome_lower for frase in frases_invalidas):
                    logger.info(f"🔍 Ignorando frase comum como nome: {nome_completo}")
                    resultado["erro_nome"] = "Frase comum detectada, não é um nome"
                elif len(palavras_validas) >= 2:
                    # Nome válido!
                    resultado["nome"] = nome_completo.title()
                elif len(palavras_validas) == 1:
                    resultado["erro_nome"] = "Para o cadastro médico, preciso do nome completo (nome e sobrenome)"
            else:
                resultado["erro_nome"] = "Nome contém caracteres inválidos"
        
        return resultado

    # ===== Encerramento de contexto =====
    def _should_end_context(self, context: ConversationContext, last_user_message: str) -> bool:
        """Decide se devemos encerrar o contexto.
        Regras:
        - Resposta negativa após pergunta final do bot
        - Qualquer negativa explícita quando não há fluxo ativo
        - Pausado para humano (tratado em main.py)
        """
        try:
            if not context:
                return False
            text = (last_user_message or "").strip().lower()
            # Triggers ESPECÍFICOS para evitar encerramentos prematuros
            negative_triggers = [
                "só isso mesmo",
                "só isso",
                "pode encerrar",
                "pode finalizar",
                "não preciso de mais nada",
                "não preciso mais",
                "obrigado tchau",
                "obrigada tchau",
                "até logo",
                "até mais"
            ]
            is_negative = any(t in text for t in negative_triggers)

            # Verificar se a última mensagem do assistente foi a pergunta final
            last_assistant_asks_more = False
            for msg in reversed(context.messages):
                if msg.get("role") == "assistant":
                    content = (msg.get("content") or "").lower()
                    if "posso te ajudar com mais alguma coisa" in content:
                        last_assistant_asks_more = True
                    break

            # NUNCA encerrar se estamos no meio de um fluxo ativo
            if context.current_flow == "booking":
                logger.info(f"❌ NÃO encerrando - fluxo de agendamento ativo")
                return False
            
            # Encerrar APENAS se:
            # 1. Bot perguntou "posso te ajudar com mais alguma coisa?"
            # 2. E usuário respondeu negativamente
            if is_negative and last_assistant_asks_more:
                logger.info(f"✅ Encerrando - ação completa + usuário não precisa mais")
                return True
            
            return False
        except Exception:
            return False

    def _detect_confirmation_intent(self, message: str) -> str:
        """
        Detecta se a mensagem é uma confirmação positiva ou negativa.
        
        Returns:
            "positive" - usuário confirmou
            "negative" - usuário negou/quer mudar
            "unclear" - não foi possível determinar
        """
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
        
        # Verificar positivos
        for keyword in positive_keywords:
            if keyword in message_lower:
                return "positive"
        
        # Verificar negativos
        for keyword in negative_keywords:
            if keyword in message_lower:
                return "negative"
        
        return "unclear"

    def _parse_iso_datetime(self, value: Optional[str]) -> Optional[datetime]:
        """Converte string ISO em datetime, retornando None em caso de erro."""
        if not value or not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _auto_offer_next_slot(self, context: ConversationContext, db: Session, phone: str, reset_history: bool = False) -> Optional[str]:
        """Busca e prepara a próxima sugestão automática de horário."""

        if not context.flow_data:
            context.flow_data = MutableDict()

        tz = self.timezone
        now_plus_buffer = now_brazil() + timedelta(hours=48)

        # Determinar ponto de partida da busca
        next_search_iso = context.flow_data.get("auto_offer_next_search")
        next_search_dt = self._parse_iso_datetime(next_search_iso)

        if next_search_dt is None:
            start_after = now_plus_buffer
        else:
            start_after = max(next_search_dt.astimezone(tz), now_plus_buffer)

        # Histórico de horários já oferecidos
        if reset_history:
            context.flow_data["auto_offer_history"] = []
            context.flow_data["auto_offer_rejections"] = 0
        history_list = context.flow_data.get("auto_offer_history") or []
        history_set = set(history_list)

        # Validar convênio antes de gerar horários automáticos
        insurance_plan_value = context.flow_data.get("insurance_plan")
        valid_insurance = {"CABERGS", "IPE", "Particular"}

        if insurance_plan_value not in valid_insurance:
            logger.info("⏸️ Auto-oferta bloqueada: convênio ausente ou inválido")
            context.flow_data.pop("auto_offer_pending", None)
            context.flow_data.pop("auto_offer_current", None)
            context.flow_data.pop("auto_offer_next_search", None)
            context.flow_data.pop("auto_offer_history", None)
            context.flow_data.pop("auto_offer_rejections", None)
            db.commit()
            return (
                "Antes de buscar os horários disponíveis, preciso saber se você possui algum convênio.\n"
                "Trabalhamos com CABERGS e IPE. Se não tiver convênio, responda 'Não' para seguirmos com consulta particular."
            )

        # Buscar próximo(s) horário(s)
        search_limit = max(3, len(history_set) + 1)
        candidate_slots = appointment_rules.find_next_available_slots(start_after, db, limit=search_limit)

        selected_slot = None
        for slot in candidate_slots:
            slot_tz = tz.localize(slot)
            slot_iso = slot_tz.isoformat()
            if slot_iso in history_set:
                continue
            selected_slot = slot_tz
            break

        if selected_slot is None:
            # Tentativa extra: buscar mais distante
            candidate_slots = appointment_rules.find_next_available_slots(start_after + timedelta(days=1), db, limit=search_limit + 2)
            for slot in candidate_slots:
                slot_tz = tz.localize(slot)
                slot_iso = slot_tz.isoformat()
                if slot_iso in history_set:
                    continue
                selected_slot = slot_tz
                break

        if selected_slot is None:
            logger.warning("⚠️ Nenhum horário disponível encontrado para sugestão automática")
            return "❌ No momento não encontrei horários disponíveis após as próximas 48 horas. Posso tentar novamente em instantes ou você pode sugerir uma data específica." 

        date_str = selected_slot.strftime('%d/%m/%Y')
        time_str = selected_slot.strftime('%H:%M')
        weekday_names = [
            'segunda-feira', 'terça-feira', 'quarta-feira',
            'quinta-feira', 'sexta-feira', 'sábado', 'domingo'
        ]
        weekday_label = weekday_names[selected_slot.weekday()].capitalize()

        tipos_consulta = self.clinic_info.get('tipos_consulta', {})
        consultation_type = context.flow_data.get("consultation_type")
        tipo_msg = ""
        if consultation_type:
            tipo_info = tipos_consulta.get(consultation_type, {})
            tipo_nome = tipo_info.get('nome', consultation_type)
            tipo_valor = tipo_info.get('valor')
            if tipo_valor is not None:
                tipo_msg = f"🏥 Consulta: {tipo_nome} (R$ {tipo_valor:.2f})\n"
            else:
                tipo_msg = f"🏥 Consulta: {tipo_nome}\n"

        insurance_plan = context.flow_data.get("insurance_plan") or "Particular"

        message = (
            "Encontrei o próximo horário disponível respeitando a carência mínima de 48 horas:\n\n"
            f"📅 {weekday_label} - {date_str}\n"
            f"⏰ {time_str}\n"
            f"💳 Convênio: {insurance_plan}\n"
        )

        if tipo_msg:
            message += tipo_msg

        message += (
            "\nPosso reservar esse horário para você?\n"
            "Se não for possível, é só responder 'não' que eu busco outra opção."
        )

        # Atualizar estado do flow_data
        slot_iso = selected_slot.isoformat()
        history_list.append(slot_iso)
        context.flow_data["auto_offer_history"] = history_list
        context.flow_data["auto_offer_current"] = {
            "date": date_str,
            "time": time_str,
            "weekday": weekday_label,
            "iso": slot_iso
        }
        context.flow_data["appointment_date"] = date_str
        context.flow_data["appointment_time"] = time_str
        context.flow_data["auto_offer_pending"] = True
        context.flow_data.setdefault("auto_offer_rejections", 0)
        context.flow_data["auto_offer_next_search"] = (selected_slot + timedelta(minutes=1)).isoformat()
        context.flow_data.pop("awaiting_manual_date", None)
        context.flow_data.pop("fallback_confirm_time_slot_attempted", None)

        db.commit()

        logger.info(f"📅 Sugestão automática preparada: {date_str} às {time_str} ({weekday_label})")
        return message

    def _clear_auto_offer_state(self, context: ConversationContext, db: Session) -> None:
        """Remove dados temporários do fluxo de sugestão automática."""
        if not context.flow_data:
            return
        for key in [
            "auto_offer_pending",
            "auto_offer_current",
            "auto_offer_next_search",
            "auto_offer_history",
            "auto_offer_rejections"
        ]:
            context.flow_data.pop(key, None)
        db.commit()

    def _handle_manual_date_selection(self, context: ConversationContext, message: str, db: Session, phone: str) -> str:
        """Processa resposta do paciente com data (e horário) customizados."""

        if not context.flow_data:
            context.flow_data = MutableDict()

        import re
        date_match = re.search(r'(\d{2}/\d{2}/\d{4})', message)
        if not date_match:
            return (
                "Para continuar, preciso que você me informe a data desejada no formato DD/MM/AAAA.\n"
                "Exemplo: 25/11/2025. Se quiser sugerir horário, escreva junto (ex: 25/11/2025 às 15:00)."
            )

        date_str = date_match.group(1)
        appointment_date = parse_date_br(date_str)
        if not appointment_date:
            return (
                f"Não consegui entender a data '{date_str}'.\n"
                "Use o formato DD/MM/AAAA (exemplo: 07/08/2025)."
            )

        tz = self.timezone
        now_buffer = now_brazil() + timedelta(hours=48)

        # Capturar horário, se fornecido
        time_str = None
        time_match = re.search(r'(\d{1,2})(?:[:h](\d{2}))', message, re.IGNORECASE)
        if time_match:
            hours = time_match.group(1)
            minutes = time_match.group(2)
            candidate = f"{int(hours):02d}:{int(minutes):02d}"
            from app.utils import normalize_time_format
            normalized = normalize_time_format(candidate)
            if normalized and normalized.endswith(':00'):
                time_str = normalized

        if time_str is None:
            # Procurar padrões simples como "às 15" ou "15h"
            simple_match = re.search(r'(?:às|as|a partir das|depois das|preferencialmente as)\s*(\d{1,2})', message, re.IGNORECASE)
            if simple_match:
                hours = int(simple_match.group(1))
                if 0 <= hours <= 23:
                    time_str = f"{hours:02d}:00"

        # Determinar ponto inicial da busca
        if time_str:
            hour, minute = map(int, time_str.split(':'))
            start_after = tz.localize(datetime.combine(appointment_date.date(), time(hour, minute)))
        else:
            start_after = tz.localize(datetime.combine(appointment_date.date(), time.min))

        if start_after < now_buffer:
            min_date_str = now_buffer.strftime('%d/%m/%Y às %H:%M')
            return (
                "Para cumprir a carência mínima, só consigo agendar com pelo menos 48 horas de antecedência.\n"
                f"A partir de agora, consigo oferecer horários a partir de {min_date_str}.\n"
                "Você pode informar outra data depois desse limite?"
            )

        # Reiniciar estado e preparar próxima busca
        context.flow_data["auto_offer_rejections"] = 0
        context.flow_data["auto_offer_pending"] = False
        context.flow_data["auto_offer_next_search"] = start_after.isoformat()
        context.flow_data["auto_offer_history"] = []
        db.commit()

        suggestion = self._auto_offer_next_slot(context, db, phone, reset_history=True)
        if suggestion:
            return suggestion

        return (
            "Verifiquei e infelizmente não encontrei horários disponíveis para essa data.\n"
            "Quer tentar uma outra data ou prefere que eu procure automaticamente os próximos horários livres?"
        )

    def _can_start_auto_offer(self, context: ConversationContext) -> bool:
        if not context.flow_data:
            return False
        flow_data = context.flow_data
        required_fields = [
            "patient_name",
            "patient_birth_date",
            "consultation_type",
            "insurance_plan"
        ]
        if any(not flow_data.get(field) for field in required_fields):
            return False
        if flow_data.get("appointment_completed"):
            return False
        if flow_data.get("auto_offer_pending"):
            return False
        if flow_data.get("awaiting_manual_date"):
            return False
        if flow_data.get("auto_offer_current"):
            return False

        insurance_plan = flow_data.get("insurance_plan")
        valid_insurance = {"CABERGS", "IPE", "Particular"}
        if insurance_plan not in valid_insurance:
            return False

        return True

    def process_message(self, message: str, phone: str, db: Session) -> str:
        """Processa uma mensagem do usuário e retorna a resposta com contexto persistente"""
        try:
            # 1. Carregar contexto do banco
            context = db.query(ConversationContext).filter_by(phone=phone).first()
            if not context:
                # Primeira mensagem deste usuário, criar contexto novo
                context = ConversationContext(
                    phone=phone,
                    messages=MutableList(),
                    flow_data=MutableDict(),
                    status="active"
                )
                db.add(context)
                logger.info(f"🆕 Novo contexto criado para {phone}")
            else:
                logger.info(f"📱 Contexto carregado para {phone}: {len(context.messages)} mensagens")
            
            # 2. Verificação de timeout removida - agora é proativa via scheduler
            
            # 2.1 Resetar estado quando paciente iniciar novo agendamento
            message_lower = message.lower().strip()
            is_new_booking_request = (
                message_lower in {"1", "1️⃣"}
                or ("marcar" in message_lower and "consulta" in message_lower)
                or "quero marcar" in message_lower
            )

            if context.flow_data and is_new_booking_request:
                logger.info("🧹 Novo agendamento detectado - limpando dados sensíveis do flow_data")

                keys_to_reset = [
                    "appointment_completed",
                    "consultation_type",
                    "insurance_plan",
                    "appointment_date",
                    "appointment_time",
                    "pending_confirmation",
                    "awaiting_manual_date"
                ]

                for key in keys_to_reset:
                    if key in context.flow_data:
                        context.flow_data.pop(key, None)

                # Limpar estado de auto-ofertas (também executa commit)
                self._clear_auto_offer_state(context, db)

                # Garantir que alterações sejam persistidas caso _clear_auto_offer_state não execute commit
                db.commit()

            # 3. Decidir se deve encerrar contexto por resposta negativa
            if self._should_end_context(context, message):
                logger.info(f"🔚 Encerrando contexto para {phone} por resposta negativa do usuário")
                db.delete(context)
                db.commit()
                return "Foi um prazer atender você! Até logo! 😊"

            # 4.1 Verificar se há uma sugestão automática pendente
            if context.flow_data and context.flow_data.get("auto_offer_pending"):
                intent = self._detect_confirmation_intent(message)

                context.messages.append({
                    "role": "user",
                    "content": message,
                    "timestamp": datetime.utcnow().isoformat()
                })
                flag_modified(context, 'messages')

                current_slot = (context.flow_data or {}).get("auto_offer_current") or {}
                date_str = current_slot.get("date")
                time_str = current_slot.get("time")

                if intent == "positive" and date_str and time_str:
                    logger.info(f"✅ Usuário {phone} aceitou o horário sugerido automaticamente")

                    data = context.flow_data or {}
                    if not data.get("patient_name") or not data.get("patient_birth_date"):
                        extracted = self._extract_appointment_data_from_messages(context.messages)
                        data["patient_name"] = data.get("patient_name") or extracted.get("patient_name")
                        if not data.get("patient_birth_date"):
                            data["patient_birth_date"] = extracted.get("patient_birth_date")

                    # Desativar estado de auto-oferta antes de criar o agendamento
                    context.flow_data["auto_offer_pending"] = False
                    context.flow_data.pop("auto_offer_current", None)
                    context.flow_data.pop("auto_offer_next_search", None)
                    context.flow_data.pop("fallback_confirm_time_slot_attempted", None)
                    db.commit()

                    payload = {
                        "patient_name": data.get("patient_name"),
                        "patient_birth_date": data.get("patient_birth_date"),
                        "appointment_date": date_str,
                        "appointment_time": time_str,
                        "patient_phone": phone,
                        "consultation_type": data.get("consultation_type"),
                        "insurance_plan": data.get("insurance_plan")
                    }

                    result = self._handle_create_appointment(payload, db, phone)
                    self._clear_auto_offer_state(context, db)

                    context.messages.append({
                        "role": "assistant",
                        "content": result,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    flag_modified(context, 'messages')
                    context.last_activity = datetime.utcnow()
                    db.commit()
                    return result

                if intent == "negative":
                    logger.info(f"❌ Usuário {phone} rejeitou o horário sugerido automaticamente")
                    rejections = context.flow_data.get("auto_offer_rejections", 0) + 1
                    context.flow_data["auto_offer_rejections"] = rejections
                    context.flow_data["auto_offer_pending"] = False

                    if rejections >= 3:
                        context.flow_data["awaiting_manual_date"] = True
                        context.flow_data.pop("appointment_date", None)
                        context.flow_data.pop("appointment_time", None)
                        context.flow_data.pop("auto_offer_current", None)
                        context.flow_data["auto_offer_history"] = []
                        db.commit()
                        response = (
                            "Sem problemas! 😊\n"
                            "Você pode me informar uma data que fique boa para você (DD/MM/AAAA)?\n"
                            "Se tiver um horário preferido, escreva junto (ex: 25/11/2025 às 15:00)."
                        )
                        context.messages.append({
                            "role": "assistant",
                            "content": response,
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        flag_modified(context, 'messages')
                        context.last_activity = datetime.utcnow()
                        db.commit()
                        return response

                    # Buscar próxima opção automaticamente
                    next_option = self._auto_offer_next_slot(context, db, phone)
                    if not next_option:
                        next_option = (
                            "Ainda não encontrei outro horário após as próximas 48 horas.\n"
                            "Quer me informar uma data específica para eu tentar marcar?"
                        )

                    context.messages.append({
                        "role": "assistant",
                        "content": next_option,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    flag_modified(context, 'messages')
                    context.last_activity = datetime.utcnow()
                    db.commit()
                    return next_option

                # Intenção não clara
                clarification = (
                    "Só para confirmar: esse horário funciona para você?\n"
                    "Responda 'sim' para confirmar ou 'não' para eu buscar outra opção."
                )
                context.messages.append({
                    "role": "assistant",
                    "content": clarification,
                    "timestamp": datetime.utcnow().isoformat()
                })
                flag_modified(context, 'messages')
                context.last_activity = datetime.utcnow()
                db.commit()
                return clarification

            # 4.2 Verificar se estamos aguardando data manual após múltiplas recusas
            if context.flow_data and context.flow_data.get("awaiting_manual_date"):
                context.messages.append({
                    "role": "user",
                    "content": message,
                    "timestamp": datetime.utcnow().isoformat()
                })
                flag_modified(context, 'messages')

                response = self._handle_manual_date_selection(context, message, db, phone)
                context.messages.append({
                    "role": "assistant",
                    "content": response,
                    "timestamp": datetime.utcnow().isoformat()
                })
                flag_modified(context, 'messages')
                context.last_activity = datetime.utcnow()
                db.commit()
                return response

            # 4.3 Verificar se há confirmação pendente ANTES de processar com Claude
            if context.flow_data and context.flow_data.get("pending_confirmation"):
                intent = self._detect_confirmation_intent(message)
                
                if intent == "positive":
                    # Usuário confirmou! Executar agendamento
                    logger.info(f"✅ Usuário {phone} confirmou agendamento")
                    
                    # Usar dados do flow_data como fonte primária
                    data = context.flow_data or {}
                    
                    # Apenas extrair do histórico se flow_data estiver completamente vazio
                    if not data.get("patient_name") or not data.get("patient_birth_date"):
                        logger.warning(f"⚠️ Dados ausentes no flow_data, extraindo do histórico")
                        logger.warning(f"   flow_data atual: {data}")
                        extracted = self._extract_appointment_data_from_messages(context.messages)
                        data["patient_name"] = data.get("patient_name") or extracted.get("patient_name")
                        if not data.get("patient_birth_date"):
                            data["patient_birth_date"] = extracted.get("patient_birth_date")
                        logger.info(f"   Dados após extração: {data}")
                    else:
                        logger.info(f"✅ Usando dados do flow_data: {data}")
                    
                    # Criar agendamento
                    result = self._handle_create_appointment({
                        "patient_name": data.get("patient_name"),
                        "patient_birth_date": data.get("patient_birth_date"),
                        "appointment_date": data.get("appointment_date"),
                        "appointment_time": data.get("appointment_time"),
                        "patient_phone": phone
                    }, db, phone)
                    
                    # Limpar pending_confirmation
                    if not context.flow_data:
                        context.flow_data = MutableDict()
                    context.flow_data["pending_confirmation"] = False
                    context.messages.append({
                        "role": "user",
                        "content": message,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    context.messages.append({
                        "role": "assistant",
                        "content": result,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    context.last_activity = datetime.utcnow()
                    db.commit()
                    
                    return result
                
                elif intent == "negative":
                    # Usuário NÃO confirmou, quer mudar
                    logger.info(f"❌ Usuário {phone} não confirmou, pedindo alteração")
                    
                    # Limpar pending_confirmation
                    if not context.flow_data:
                        context.flow_data = MutableDict()
                    context.flow_data["pending_confirmation"] = False
                    db.commit()
                    
                    # Perguntar o que mudar
                    response = "Sem problemas! O que você gostaria de mudar?\n\n" \
                               "1️⃣ Data\n" \
                               "2️⃣ Horário\n" \
                               "3️⃣ Ambos"
                    
                    context.messages.append({
                        "role": "user",
                        "content": message,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    context.messages.append({
                        "role": "assistant",
                        "content": response,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                    context.last_activity = datetime.utcnow()
                    db.commit()
                    
                    return response
                
                # Se unclear, processar normalmente com Claude
                logger.info(f"⚠️ Intenção não clara, processando com Claude")

            # 5. Adicionar mensagem do usuário ao histórico
            context.messages.append({
                "role": "user",
                "content": message,
                "timestamp": datetime.utcnow().isoformat()
            })
            flag_modified(context, 'messages')

            # 6. Preparar mensagens para Claude (histórico completo)
            claude_messages = []
            for msg in context.messages:
                claude_messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })
            
            # 6. Fazer chamada para o Claude com histórico completo
            logger.info(f"🤖 Enviando {len(claude_messages)} mensagens para Claude")
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
                temperature=0.1,
                system=self.system_prompt,
                messages=claude_messages,  # ✅ HISTÓRICO COMPLETO!
                tools=self.tools
            )
            
            # 7. Processar resposta do Claude
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
                                # Usar diretamente o resultado da tool como resposta
                                bot_response = tool_result
                                logger.info("📤 Usando tool_result como resposta (Claude retornou vazio)")
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
                            
                            # CRÍTICO: Se end_conversation foi executado, retornar imediatamente
                            # sem continuar processamento para evitar fallback executar
                            if content.name == "end_conversation":
                                logger.info("🔚 end_conversation executado - retornando imediatamente sem continuar processamento")
                                return tool_result
                            
                            logger.info(f"🔧 Iteration {iteration}: Tool {content.name} result: {tool_result[:200] if len(tool_result) > 200 else tool_result}")
                            
                            # Fazer follow-up com o resultado
                            current_response = self.client.messages.create(
                                model="claude-sonnet-4-20250514",
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
            
            # 7.5. Persistir dados incrementalmente no flow_data
            # Após cada resposta do Claude, verificar se coletou nome ou data nascimento
            # e salvar no flow_data imediatamente (não sobrescrever dados existentes)
            if not context.flow_data:
                context.flow_data = MutableDict()
            
            # Extrair dados do histórico
            extracted = self._extract_appointment_data_from_messages(context.messages)
            
            # NÃO extrair nome aqui - deixar Claude fazer via tool extract_patient_data
            # Extração manual de nome foi removida pois causava erros (ex: "Eu Preciso Marcar Uma Consulta")
            # Se precisar do nome, Claude deve chamar tool extract_patient_data
            
            # Verificar se está aguardando correção de data de nascimento
            if context.flow_data.get("awaiting_birth_date_correction"):
                # Tentar extrair nova data de nascimento
                if extracted.get("patient_birth_date"):
                    context.flow_data["patient_birth_date"] = extracted["patient_birth_date"]
                    context.flow_data["awaiting_birth_date_correction"] = False
                    db.commit()
                    logger.info("🔄 Data de nascimento corrigida, tentando agendar novamente")
            elif extracted.get("patient_birth_date") and not context.flow_data.get("patient_birth_date"):
                context.flow_data["patient_birth_date"] = extracted["patient_birth_date"]
                logger.info(f"💾 Data nascimento salva no flow_data: {extracted['patient_birth_date']}")
            
            # Prevenir re-extração de appointment_date/appointment_time se agendamento já foi completado
            appointment_completed = context.flow_data.get("appointment_completed", False)
            
            if extracted.get("appointment_date") and not context.flow_data.get("appointment_date") and not appointment_completed:
                context.flow_data["appointment_date"] = extracted["appointment_date"]
                logger.info(f"💾 Data consulta salva no flow_data: {extracted['appointment_date']}")
            elif appointment_completed and extracted.get("appointment_date"):
                logger.info(f"⏭️ Pulando salvamento de appointment_date - agendamento já foi completado")
            
            if extracted.get("appointment_time") and not context.flow_data.get("appointment_time") and not appointment_completed:
                # Validar horário antes de salvar usando função robusta
                time_str = extracted["appointment_time"]
                from app.utils import validate_time_format
                if validate_time_format(time_str):
                    context.flow_data["appointment_time"] = time_str
                    logger.info(f"💾 Horário consulta salvo no flow_data: {time_str}")
                else:
                    logger.warning(f"⚠️ Horário inválido rejeitado: {time_str}")
            elif appointment_completed and extracted.get("appointment_time"):
                logger.info(f"⏭️ Pulando salvamento de appointment_time - agendamento já foi completado")
            
            # SEMPRE atualizar tipo de consulta quando extraído (permite correção)
            if extracted.get("consultation_type"):
                tipo_anterior = context.flow_data.get("consultation_type")
                context.flow_data["consultation_type"] = extracted["consultation_type"]
                if tipo_anterior:
                    logger.info(f"💾 Tipo consulta ATUALIZADO no flow_data: {tipo_anterior} → {extracted['consultation_type']}")
                else:
                    logger.info(f"💾 Tipo consulta salvo no flow_data: {extracted['consultation_type']}")
            
            # SEMPRE atualizar convênio quando extraído (permite correção)
            if extracted.get("insurance_plan"):
                convenio_anterior = context.flow_data.get("insurance_plan")
                context.flow_data["insurance_plan"] = extracted["insurance_plan"]
                if convenio_anterior:
                    logger.info(f"💾 Convênio ATUALIZADO no flow_data: {convenio_anterior} → {extracted['insurance_plan']}")
                else:
                    logger.info(f"💾 Convênio salvo no flow_data: {extracted['insurance_plan']}")
            
            # 8. Iniciar sugestão automática quando dados estiverem completos
            if self._can_start_auto_offer(context):
                logger.info(f"🤖 Preparando sugestão automática de horário para {phone}")
                suggestion = self._auto_offer_next_slot(context, db, phone, reset_history=True)
                if suggestion:
                    if context.messages and context.messages[-1].get("role") == "assistant":
                        context.messages[-1]["content"] = suggestion
                        context.messages[-1]["timestamp"] = datetime.utcnow().isoformat()
                    else:
                        context.messages.append({
                            "role": "assistant",
                            "content": suggestion,
                            "timestamp": datetime.utcnow().isoformat()
                        })
                    flag_modified(context, 'messages')
                    context.last_activity = datetime.utcnow()
                    db.commit()
                    return suggestion

            # 9. Atualizar contexto no banco
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
            elif tool_name == "confirm_time_slot":
                return self._handle_confirm_time_slot(tool_input, db, phone)
            elif tool_name == "create_appointment":
                return self._handle_create_appointment(tool_input, db, phone)
            elif tool_name == "search_appointments":
                return self._handle_search_appointments(tool_input, db)
            elif tool_name == "cancel_appointment":
                return self._handle_cancel_appointment(tool_input, db)
            elif tool_name == "request_human_assistance":
                return self._handle_request_human_assistance(tool_input, db, phone)
            elif tool_name == "extract_patient_data":
                return self._handle_extract_patient_data(tool_input, db, phone)
            elif tool_name == "end_conversation":
                return self._handle_end_conversation(tool_input, db, phone)
            
            # Tool não reconhecida
            logger.warning(f"❌ Tool não reconhecida: {tool_name}")
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
            
            # Verificar horários de funcionamento
            horarios = self.clinic_info.get('horario_funcionamento', {})
            horario_dia = horarios.get(weekday_pt, "FECHADO")
            
            if horario_dia == "FECHADO":
                return False, f"❌ A clínica não funciona aos {weekday_pt}s. Horários de funcionamento:\n" + \
                       self._format_business_hours()
            
            # Verificar se horário atual está dentro do funcionamento
            try:
                hora_atual = now_br.time()
                hora_inicio, hora_fim = horario_dia.split('-')
                hora_inicio = datetime.strptime(hora_inicio, '%H:%M').time()
                hora_fim = datetime.strptime(hora_fim, '%H:%M').time()
                
                if hora_inicio <= hora_atual <= hora_fim:
                    return True, f"✅ A clínica está aberta! Funcionamos das {hora_inicio.strftime('%H:%M')} às {hora_fim.strftime('%H:%M')} aos {weekday_pt}s."
                else:
                    return False, f"❌ A clínica está fechada no momento. Funcionamos das {hora_inicio.strftime('%H:%M')} às {hora_fim.strftime('%H:%M')} aos {weekday_pt}s."
                            
            except ValueError:
                return False, "Erro ao verificar horário de funcionamento."
            
        except Exception as e:
            logger.error(f"Erro ao verificar se clínica está aberta: {str(e)}")
            return False, f"Erro ao verificar horário: {str(e)}"
    def _handle_confirm_time_slot(self, tool_input: Dict, db: Session, phone: str = None) -> str:
        """Validar e confirmar horário escolhido"""
        try:
            import re
            from app.utils import normalize_time_format
            
            date_str = tool_input.get("date")
            time_str = tool_input.get("time")
            
            # Normalizar formato de horário
            time_str_original = time_str
            time_str = normalize_time_format(time_str)
            
            if not time_str:
                # Limpar appointment_time do flow_data se existir
                if phone:
                    context = db.query(ConversationContext).filter_by(phone=phone).first()
                    if context and context.flow_data and context.flow_data.get("appointment_time"):
                        context.flow_data["appointment_time"] = None
                        db.commit()
                        logger.info(f"🧹 Horário inválido removido do flow_data (formato incorreto)")
                return f"❌ Formato de horário inválido: '{time_str_original}'. Use um horário válido (exemplo: 14:00, 14, ou 8:00)"
            
            # Validar se é hora inteira
            hour, minute = time_str.split(':')
            if minute != '00':
                # Limpar appointment_time do flow_data se existir
                if phone:
                    context = db.query(ConversationContext).filter_by(phone=phone).first()
                    if context and context.flow_data and context.flow_data.get("appointment_time"):
                        context.flow_data["appointment_time"] = None
                        db.commit()
                        logger.info(f"🧹 Horário inválido removido do flow_data (não inteiro)")
                
                # Buscar todos os horários disponíveis para aquela data
                appointment_date = parse_date_br(date_str)
                if not appointment_date:
                    return "❌ Data inválida. Use formato DD/MM/AAAA."
                
                # Validar dia da semana e obter horários disponíveis
                weekday = appointment_date.weekday()
                dias_semana_pt = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
                dia_nome = dias_semana_pt[weekday]
                
                horarios_disponiveis = self.clinic_info.get('horarios_disponiveis', {})
                horarios_do_dia = horarios_disponiveis.get(dia_nome, [])
                
                if not horarios_do_dia:
                    return f"❌ A clínica não atende em {dia_nome.capitalize()}. Por favor, escolha outra data."
                
                # Buscar consultas já agendadas nesse dia
                date_str_formatted = appointment_date.strftime('%Y%m%d')  # YYYYMMDD
                existing_appointments = db.query(Appointment).filter(
                    Appointment.appointment_date == date_str_formatted,
                    Appointment.status == AppointmentStatus.AGENDADA
                ).all()
                
                # Gerar slots disponíveis baseados na lista de horários fixos
                available_slots = []
                for horario_str in horarios_do_dia:
                    hora, minuto = map(int, horario_str.split(':'))
                    current_time = time(hora, minuto)
                    
                    # Verificar se tem consulta nesse horário
                    slot_datetime = datetime.combine(appointment_date.date(), current_time)
                    tem_conflito = False
                    
                    for apt in existing_appointments:
                        # Converter appointment_time para time object (pode ser string ou time)
                        if isinstance(apt.appointment_time, str):
                            apt_time = datetime.strptime(apt.appointment_time, '%H:%M').time()
                        else:
                            apt_time = apt.appointment_time
                        
                        apt_datetime = datetime.combine(appointment_date.date(), apt_time)
                        
                        # Verificar se há sobreposição - se o horário é exatamente o mesmo
                        if slot_datetime == apt_datetime:
                            tem_conflito = True
                            break
                    
                    if not tem_conflito:
                        available_slots.append(horario_str)
                
                # Montar mensagem com todos os horários disponíveis
                if available_slots:
                    msg = "❌ Por favor, escolha um horário inteiro (exemplo: 14:00, 15:00).\n\n"
                    msg += "Esses são os únicos horários disponíveis para esta data:\n"
                    for slot in available_slots:
                        msg += f"• {slot}\n"
                    return msg
                else:
                    return "❌ Por favor, escolha um horário inteiro (exemplo: 14:00, 15:00).\n\nNão há horários disponíveis para esta data."
            
            # Verificar disponibilidade no banco (segurança contra race condition)
            appointment_date = parse_date_br(date_str)
            appointment_datetime = datetime.combine(appointment_date.date(), 
                                                    datetime.strptime(time_str, '%H:%M').time())
            
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
            is_available = appointment_rules.check_slot_availability(appointment_datetime, duracao, db)
            
            if not is_available:
                return (f"❌ Desculpe, o horário {time_str} foi agendado por outra pessoa há pouco.\n"
                        f"Por favor, escolha outro horário disponível.")
            
            # Salvar no flow_data para confirmação
            context = None
            if phone:
                context = db.query(ConversationContext).filter_by(phone=phone).first()
                if context:
                    if not context.flow_data:
                        context.flow_data = MutableDict()
                    context.flow_data["appointment_date"] = date_str
                    context.flow_data["appointment_time"] = time_str
                    context.flow_data["pending_confirmation"] = True
                    db.commit()
            
            # Buscar dados do paciente - priorizar flow_data, mas usar histórico como fallback
            nome = ""
            nascimento = ""
            tipo = "clinica_geral"
            convenio = "particular"
            
            if context and context.flow_data:
                nome = context.flow_data.get("patient_name", "")
                nascimento = context.flow_data.get("patient_birth_date", "")
                tipo = context.flow_data.get("consultation_type", "clinica_geral")
                convenio = context.flow_data.get("insurance_plan", "particular")
            
            # Se flow_data está incompleto, extrair dados básicos do histórico (mas não nome)
            # Para nome, preferir que Claude use tool extract_patient_data, mas aqui fazemos fallback básico
            if (not nome or tipo == "clinica_geral" or not convenio or convenio == "particular") and context and context.messages:
                logger.info(f"🔍 flow_data incompleto, buscando dados básicos no histórico...")
                extracted = self._extract_appointment_data_from_messages(context.messages)
                
                # Atualizar tipo se não tem ou é padrão
                if tipo == "clinica_geral" and extracted.get("consultation_type"):
                    tipo = extracted["consultation_type"]
                    logger.info(f"✅ Tipo encontrado no histórico: {tipo}")
                
                # Atualizar convênio se não tem ou é padrão
                if (not convenio or convenio == "particular") and extracted.get("insurance_plan"):
                    convenio = extracted["insurance_plan"]
                    logger.info(f"✅ Convênio encontrado no histórico: {convenio}")
                
                # Se nome estiver faltando ou parecer inválido (frases como "Eu Preciso Marcar Uma Consulta"),
                # tentar extrair usando Claude diretamente
                if not nome or any(phrase in nome.lower() for phrase in ["preciso", "quero", "marcar", "consulta", "agendamento", "tudo bem"]):
                    logger.warning(f"⚠️ Nome suspeito/inválido detectado: '{nome}'. Tentando extrair com Claude...")
                    try:
                        # Chamar função auxiliar para extrair dados diretamente
                        extracted_data = self._extract_patient_data_with_claude(context)
                        if extracted_data and extracted_data.get("patient_name"):
                            novo_nome = extracted_data["patient_name"]
                            if novo_nome and novo_nome != nome:
                                nome = novo_nome
                                # Atualizar também no flow_data
                                context.flow_data["patient_name"] = novo_nome
                                db.commit()
                                logger.info(f"✅ Nome corrigido pelo Claude: {nome}")
                    except Exception as e:
                        logger.error(f"Erro ao tentar extrair nome com Claude: {e}")
            
            # Retornar resumo para confirmação
            msg = f"✅ Horário {time_str} disponível!\n\n"
            msg += "📋 Resumo da consulta:\n"
            if nome:
                msg += f"👤 Nome: {nome}\n"
            msg += f"📅 Data: {date_str}\n"
            msg += f"⏰ Horário: {time_str}\n"
            if tipo:
                tipo_map = {
                    "clinica_geral": "Clínica Geral",
                    "geriatria": "Geriatria Clínica e Preventiva",
                    "domiciliar": "Atendimento Domiciliar"
                }
                msg += f"🏥 Tipo: {tipo_map.get(tipo, tipo)}\n"
            if convenio:
                msg += f"💳 Convênio: {convenio}\n"
            
            msg += "\nPosso confirmar o agendamento?"
            return msg
            
        except Exception as e:
            logger.error(f"Erro ao confirmar horário: {str(e)}")
            return f"Erro ao validar horário: {str(e)}"

    def _handle_create_appointment(self, tool_input: Dict, db: Session, phone: str = None) -> str:
        """Tool: create_appointment"""
        try:
            patient_name = tool_input.get("patient_name")
            patient_phone = tool_input.get("patient_phone") or phone  # Usar phone do contexto se não fornecido
            patient_birth_date = tool_input.get("patient_birth_date")
            appointment_date = tool_input.get("appointment_date")
            appointment_time = tool_input.get("appointment_time")
            notes = tool_input.get("notes", "")
            consultation_type = tool_input.get("consultation_type", "clinica_geral")
            insurance_plan = tool_input.get("insurance_plan", "particular")
            
            # Buscar dados do contexto se não fornecidos na tool
            # CRÍTICO: Priorizar tool_input (dados do Claude) sobre flow_data (fallback)
            if phone:
                context = db.query(ConversationContext).filter_by(phone=phone).first()
                if context and context.flow_data:
                    # Usar dados do contexto apenas como fallback se tool_input não tiver
                    if not patient_phone:
                        patient_phone = context.flow_data.get("patient_phone") or phone
                    
                    # Usar flow_data APENAS se tool_input não forneceu o dado
                    if not consultation_type or consultation_type == "clinica_geral":  # valor padrão
                        if context.flow_data.get("consultation_type"):
                            consultation_type = context.flow_data.get("consultation_type")
                            logger.info(f"📋 Usando consultation_type do flow_data (fallback): {consultation_type}")
                    
                    if not insurance_plan or insurance_plan == "particular":  # valor padrão
                        if context.flow_data.get("insurance_plan"):
                            insurance_plan = context.flow_data.get("insurance_plan")
                            logger.info(f"📋 Usando insurance_plan do flow_data (fallback): {insurance_plan}")
            
            # Validar tipo de consulta
            valid_types = ["clinica_geral", "geriatria", "domiciliar"]
            if consultation_type not in valid_types:
                consultation_type = "clinica_geral"  # Fallback
            
            # NOVA VALIDAÇÃO: Garantir que insurance_plan é válido (Camada 3)
            valid_insurance = ["CABERGS", "IPE", "Particular", "particular"]
            
            if insurance_plan not in valid_insurance:
                logger.warning(f"⚠️ Convênio inválido detectado: '{insurance_plan}' - Assumindo Particular")
                insurance_plan = "Particular"
            
            # Normalizar "particular" → "Particular"
            if insurance_plan == "particular":
                insurance_plan = "Particular"
            
            logger.info(f"✅ Convênio validado: {insurance_plan}")
            
            # Log detalhado antes da validação
            logger.info(f"🔍 Validando dados para criar agendamento:")
            logger.info(f"   patient_name: {patient_name}")
            logger.info(f"   patient_phone: {patient_phone}")
            logger.info(f"   patient_birth_date: {patient_birth_date}")
            logger.info(f"   appointment_date: {appointment_date}")
            logger.info(f"   appointment_time: {appointment_time}")
            logger.info(f"   consultation_type: {consultation_type}")
            logger.info(f"   insurance_plan: {insurance_plan}")
            
            if not all([patient_name, patient_phone, patient_birth_date, appointment_date, appointment_time]):
                logger.error(f"❌ VALIDAÇÃO FALHOU - Dados incompletos")
                return "Todos os campos obrigatórios devem ser preenchidos."
            
            # Normalizar telefone
            normalized_phone = normalize_phone(patient_phone)
            
            # Converter datas COM VALIDAÇÃO
            birth_date = parse_date_br(patient_birth_date)
            appointment_datetime = parse_date_br(appointment_date)
            
            if not birth_date:
                logger.error(f"❌ Data de nascimento inválida: {patient_birth_date}")
                # Marcar que está aguardando correção
                if phone:
                    context = db.query(ConversationContext).filter_by(phone=phone).first()
                    if context:
                        if not context.flow_data:
                            context.flow_data = MutableDict()
                        context.flow_data["awaiting_birth_date_correction"] = True
                        db.commit()
                # NÃO limpar flow_data para permitir correção
                return (f"❌ A data de nascimento '{patient_birth_date}' está em formato inválido.\n"
                       f"Por favor, informe sua data de nascimento correta no formato DD/MM/AAAA (exemplo: 07/08/2003)")
            
            if not appointment_datetime:
                logger.error(f"❌ Data de consulta inválida: {appointment_date}")
                # NÃO limpar flow_data para permitir correção
                return (f"❌ A data da consulta '{appointment_date}' está em formato inválido.\n"
                       f"Por favor, informe a data correta no formato DD/MM/AAAA")
            
            # Combinar data e horário (com arredondamento para múltiplo de 5 min)
            try:
                time_obj_original = datetime.strptime(appointment_time, '%H:%M').time()
                temp_dt = datetime.combine(appointment_datetime.date(), time_obj_original).replace(tzinfo=None)
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
            # IMPORTANTE: Remover timezone para compatibilidade com check_slot_availability
            appointment_datetime_naive = appointment_datetime_local.replace(tzinfo=None)
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
            is_available = appointment_rules.check_slot_availability(appointment_datetime_naive, duracao, db)
            
            if not is_available:
                return f"❌ Horário {appointment_time} não está disponível. Vou procurar outro horário para você em instantes."
            
            # Criar agendamento - SALVAR COMO STRING YYYYMMDD para evitar problemas de timezone
            appointment_datetime_formatted = str(appointment_datetime.strftime('%Y%m%d'))  # "20251022" - GARANTIR STRING
            
            appointment = Appointment(
                patient_name=patient_name,
                patient_phone=normalized_phone,
                patient_birth_date=patient_birth_date,  # Manter como string
                appointment_date=appointment_datetime_formatted,  # "20251022" - STRING EXPLÍCITA
                appointment_time=appointment_time,  # Salvar como string HH:MM
                duration_minutes=duracao,
                consultation_type=consultation_type,
                insurance_plan=insurance_plan,
                status=AppointmentStatus.AGENDADA,
                notes=notes
            )
            
            db.add(appointment)
            db.commit()
            logger.info(f"✅ AGENDAMENTO SALVO NO BANCO - ID: {appointment.id}")
            
            # Limpar appointment_date, appointment_time e pending_confirmation do flow_data
            # para evitar loop infinito do fallback
            if phone:
                context = db.query(ConversationContext).filter_by(phone=phone).first()
                if context and context.flow_data:
                    context.flow_data.pop("appointment_date", None)
                    context.flow_data.pop("appointment_time", None)
                    context.flow_data.pop("pending_confirmation", None)
                    # Adicionar flag para indicar que agendamento foi completado
                    context.flow_data["appointment_completed"] = True
                    # Limpar estado de ofertas automáticas
                    self._clear_auto_offer_state(context, db)
                    db.commit()
                    logger.info("🧹 Limpeza do flow_data: appointment_date, appointment_time e pending_confirmation removidos")
                    logger.info("✅ Flag appointment_completed adicionada ao flow_data")
            
            # Buscar informações do tipo de consulta e convênio
            tipos_consulta = self.clinic_info.get('tipos_consulta', {})
            tipo_info = tipos_consulta.get(consultation_type, {})
            tipo_nome = tipo_info.get('nome', 'Clínica Geral')
            tipo_valor = tipo_info.get('valor', 300)
            
            convenios_aceitos = self.clinic_info.get('convenios_aceitos', {})
            convenio_info = convenios_aceitos.get(insurance_plan, {})
            convenio_nome = convenio_info.get('nome', 'Particular')
            
            return f"✅ *Agendamento realizado com sucesso!*\n\n" + \
                   "Obrigado por confiar em nossa clínica! 😊\n\n" + \
                   "📋 *Informações importantes:*\n" + \
                   "• Por favor, traga seus últimos exames\n" + \
                   "• Traga a lista de medicações que você usa\n\n" + \
                   "Vamos enviar uma notificação por WhatsApp no dia da sua consulta.\n\n" + \
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
                
                # Formatar appointment_date (string YYYYMMDD) e appointment_time (string HH:MM)
                app_date_formatted = f"{apt.appointment_date[6:8]}/{apt.appointment_date[4:6]}/{apt.appointment_date[:4]}"
                app_time_str = apt.appointment_time if isinstance(apt.appointment_time, str) else apt.appointment_time.strftime('%H:%M')
                
                response += f"   📅 {app_date_formatted} às {app_time_str}\n"
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
            
            # Formatar appointment_date (string YYYYMMDD) e appointment_time (string HH:MM)
            app_date_formatted = f"{appointment.appointment_date[6:8]}/{appointment.appointment_date[4:6]}/{appointment.appointment_date[:4]}"
            app_time_str = appointment.appointment_time if isinstance(appointment.appointment_time, str) else appointment.appointment_time.strftime('%H:%M')
            
            return f"✅ **Agendamento cancelado com sucesso!**\n\n" + \
                   f"👤 **Paciente:** {appointment.patient_name}\n" + \
                   f"📅 **Data:** {app_date_formatted} às {app_time_str}\n" + \
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

    def _extract_patient_data_with_claude(self, context: ConversationContext, return_dict: bool = False) -> Dict[str, Any]:
        """Usa Claude para extrair dados do paciente do histórico (função auxiliar interna)"""
        try:
            if not context or not context.messages:
                return {}
            
            # Preparar mensagens para Claude (apenas mensagens do usuário relevantes)
            user_messages = []
            for msg in context.messages:
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    # Ignorar mensagens muito curtas ou apenas números
                    if len(content.strip()) > 3 and content.strip() not in ["1", "2", "3", "sim", "não", "nao"]:
                        user_messages.append(content)
            
            if not user_messages:
                return {}
            
            # Criar prompt para Claude extrair dados
            messages_text = "\n".join([f"Mensagem {i+1}: {msg}" for i, msg in enumerate(user_messages)])
            
            extraction_prompt = f"""Analise as seguintes mensagens do usuário e extraia APENAS dados reais de paciente. IGNORE frases de pedido de agendamento.

Mensagens do usuário:
{messages_text}

Extraia e retorne APENAS se encontrar:
1. Nome completo REAL do paciente (não frases como "Eu Preciso Marcar Uma Consulta", "Quero Agendamento", etc)
2. Data de nascimento (formato DD/MM/AAAA)
3. Data da consulta desejada (formato DD/MM/AAAA, apenas se mencionada)
4. Horário da consulta (formato HH:MM, apenas se mencionado)
5. Tipo de consulta (clinica_geral, geriatria, domiciliar)
6. Convênio (CABERGS, IPE, particular)

Retorne um JSON válido com este formato (use null para campos não encontrados):
{{
    "patient_name": "nome completo aqui ou null",
    "patient_birth_date": "DD/MM/AAAA ou null",
    "appointment_date": "DD/MM/AAAA ou null",
    "appointment_time": "HH:MM ou null",
    "consultation_type": "clinica_geral/geriatria/domiciliar ou null",
    "insurance_plan": "CABERGS/IPE/particular ou null"
}}

IMPORTANTE: Se identificar que "patient_name" é uma frase de pedido (ex: "Eu Preciso Marcar Uma Consulta"), retorne null para esse campo."""

            # Chamar Claude para extrair
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                temperature=0.1,
                messages=[
                    {"role": "user", "content": extraction_prompt}
                ]
            )
            
            # Extrair resposta do Claude
            claude_response = ""
            if response.content:
                for content_block in response.content:
                    if hasattr(content_block, 'text'):
                        claude_response += content_block.text
            
            # Tentar parsear JSON da resposta
            import json
            import re
            
            # Buscar JSON na resposta (pode estar entre markdown code blocks ou direto)
            json_match = re.search(r'\{[^{}]*"patient_name"[^{}]*\}', claude_response, re.DOTALL)
            if not json_match:
                # Tentar encontrar qualquer JSON válido
                json_match = re.search(r'\{.*\}', claude_response, re.DOTALL)
            
            if json_match:
                try:
                    extracted_data = json.loads(json_match.group(0))
                    logger.info(f"✅ Dados extraídos pelo Claude: {extracted_data}")
                    return extracted_data
                except json.JSONDecodeError as e:
                    logger.error(f"Erro ao parsear JSON da resposta do Claude: {e}")
                    return {}
            else:
                logger.warning(f"⚠️ Claude não retornou JSON válido na resposta")
                return {}
            
        except Exception as e:
            logger.error(f"Erro ao extrair dados com Claude: {str(e)}")
            return {}

    def _handle_extract_patient_data(self, tool_input: Dict, db: Session, phone: str) -> str:
        """Tool: extract_patient_data - Usa Claude para extrair dados do paciente do histórico"""
        try:
            logger.info(f"🔍 Tool extract_patient_data chamada para {phone}")
            
            # Buscar contexto e histórico
            context = db.query(ConversationContext).filter_by(phone=phone).first()
            if not context:
                return "Nenhum histórico de mensagens disponível."
            
            # Usar função auxiliar para extrair dados
            extracted_data = self._extract_patient_data_with_claude(context)
            
            if not extracted_data:
                return "Nenhuma mensagem relevante encontrada no histórico."
            
            # Atualizar flow_data com dados extraídos
            if not context.flow_data:
                context.flow_data = MutableDict()
            
            # Atualizar apenas campos válidos (não None/null)
            if extracted_data.get("patient_name"):
                context.flow_data["patient_name"] = extracted_data["patient_name"]
                logger.info(f"💾 Nome atualizado no flow_data: {extracted_data['patient_name']}")
            
            if extracted_data.get("patient_birth_date"):
                context.flow_data["patient_birth_date"] = extracted_data["patient_birth_date"]
            
            if extracted_data.get("appointment_date"):
                context.flow_data["appointment_date"] = extracted_data["appointment_date"]
            
            if extracted_data.get("appointment_time"):
                # Validar formato HH:MM antes de salvar
                import re
                if re.match(r'^\d{2}:\d{2}$', extracted_data["appointment_time"]):
                    hour, minute = extracted_data["appointment_time"].split(':')
                    if minute == '00':
                        context.flow_data["appointment_time"] = extracted_data["appointment_time"]
            
            if extracted_data.get("consultation_type"):
                context.flow_data["consultation_type"] = extracted_data["consultation_type"]
            
            if extracted_data.get("insurance_plan"):
                context.flow_data["insurance_plan"] = extracted_data["insurance_plan"]
            
            db.commit()
            
            return f"Dados extraídos com sucesso:\nNome: {extracted_data.get('patient_name', 'Não encontrado')}\nData nascimento: {extracted_data.get('patient_birth_date', 'Não encontrada')}\nTipo consulta: {extracted_data.get('consultation_type', 'Não encontrado')}\nConvênio: {extracted_data.get('insurance_plan', 'Não encontrado')}"
            
        except Exception as e:
            logger.error(f"Erro ao extrair dados com Claude: {str(e)}")
            db.rollback()
            return f"Erro ao extrair dados: {str(e)}"

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