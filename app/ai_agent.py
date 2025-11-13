"""
Agente de IA com Claude SDK + Tools para agendamento de consultas.
Versão completa com menu estruturado e gerenciamento de contexto.
Corrigido: persistência de contexto + loop de processamento de tools.
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
import json
import logging
import pytz
import re
import unicodedata
from anthropic import Anthropic

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.simple_config import settings
from app.models import Appointment, AppointmentStatus, ConversationContext, PausedContact
from app.utils import (
    load_clinic_info, normalize_phone, parse_date_br, 
    format_datetime_br, now_brazil, get_brazil_timezone, round_up_to_next_5_minutes,
    get_minimum_appointment_datetime, format_date_br, normalize_time_format
)
from app.appointment_rules import appointment_rules

logger = logging.getLogger(__name__)


def format_closed_days(dias_fechados: List[str]) -> str:
    """Agrupa dias consecutivos e formata bonito"""
    if not dias_fechados:
        return ""
    
    from datetime import datetime
    
    # Converter para datetime e ordenar
    dates = []
    for d in dias_fechados:
        try:
            dates.append(datetime.strptime(d, '%d/%m/%Y'))
        except:
            continue
    
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
        self.special_holiday_ranges = [
            (datetime(2025, 12, 15).date(), datetime(2025, 12, 21).date()),
            (datetime(2025, 12, 26).date(), datetime(2026, 1, 4).date()),
        ]
        
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
        
        return f"""Você é a Beatriz, secretária da {clinic_name}. Você é prestativa, educada e ajuda pacientes de forma natural e conversacional.

INFORMAÇÕES DA CLÍNICA:
📍 Endereço: {endereco}
⏰ Horários de funcionamento:
{horarios_str}
⏱️ Duração das consultas: {duracao} minutos
📞 Telefone: {self.clinic_info.get('telefone', 'Não informado')}

═══════════════════════════════════════════════════════════
SEU OBJETIVO PRINCIPAL
═══════════════════════════════════════════════════════════

Ajudar pacientes a agendar consultas de forma eficiente e natural. Adapte-se ao estilo de comunicação do usuário e use as tools disponíveis conforme necessário.

═══════════════════════════════════════════════════════════
ABORDAGEM DE COMUNICAÇÃO
═══════════════════════════════════════════════════════════

MENU INICIAL:
- Quando não houver contexto claro de agendamento ou o usuário iniciar nova conversa, apresente o menu:

"Olá! Eu sou a Beatriz, secretária do {clinic_name}! 😊
Como posso te ajudar hoje?

ℹ️ Para deixar o atendimento mais rápido, envie uma mensagem por vez e aguarde minha resposta antes de mandar a próxima, combinado?

1️⃣ Marcar consulta (presencial na clínica)
2️⃣ Atendimento domiciliar
3️⃣ Remarcar/Cancelar consulta  
4️⃣ Receitas

Digite o número da opção desejada."
- Se o usuário já estiver no meio de um fluxo, mantenha o contexto e continue naturalmente

PRINCÍPIOS DE COMUNICAÇÃO:
- Seja conversacional e adapte-se ao estilo do usuário (formal ou informal)
- Peça informações de forma natural, uma por vez
- Se o usuário fornecer múltiplas informações juntas, extraia o que conseguir e pergunte o que faltar
- Se o usuário corrigir algo, agradeça e atualize os dados
- Se informação estiver incompleta ou ambígua, pergunte de forma clara e educada
- Se não entender algo, peça esclarecimento de forma amigável
- Quando o usuário pedir informações sobre a clínica:
  • Responda você mesma usando o que já sabe do clinic_info sempre que a resposta for curta (ex.: “Atendemos apenas no consultório”, “Sim, fazemos atendimento domiciliar”).  
  • Só chame a tool `get_clinic_info` quando precisar montar blocos completos (horários, listas grandes) ou quando estiver em dúvida sobre a informação.
  • Se a pergunta for genérica (ex.: “me fala da clínica”), peça para especificar ou responda de forma resumida; evite mandar o bloco completo sem necessidade.
  • Combine blocos apenas quando a pergunta mencionar explicitamente mais de um item.
  • Mantenha o tom acolhedor e ofereça ajuda adicional quando fizer sentido.

═══════════════════════════════════════════════════════════
FLUXO DE AGENDAMENTO
═══════════════════════════════════════════════════════════

Após o usuário escolher qualquer opção do menu inicial, siga esta sequência obrigatória:

1. NOME COMPLETO
   - Verifique se já existe um nome válido salvo para o telefone. Se não houver, peça EXCLUSIVAMENTE o nome completo (sem falar de data na mesma mensagem).
   - Aguarde e valide a resposta. Nome deve ter pelo menos duas palavras (nome + sobrenome).
   - Se já existir um nome salvo, confirme de forma natural se deve mantê-lo ou atualizá-lo.
   - Use a tool 'extract_patient_data' apenas quando precisar validar/recuperar o nome do histórico.

2. DATA DE NASCIMENTO
   - Somente depois de registrar um nome válido peça a data de nascimento (formato DD/MM/AAAA).
   - Se vier em formato incorreto, explique o motivo e solicite novamente.
   - IMPORTANTE: Se Python validar a data (sem erro_data), aceite imediatamente. Não questione datas aprovadas pelo sistema.
   - Lembre-se: alguém pode agendar para outra pessoa; mantenha os dados informados pelo usuário.

3. TIPO DE CONSULTA
   - Após ter nome e data, apresente apenas os nomes das consultas e peça para o paciente escrever o nome completo da opção desejada (ex.: "Clínica Geral" ou "Geriatria Clínica e Preventiva").
   - Reforce que a escolha deve ser textual; números só devem ser usados no menu principal.

3.1. FLUXO ESPECIAL - ATENDIMENTO DOMICILIAR (opção 2 do menu inicial):
   Quando o usuário escolher "Atendimento domiciliar" no menu inicial:
   1. NÃO chame find_next_available_slot (não precisa agendar horário específico)
   2. PRIMEIRO: Pergunte ao usuário com esta mensagem formatada (NÃO chame nenhuma tool ainda):
      "Perfeito! Para o atendimento domiciliar, preciso do seu endereço completo. Por favor, me informe:
      
      📍 Cidade
      🏘️ Bairro
      🛣️ Rua
      🏠 Número da casa
      
      Você pode enviar tudo junto ou separado, como preferir!"
   3. AGUARDE o usuário fornecer o endereço completo
   4. DEPOIS: Chame request_home_address para extrair e salvar o endereço fornecido
   5. Após request_home_address retornar sucesso, o sistema chamará notify_doctor_home_visit automaticamente
   6. Após notify_doctor_home_visit retornar sucesso, você receberá uma mensagem de confirmação para enviar ao paciente
   7. Envie a mensagem de confirmação e pergunte: "Posso te ajudar com mais alguma coisa?"
   8. Se resposta for "não" ou similar → chame end_conversation
   9. Se resposta for "sim" → ajude com o necessário e repita a pergunta até receber "não"

4. CONVÊNIO
   "Ótimo! Você possui convênio médico?

   Trabalhamos com os seguintes convênios:
   • CABERGS
   • IPE

   📋 Como responder:
   • Se você TEM um desses convênios → Digite o nome (CABERGS ou IPE)
   • Se você NÃO TEM convênio → Responda apenas "Não"

   Vamos prosseguir com consulta particular se você não tiver convênio."
   
   IMPORTANTE - INTERPRETAÇÃO DE CONVÊNIO:
   - Você DEVE identificar e interpretar o convênio quando o usuário mencionar durante a conversa
   - Use seu entendimento de linguagem natural para interpretar a intenção do usuário
   - Exemplos de identificação:
     * "CABERGS", "cabergs", "CaberGs" → CABERGS
     * "IPE", "ipe" → IPE
     * "não", "não tenho", "sem convênio", "particular" → Particular
     * "sim, tenho" (quando você perguntou sobre convênio) → perguntar qual específico
   - Quando identificar o convênio, salve mentalmente e use nas próximas interações
   - Normalize sempre os valores: CABERGS, IPE ou Particular (não "particular" minúsculo)
   - Ao chamar tools como find_next_available_slot ou create_appointment, se você identificou o convênio, passe como parâmetro insurance_plan
   - Se não passou como parâmetro, as tools buscarão automaticamente do flow_data
   
   MUDANÇA DE CONVÊNIO DURANTE CONFirmaÇÃO:
   - Quando o usuário estiver na etapa de confirmação (você perguntou "Posso confirmar o agendamento?") e mencionar mudança de convênio:
     * Exemplos: "quero trocar para particular", "mudar para CABERGS", "é IPE", "convênio errado"
   - O sistema detectará automaticamente e atualizará o flow_data
   - Um resumo atualizado será mostrado automaticamente com o novo convênio
   - Você deve pedir confirmação novamente após a atualização

5. BUSCA AUTOMÁTICA DE HORÁRIO
   - Após coletar convênio (ou particular), chame IMEDIATAMENTE a tool 'find_next_available_slot' SEM ADICIONAR TEXTO PRÉVIO
   - Não diga "vou buscar", "deixe-me buscar" ou "permita-me buscar" - apenas execute a tool diretamente
   - Esta tool busca o próximo horário disponível respeitando 48 horas exatas de antecedência mínima
   - A tool retorna um resumo completo formatado - repasse a mensagem ao usuário
   - O sistema calcula 48h a partir do momento atual, contando finais de semana também
   - IMPORTANTE: Quando receber resultado de find_next_available_slot, SEMPRE mostre o resumo completo retornado pela tool antes de pedir confirmação. Não assuma que o usuário já viu o resumo.

FLUXO COMPLETO APÓS COLETAR DADOS:
1. Chame find_next_available_slot (sem texto prévio)
2. Receba o resultado completo com resumo formatado
3. SEMPRE mostre o resumo completo ao usuário (copie exatamente o que a tool retornou)
4. Depois de mostrar o resumo, pergunte: "Posso confirmar o agendamento?"
5. Aguarde confirmação antes de criar agendamento

REGRAS CRÍTICAS PARA find_next_available_slot:
1. Quando receber resultado desta tool, você DEVE:
   a) Copiar EXATAMENTE o resumo completo retornado (incluindo todas as linhas: Nome, Tipo, Convênio, Data, Horário)
   b) Mostrar o resumo COMPLETO ao usuário (sem omitir nada, sem resumir, sem parafrasear)
   c) DEPOIS de mostrar o resumo completo, adicione: "Posso confirmar o agendamento?"
2. NUNCA pule a etapa de mostrar o resumo completo
3. NUNCA peça confirmação sem mostrar o resumo primeiro
4. NUNCA assuma que o usuário já viu o resumo - sempre mostre novamente
5. O resumo retornado pela tool contém TODAS as informações necessárias - use-o completamente

6. CONFIRMAÇÃO OU ALTERNATIVAS
   - Se usuário confirmar → use 'create_appointment' com os dados coletados
   - Se usuário rejeitar → chame 'find_alternative_slots' para mostrar 3 opções alternativas
   - Se usuário mencionar preferência (ex: "quinta à tarde") → interprete e use 'validate_date_and_show_slots' com a próxima ocorrência do dia após 48h
   - Se usuário escolher uma das 3 alternativas (1, 2 ou 3) → use os dados dessa opção para criar agendamento
   - Se rejeitar todas alternativas → pergunte qual dia prefere e use 'validate_date_and_show_slots' para mostrar horários

7. ESCOLHA DE HORÁRIO (fluxo manual)
   - Se usuário mencionar horário no formato HH:MM → use 'confirm_time_slot' para validar e mostrar resumo
   - Aguarde confirmação final antes de criar agendamento

═══════════════════════════════════════════════════════════
      - date: a data que foi validada anteriormente (appointment_date)
      - time: o horário que o usuário acabou de escolher
   
   b) Esta tool vai automaticamente:
      - Verificar se é horário inteiro (só aceita 08:00, 09:00, etc)
      - Verificar disponibilidade final (segurança contra race condition)
      - Mostrar resumo da consulta (nome, data, hora, tipo, convênio)
      - Pedir confirmação: "Posso confirmar o agendamento?"
   
   c) NÃO execute create_appointment imediatamente
   d) Apenas repasse a mensagem da tool ao usuário
   e) Aguarde confirmação do usuário ("sim", "confirma", "quero", etc)
   
   REGRA CRÍTICA: Se o usuário enviar QUALQUER mensagem contendo horário, você DEVE executar confirm_time_slot IMEDIATAMENTE, sem exceção.
   
   Exemplos de horários que devem acionar confirm_time_slot:
   - "14:00", "15:30", "10:00"
   - "às 14h", "15 horas", "10h"
   - "quatorze horas", "quinze e meia"
   - Qualquer menção a horário no formato HH:MM ou variações
   
   NÃO espere confirmação do usuário após ele escolher horário - execute a tool automaticamente.
   NÃO pergunte "você quis dizer 14:00?" - execute confirm_time_slot diretamente.

7.5. **REGRAS CRÍTICAS PARA RESPOSTAS APÓS TOOLS:**
   APÓS executar qualquer tool, você DEVE sempre gerar uma resposta de texto completa para o usuário.
   NUNCA retorne apenas um caractere ou espaço.
   Sua resposta deve ser útil e informativa.
   
   Exemplos:
   - Após confirm_time_slot, diga: "Horário confirmado! Posso criar o agendamento?" em vez de apenas "OK"
   - Após find_next_available_slot, sempre mostre o resumo completo antes de pedir confirmação
   - Após create_appointment, gere uma mensagem natural incluindo todas as informações importantes

8. **FLUXO CRÍTICO - Após confirmação do usuário:**
   a) Execute create_appointment com TODOS os dados
   b) Os dados vêm do flow_data (já foram salvos nas etapas anteriores)
   c) Quando create_appointment retornar sucesso, você receberá um contexto com informações importantes
   d) VOCÊ DEVE gerar uma mensagem natural e amigável incluindo APENAS as informações fornecidas:
      - NÃO inclua resumo da consulta (data, horário, paciente, tipo) - o usuário já sabe disso
      - NÃO inclua mensagem de sucesso em negrito ou emojis de celebração
      - Inclua APENAS as informações importantes:
        * Pedido para trazer últimos exames
        * Pedido para tragar lista de medicações
        * Endereço completo do consultório
        * Informação sobre cadeira de rodas disponível (se mencionado no contexto)
        * Informação sobre mensagem de lembrete que será enviada no dia da consulta para relembrar sobre a consulta
   e) Termine sempre perguntando: "Posso te ajudar com mais alguma coisa?"

IMPORTANTE - FLUXO DE CONFirmaÇÃO:
1. O fluxo é: validate_date_and_show_slots → confirm_time_slot → create_appointment
2. NÃO pule etapas
3. NÃO tente criar o agendamento antes de confirmar o horário
4. Use confirm_time_slot APENAS quando o usuário escolher um horário específico

═══════════════════════════════════════════════════════════
FERRAMENTAS E QUANDO USAR
═══════════════════════════════════════════════════════════

- get_clinic_info: Quando usuário perguntar sobre horários, endereço, telefone, dias fechados, etc. Antes de chamar, identifique a intenção e use o 'type' adequado:
  * "prices": perguntas sobre valores, preços, custos, quanto custa.
  * "hours": perguntas sobre horários, funcionamento, que horas atende.
  * "address": pedidos de endereço, localização, onde fica.
  * "phones": pedidos de telefone, contato, número.
  * "insurances": perguntas sobre convênios, planos, se aceita IPE/CABERGS etc.
  * "closed_days": perguntas sobre férias, feriados ou dias específicos sem atendimento.
  * "overview": use apenas quando o paciente pedir explicitamente uma visão geral completa ou combinar vários itens em uma única pergunta.
  Se a intenção não estiver clara, faça uma pergunta de esclarecimento antes de chamar a tool.

- extract_patient_data: Use quando o usuário mencionar seu nome mas você não tiver certeza ou precisar validar. Também use quando precisar extrair nome/data do histórico de mensagens, especialmente se houver dúvida sobre se um texto é nome real ou frase de pedido. IMPORTANTE: O sistema já extrai automaticamente nome quando formato é "Nome, DD/MM/YYYY", então use esta tool apenas se houver dúvida ou se precisar validar.

- find_next_available_slot: Use APÓS coletar nome, data nascimento, tipo consulta e convênio. IMPORTANTE: Antes de chamar, verifique se tem todos os dados necessários. O sistema tenta extrair automaticamente dados faltantes, mas se ainda faltar algo, pergunte ao usuário antes de chamar esta tool. Busca automaticamente próximo horário (48h mínimo). NÃO use quando consultation_type for 'domiciliar' - use request_home_address em vez disso.

- request_home_address: Use APENAS quando consultation_type for 'domiciliar' e patient_address não estiver no flow_data. Esta tool solicita e extrai o endereço completo do paciente.

- notify_doctor_home_visit: Use APENAS após receber endereço completo do paciente (após request_home_address retornar sucesso) para atendimento domiciliar. Esta tool envia notificação formatada para a doutora com todas as informações do paciente.

- find_alternative_slots: Use quando usuário rejeitar o primeiro horário oferecido. Retorna 3 opções alternativas.

- validate_date_and_show_slots: Use quando:
  - Usuário mencionar preferência de dia específico (ex: "quinta à tarde")
  - Usuário rejeitar todas as 3 alternativas e pedir para escolher dia
  - Precisar mostrar horários disponíveis de uma data específica

- confirm_time_slot: Use quando usuário escolher um horário específico (HH:MM). Valida e mostra resumo para confirmação.

- create_appointment: Use para criar o agendamento final após confirmação do usuário. Os dados já estão no flow_data.

- search_appointments: Use quando usuário quiser verificar consultas agendadas ou remarcar/cancelar.

- cancel_appointment: Use para cancelar uma consulta existente.

- request_human_assistance: Use APENAS quando usuário solicitar EXPLICITAMENTE falar com secretária ou atendente humano. 
  Exemplos válidos: "quero falar com a secretária", "preciso de atendente", "pode transferir para humano".
  NÃO use para: saudações como "Olá, Doutora", menções casuais ou quando usuário está apenas sendo educado.
  Lembre-se: o objetivo é automatizar - só transfira quando realmente necessário.

- end_conversation: Use quando usuário indicar que não precisa de mais nada (após pergunta "Posso te ajudar com mais alguma coisa?").

═══════════════════════════════════════════════════════════
RECUPERAÇÃO E ADAPTAÇÃO
═══════════════════════════════════════════════════════════

LIDANDO COM VARIAÇÕES:
- Se usuário usar linguagem informal, adapte sua resposta mantendo profissionalismo
- Se usuário der informações incompletas, pergunte o que falta de forma natural
- Se usuário pular etapas (ex: "quero marcar quinta às 15h"), tente extrair o que conseguir e pergunte o que faltar
- Se usuário mencionar algo fora do fluxo (ex: "quanto custa?" no meio do agendamento), responda brevemente e retome o fluxo

DETECTANDO CORREÇÕES:
- Se usuário disser "mudou", "corrigindo", "na verdade", "errei" → entenda como correção
- Agradeça a correção e atualize os dados
- Continue de onde parou

INTERPRETANDO ESCOLHAS:
- Aceite variações: "1", "primeira opção", "opção 1", "a primeira", etc
- Use contexto para entender intenções ambíguas
- Se não tiver certeza, pergunte de forma amigável

PERGUNTAS FORA DO FLUXO:
- Se usuário fizer perguntas sobre a clínica durante agendamento, responda brevemente usando 'get_clinic_info' e retome o fluxo
- Mantenha o contexto do agendamento ativo

═══════════════════════════════════════════════════════════
CICLO DE ATENDIMENTO E ENCERRAMENTO
═══════════════════════════════════════════════════════════

Após qualquer tarefa concluída (agendamento, cancelamento, resposta a dúvida):
- Sempre pergunte: "Posso te ajudar com mais alguma coisa?"
- Se usuário responder positivamente (sim, quero, preciso, etc) ou fizer nova pergunta → continue ajudando com contexto completo
- Se usuário responder negativamente (não, não preciso, obrigado, tchau, etc) → use imediatamente a tool 'end_conversation'
- Após usar 'end_conversation', encerre a conversa com mensagem de despedida amigável

REGRAS PARA end_conversation:
- Use APENAS quando usuário indicar claramente que não precisa de mais nada
- Exemplos de quando usar: "não", "não preciso", "não, obrigado", "só isso", "tchau", "até logo"
- NÃO use para perguntas do usuário ou quando ele está pedindo ajuda
- Após chamar end_conversation, o contexto será limpo automaticamente

Mantenha TODO o contexto histórico durante o ciclo (nome, data nascimento, etc) para evitar repetir perguntas.

═══════════════════════════════════════════════════════════
PERSISTÊNCIA E COMPLETAR TAREFAS
═══════════════════════════════════════════════════════════

PRINCÍPIO FUNDAMENTAL: Sempre complete a tarefa até o final. Não pare com mensagens genéricas.

QUANDO DADOS FALTAREM:
- NÃO retorne mensagem genérica de erro
- Tente extrair dados do histórico usando extract_patient_data primeiro
- Se não conseguir extrair, pergunte de forma natural e específica o que falta
- Mantenha o contexto e continue de onde parou
- Exemplo: Em vez de "Nome não encontrado", diga "Para continuar, preciso do seu nome completo. Pode me informar?"

QUANDO UMA TOOL FALHAR:
- Tente abordagem alternativa antes de retornar erro
- Se faltar dados, tente extrair do histórico antes de retornar erro
- Explique o problema de forma amigável e sugira solução
- NÃO desista - continue tentando até completar a tarefa

COMPLETANDO TAREFAS:
- Marcar consulta: Não pare até o agendamento estar confirmado e salvo
- Cancelar consulta: Não pare até o cancelamento estar completo e confirmado
- Reagendar: Não pare até a nova data estar confirmada e salva
- Receita: Não pare até a informação estar fornecida completamente

═══════════════════════════════════════════════════════════
VALIDAÇÕES CRÍTICAS
═══════════════════════════════════════════════════════════

- Confie nas validações do Python para dados críticos (formato de data, horários válidos)
- Se Python aprovar uma data (sem erro_data), aceite imediatamente
- Não questione ou valide manualmente dados já aprovados pelo sistema
- Para nome: use 'extract_patient_data' se houver dúvida se é nome real ou frase

═══════════════════════════════════════════════════════════

Lembre-se: Seja natural, adaptável e prestativa. Use as tools disponíveis conforme necessário e mantenha uma conversa fluida e educada. Sempre complete a tarefa até o final."""

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
                "name": "validate_date_and_show_slots",
                "description": "Validar data e mostrar todos os horários disponíveis do dia. Use quando: usuário mencionar preferência de dia específico (ex: 'quinta à tarde'), usuário rejeitar todas as 3 alternativas e pedir para escolher dia, ou precisar mostrar horários de uma data específica.",
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
                "name": "confirm_time_slot",
                "description": "Confirmar e validar o horário escolhido pelo paciente. Execute esta tool IMEDIATAMENTE quando detectar qualquer menção a horário no formato HH:MM, HH:MM, ou variações como 'às 14h', '15 horas', '10h', 'quatorze horas', etc. Use quando usuário mencionar um horário específico após ter uma data validada. Esta tool valida o horário e mostra resumo para confirmação final. IMPORTANTE: Execute automaticamente sem perguntar confirmação ao usuário.",
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
                "description": "Criar um novo agendamento de consulta. Use após confirmação final do usuário. Os dados necessários já devem estar coletados (nome, data nascimento, tipo consulta, convênio, data e horário da consulta).",
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
                "description": "Buscar agendamentos por telefone ou nome do paciente. Use quando usuário quiser verificar consultas agendadas, remarcar ou cancelar uma consulta.",
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
                "description": "Cancelar um agendamento existente. Use quando usuário solicitar cancelamento de uma consulta. É necessário o ID do agendamento e motivo do cancelamento.",
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
                "name": "find_next_available_slot",
                "description": "Encontra automaticamente o próximo horário disponível para agendamento respeitando 48h de antecedência mínima. Use esta tool APÓS coletar todos os dados do paciente (nome, data nascimento, tipo consulta e convênio). Esta tool busca o primeiro dia útil após 48h e encontra o primeiro horário disponível desse dia. Retorna resumo completo formatado pronto para confirmação. IMPORTANTE: Sempre mostre o resumo completo retornado pela tool ao usuário antes de pedir confirmação.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "find_alternative_slots",
                "description": "Encontra 3 opções alternativas de agendamento (primeiro horário disponível de 3 dias diferentes) respeitando 48h de antecedência mínima. Use esta tool quando o usuário rejeitar o primeiro horário oferecido. Retorna lista formatada com 3 opções numeradas para o usuário escolher.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "request_human_assistance",
                "description": "Transferir atendimento para SECRETÁRIA quando solicitado explicitamente. Use APENAS quando usuário solicitar claramente falar com secretária ou atendente humano (ex: 'quero falar com a secretária', 'preciso de atendente', 'pode transferir'). NÃO use para saudações casuais ou menções à doutora. Execute imediatamente sem perguntar confirmação quando houver solicitação explícita.",
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
                "name": "request_home_address",
                "description": "Extrai e salva o endereço completo do paciente para atendimento domiciliar. Use APENAS quando o usuário já forneceu o endereço completo (após você ter pedido o endereço). NÃO use quando o usuário ainda não forneceu o endereço - nesse caso, apenas peça o endereço sem chamar esta tool. Esta tool valida se a mensagem realmente contém um endereço antes de salvar.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "notify_doctor_home_visit",
                "description": "Envia notificação para a doutora sobre nova solicitação de atendimento domiciliar. Use APENAS após receber endereço completo do paciente (após request_home_address). Esta tool coleta nome, data nascimento, endereço e telefone do flow_data e envia mensagem formatada para a doutora.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "end_conversation",
                "description": "Encerrar conversa e limpar contexto do banco de dados quando usuário indicar claramente que não precisa de mais nada (ex: 'não', 'não preciso', 'não obrigado', 'só isso', 'tchau'). Use APENAS após perguntar 'Posso te ajudar com mais alguma coisa?' e receber resposta negativa. NÃO use para perguntas do usuário ou quando ele está pedindo ajuda.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        ]

    def _is_special_holiday_date(self, date_obj: datetime) -> bool:
        if not date_obj:
            return False
        target = date_obj.date()
        for start, end in self.special_holiday_ranges:
            if start <= target <= end:
                return True
        return False

    def _handoff_due_to_holiday(self, db: Session, phone: Optional[str]) -> str:
        if phone:
            return self._handle_request_special_holiday_pause(db, phone)
        return (
            "Durante este período especial a secretária está cuidando dos agendamentos. "
            "Vou pedir para ela entrar em contato com você em até 48 horas, tudo bem?"
        )

    def _handle_request_special_holiday_pause(self, db: Session, phone: Optional[str]) -> str:
        if not phone:
            return (
                "Esse período é tratado diretamente pela secretária. "
                "Ela entrará em contato com você em até 48 horas. Posso ajudar com algo mais?"
            )

        try:
            logger.info(f"⛱️ Aplicando pausa especial de férias para {phone}")

            existing_context = db.query(ConversationContext).filter_by(phone=phone).first()
            if existing_context:
                db.delete(existing_context)
                logger.info(f"🗑️ Contexto deletado para {phone} (pausa especial)")

            existing_pause = db.query(PausedContact).filter_by(phone=phone).first()
            if existing_pause:
                db.delete(existing_pause)
                logger.info(f"🗑️ Pausa anterior removida para {phone} (pausa especial)")

            paused_until = datetime.utcnow() + timedelta(hours=48)
            paused_contact = PausedContact(
                phone=phone,
                paused_until=paused_until,
                reason="special_holiday_request"
            )
            db.add(paused_contact)
            db.commit()

            logger.info(f"⏸️ Pausa especial registrada para {phone} até {paused_until}")
            return (
                "Perfeito! Esse período é organizado diretamente com nossa secretária. "
                "Ela vai entrar em contato com você em até 48 horas. Enquanto isso, posso ajudar com mais alguma coisa?"
            )
        except Exception as exc:
            logger.error(f"❌ Erro ao aplicar pausa especial: {exc}")
            db.rollback()
            return (
                "Houve um problema ao encaminhar para a secretária. "
                "Por favor, tente novamente em instantes ou fale conosco por telefone."
            )

    def _pause_contact_for_prescription(self, db: Session, phone: Optional[str]) -> None:
        """Pausa o contato por 48 horas após receita - deleta contexto e cria pausa"""
        if not phone:
            return
        
        try:
            logger.info(f"💊 Aplicando pausa de receita para {phone}")
            
            # Deletar contexto
            existing_context = db.query(ConversationContext).filter_by(phone=phone).first()
            if existing_context:
                db.delete(existing_context)
                logger.info(f"🗑️ Contexto deletado para {phone} (pausa de receita)")
            
            # Remover pausas anteriores
            existing_pause = db.query(PausedContact).filter_by(phone=phone).first()
            if existing_pause:
                db.delete(existing_pause)
                logger.info(f"🗑️ Pausa anterior removida para {phone} (pausa de receita)")
            
            # Criar pausa de 48 horas
            paused_until = datetime.utcnow() + timedelta(hours=48)
            paused_contact = PausedContact(
                phone=phone,
                paused_until=paused_until,
                reason="prescription_payment"
            )
            db.add(paused_contact)
            db.commit()
            
            logger.info(f"⏸️ Pausa de receita registrada para {phone} até {paused_until}")
        except Exception as exc:
            logger.error(f"❌ Erro ao aplicar pausa de receita: {exc}")
            db.rollback()

    def _analyze_prescription_message_with_claude(self, message: str) -> Dict[str, Any]:
        """
        Usa o Claude para classificar se cada campo da receita foi informado.
        Retorna estrutura:
        {
            "fields": {
                "medications": {"status": "provided|missing|declared_none", "value": "..."},
                "current_prescription": {...},
                "usage": {...},
                "dosage": {...}
            }
        }
        """
        result_template = {
            "fields": {
                "medications": {"status": "missing", "value": None},
                "current_prescription": {"status": "missing", "value": None},
                "usage": {"status": "missing", "value": None},
                "dosage": {"status": "missing", "value": None},
            }
        }

        cleaned_message = (message or "").strip()
        if not cleaned_message:
            return result_template

        prompt = f"""
Analyze the patient's message below and determine whether they provided each required prescription field.

Message:
\"\"\"{cleaned_message}\"\"\"

For each field, decide:
- status: "provided" if the patient supplied the information
- status: "declared_none" if the patient explicitly says they do not have or cannot provide it
- status: "missing" if the patient did not mention it or refused without explanation

Fields to check:
1. medications (the medicines or drugs they take)
2. current_prescription (diagnosis, existing prescription, or reason)
3. usage (how and when they take it, frequency or schedule)
4. dosage (amount, milligrams, drops, tablets, etc.)

Return ONLY a JSON object with this structure:
{{
  "fields": {{
    "medications": {{"status": "...", "value": "..."}},
    "current_prescription": {{"status": "...", "value": "..."}},
    "usage": {{"status": "...", "value": "..."}},
    "dosage": {{"status": "...", "value": "..."}}
  }}
}}
"""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=400,
                temperature=0,
                messages=[{"role": "user", "content": prompt}]
            )
            raw_text = ""
            if response.content:
                for block in response.content:
                    if hasattr(block, "text"):
                        raw_text += block.text

            import json
            cleaned_output = raw_text.strip()

            if not cleaned_output:
                raise ValueError("Claude returned empty response")

            if "```" in cleaned_output:
                import re
                matches = re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', cleaned_output, re.DOTALL)
                if matches:
                    cleaned_output = matches[0]

            cleaned_output = cleaned_output.strip()

            if not cleaned_output.startswith('{'):
                import re
                json_match = re.search(r'\{.*\}', cleaned_output, re.DOTALL)
                if json_match:
                    cleaned_output = json_match.group(0)

            parsed = json.loads(cleaned_output)

            if not isinstance(parsed, dict) or "fields" not in parsed:
                raise ValueError("Unexpected response structure from Claude")

            fields = parsed.get("fields", {})
            normalized = {}
            for key in ["medications", "current_prescription", "usage", "dosage"]:
                data = fields.get(key, {})
                status = data.get("status", "missing")
                value = data.get("value")
                if status not in {"provided", "missing", "declared_none"}:
                    status = "missing"
                if isinstance(value, str):
                    value = value.strip() or None
                normalized[key] = {"status": status, "value": value}

            return {"fields": normalized}
        except Exception as exc:
            logger.error(f"❌ Erro ao analisar informações de receita com Claude: {exc}")
            try:
                logger.debug(f"Resposta completa do Claude para depuração: {raw_text!r}")
            except Exception:
                logger.debug("Resposta do Claude indisponível para depuração.")
            return result_template

    def _build_prescription_address_prompt(self, reminder: bool = False) -> str:
        base = (
            "Obrigada! Agora me informe o endereço completo para entrega ou retirada:\n\n"
            "📍 Cidade\n"
            "🏘️ Bairro\n"
            "🛣️ Rua\n"
            "🏠 Número do imóvel\n\n"
            "Pode enviar tudo junto em uma única mensagem."
        )
        if reminder:
            return (
                "Para prosseguir, preciso do endereço completo (cidade, bairro, rua e número). "
                "Envie tudo em uma mesma mensagem, por favor."
            )
        return base

    def _is_valid_address(self, address: str) -> bool:
        if not address:
            return False
        if len(address) < 12:
            return False
        has_letter = any(ch.isalpha() for ch in address)
        has_number = any(ch.isdigit() for ch in address)
        return has_letter and has_number

    def _build_prescription_payment_message(self) -> str:
        return (
            "Perfeito! Recebi as informações da sua receita.\n\n"
            "💰 Valor: R$ 25,00\n"
            "🔑 Chave Pix: 51999546355\n"
            "⏳ Assim que o comprovante for enviado, a Dra. Rose prepara a receita em até 2 dias úteis.\n"
            "📄 Receitas branca/controlada podem ser enviadas digitalmente.\n"
            "📄 Receitas azul ou amarela precisam ser retiradas no consultório, de segunda a sexta das 14h às 18h.\n\n"
            "Quando tiver o comprovante, é só me enviar por aqui."
        )

    def _notify_doctor_prescription(self, context: ConversationContext, db: Session, phone: Optional[str]) -> None:
        if not context:
            return
        flow = context.flow_data or {}
        if flow.get("prescription_notified"):
            return

        patient_name = flow.get("patient_name", "Não informado")
        patient_birth_date = flow.get("patient_birth_date", "Não informado")
        details = flow.get("prescription_details", {})
        address = flow.get("prescription_address", "Não informado")
        doctor_phone = self.clinic_info.get("informacoes_adicionais", {}).get("telefone_doutora")
        if not doctor_phone:
            logger.error("❌ Telefone da doutora não encontrado para notificação de receita.")
            return

        contact = phone or flow.get("patient_phone", "Não informado")
        def format_field(field_key: str) -> str:
            field_data = details.get(field_key, {}) if isinstance(details, dict) else {}
            status = field_data.get("status", "missing")
            value = field_data.get("value")

            if status == "provided" and value:
                return value
            if status == "declared_none":
                return "Paciente informou que não possui"
            return "Não informado"

        message = (
            "📝 NOVA SOLICITAÇÃO DE RECEITA\n\n"
            f"👤 Paciente: {patient_name}\n"
            f"📅 Data de nascimento: {patient_birth_date}\n"
            f"💊 Medicamentos: {format_field('medications')}\n"
            f"📄 Receita/diagnóstico: {format_field('current_prescription')}\n"
            f"🕒 Modo de uso: {format_field('usage')}\n"
            f"⚖️ Dosagem: {format_field('dosage')}\n"
            f"📍 Endereço: {address}\n"
            f"📞 Contato: {contact}"
        )

        try:
            from app.main import send_message_task
            send_message_task.delay(normalize_phone(doctor_phone), message)
            flow["prescription_notified"] = True
            context.flow_data = flow
            flag_modified(context, "flow_data")
            db.commit()
            logger.info("✅ Notificação de receita enviada para a doutora.")
        except Exception as e:
            logger.error(f"❌ Erro ao enviar notificação de receita: {e}")

    def _normalize_and_validate_date(self, date_str: str) -> Optional[str]:
        """
        Normaliza e valida uma string de data no formato DD/MM/YYYY.
        
        Args:
            date_str: String de data no formato DD/MM/YYYY
            
        Returns:
            String normalizada no formato DD/MM/YYYY ou None se inválida
        """
        try:
            # Validar formato básico
            if not re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', date_str):
                return None
            
            # Parsear data
            date_obj = datetime.strptime(date_str, '%d/%m/%Y')
            
            # Validar se data não é muito antiga (antes de 1900)
            if date_obj.year < 1900:
                return None
            
            # Validar se data não é muito futura (mais de 10 anos no futuro)
            current_year = datetime.now().year
            if date_obj.year > current_year + 10:
                return None
            
            # Normalizar formato (garantir DD/MM/YYYY com zeros à esquerda)
            day, month, year = date_str.split('/')
            normalized = f"{day.zfill(2)}/{month.zfill(2)}/{year}"
            
            logger.info(f"📅 Data validada: {date_str} → {normalized}")
            return normalized
            
        except (ValueError, AttributeError) as e:
            logger.warning(f"⚠️ Data inválida: {date_str} - {str(e)}")
            return None
    
    def _extract_appointment_data_from_messages(self, messages: list) -> dict:
        """Extrai dados básicos de agendamento do histórico de mensagens.
        Versão simplificada: apenas detecção rápida de datas, horários e escolhas numéricas.
        Para extração de nome, confiar no Claude via tool extract_patient_data.
        """
        try:
            data = {
                "patient_name": None,  # Agora vamos extrair aqui também
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
                    # Priorizar última data mencionada quando há múltiplas
                    for match in reversed(date_matches):
                        day, month, year = match
                        full_date = f"{day.zfill(2)}/{month.zfill(2)}/{year}"
                        
                        # Normalizar e validar data
                        normalized_date = self._normalize_and_validate_date(full_date)
                        if normalized_date:
                            y = int(year)
                            
                            if not data["patient_birth_date"] and y < 2010:
                                # Provavelmente data de nascimento
                                data["patient_birth_date"] = normalized_date
                                logger.info(f"📅 Data nascimento extraída (regex): {full_date} → {normalized_date}")
                                
                                # 3. EXTRAÇÃO DE NOME quando formato é "Nome, DD/MM/YYYY" ou "Nome DD/MM/YYYY"
                                # Se encontrou data de nascimento, tentar extrair nome que vem antes dela
                                if not data["patient_name"]:
                                    # Padrão: texto antes da data (pode ter vírgula ou espaço)
                                    # Ex: "Andressa Schenkel, 01/08/2002" ou "Andressa Schenkel 01/08/2002"
                                    name_pattern = r'^(.+?)(?:\s*,\s*|\s+)(?:' + re.escape(full_date) + r')'
                                    name_match = re.search(name_pattern, content, re.IGNORECASE)
                                    
                                    if name_match:
                                        candidate_name = name_match.group(1).strip()
                                        # Validar se parece com nome real
                                        words = candidate_name.split()
                                        if len(words) >= 2 and len(candidate_name) > 5:
                                            # Verificar se não é frase comum
                                            common_phrases = [
                                                "preciso marcar", "quero agendar", "preciso de", "gostaria de",
                                                "meu nome é", "sou", "me chamo", "olá", "oi", "bom dia", "boa tarde"
                                            ]
                                            if not any(phrase in candidate_name.lower() for phrase in common_phrases):
                                                # Validar que contém apenas letras, espaços, hífens e acentos
                                                if re.match(r"^[a-zA-ZÀ-ÿ\s\-']+$", candidate_name):
                                                    data["patient_name"] = candidate_name
                                                    logger.info(f"💾 Nome extraído automaticamente: {candidate_name}")
                                    
                                    # Se não encontrou com padrão acima, tentar padrão mais simples
                                    # Procura por 2+ palavras antes da data
                                    if not data["patient_name"]:
                                        # Remover data da mensagem e pegar o que sobra
                                        content_without_date = re.sub(r'\s*\d{1,2}/\d{1,2}/\d{4}\s*', ' ', content).strip()
                                        # Pegar primeiras palavras (até 4 palavras, mínimo 2)
                                        words_before_date = content_without_date.split()[:4]
                                        if len(words_before_date) >= 2:
                                            candidate_name = ' '.join(words_before_date)
                                            # Validar novamente
                                            if len(candidate_name) > 5:
                                                common_phrases = [
                                                    "preciso marcar", "quero agendar", "preciso de", "gostaria de",
                                                    "meu nome é", "sou", "me chamo", "olá", "oi", "bom dia", "boa tarde"
                                                ]
                                                if not any(phrase in candidate_name.lower() for phrase in common_phrases):
                                                    if re.match(r"^[a-zA-ZÀ-ÿ\s\-']+$", candidate_name):
                                                        data["patient_name"] = candidate_name
                                                        logger.info(f"💾 Nome extraído automaticamente (fallback): {candidate_name}")
                            
                            elif not data["appointment_date"] and y >= 2010:
                                # Provavelmente data de consulta
                                data["appointment_date"] = normalized_date
                                logger.info(f"📅 Data consulta extraída (regex): {full_date} → {normalized_date}")
                
                # 4. EXTRAÇÃO DE TIPO DE CONSULTA - interpretar respostas textuais
                normalized_content = content.lower()
                if "geriatr" in normalized_content:
                    data["consultation_type"] = "geriatria"
                    logger.info("💾 Tipo de consulta identificado: geriatria")
                elif "clínica geral" in normalized_content or "clinica geral" in normalized_content:
                    data["consultation_type"] = "clinica_geral"
                    logger.info("💾 Tipo de consulta identificado: clínica geral")
                
                # 5. EXTRAÇÃO DE CONVÊNIO - Removida detecção via regex
                # A detecção de convênio agora é feita totalmente pelo Claude durante a conversa
                # Claude identifica e interpreta naturalmente quando o usuário menciona convênio
            
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

    def _normalize_text_for_weekday(self, text: str) -> str:
        replacements = {
            "á": "a", "à": "a", "ã": "a", "â": "a",
            "é": "e", "ê": "e",
            "í": "i",
            "ó": "o", "ô": "o", "õ": "o",
            "ú": "u",
            "ç": "c"
        }
        normalized = text.lower()
        for original, replacement in replacements.items():
            normalized = normalized.replace(original, replacement)
        return normalized

    def _detect_custom_schedule_request(self, message: str) -> Optional[Dict[str, Any]]:
        """Identifica se a mensagem contém referência clara a data ou dia específico (com ou sem horário)."""
        if not message:
            return None
        
        result: Dict[str, Any] = {}
        
        # Detectar data explícita DD/MM/AAAA ou DD-MM-AAAA
        date_match = re.search(r'\b(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})\b', message)
        if date_match:
            day, month, year = date_match.groups()
            try:
                normalized_date = f"{int(day):02d}/{int(month):02d}/{int(year):04d}"
                # Validar data rapidamente
                if parse_date_br(normalized_date):
                    result["date"] = normalized_date
            except ValueError:
                pass
        
        # Detectar dia da semana
        normalized = self._normalize_text_for_weekday(message)
        weekday_keywords = {
            "segunda": 0,
            "segundafeira": 0,
            "segunda feira": 0,
            "terca": 1,
            "terca-feira": 1,
            "terca feira": 1,
            "quarta": 2,
            "quarta-feira": 2,
            "quarta feira": 2,
            "quinta": 3,
            "quinta-feira": 3,
            "quinta feira": 3,
            "sexta": 4,
            "sexta-feira": 4,
            "sexta feira": 4,
            "sabado": 5,
            "sabado-feira": 5,
            "sabado feira": 5,
            "domingo": 6,
            "domingo-feira": 6,
            "domingo feira": 6
        }
        if "weekday" not in result:
            for keyword, index in weekday_keywords.items():
                if re.search(rf'\b{keyword}\b', normalized):
                    result["weekday"] = index
                    break
        
        # Detectar horário (HH:MM, HHh, HH horas)
        time_candidate = None
        time_match = re.search(r'\b(\d{1,2}):(\d{2})\b', message)
        if time_match:
            time_candidate = f"{time_match.group(1)}:{time_match.group(2)}"
        else:
            time_match = re.search(r'\b(\d{1,2})\s*h(?:oras)?\b', normalized)
            if time_match:
                time_candidate = f"{time_match.group(1)}:00"
            else:
                time_match = re.search(r'\b(\d{1,2})\s*horas?\b', normalized)
                if time_match:
                    time_candidate = f"{time_match.group(1)}:00"
        
        if time_candidate:
            normalized_time = normalize_time_format(time_candidate)
            if normalized_time:
                result["time"] = normalized_time
        
        return result or None

    def _get_next_available_date_for_weekday(self, weekday_index: int) -> Optional[datetime]:
        """Retorna a próxima data >= 48h a partir de agora que cai no dia da semana fornecido."""
        if weekday_index is None or not (0 <= weekday_index <= 6):
            return None
        
        minimum_datetime = get_minimum_appointment_datetime()
        candidate_date = minimum_datetime.date()
        
        # Avançar até encontrar o dia desejado
        for _ in range(14):  # Limite de segurança (duas semanas)
            if candidate_date.weekday() == weekday_index:
                return datetime.combine(candidate_date, datetime.min.time())
            candidate_date += timedelta(days=1)
        
        return None

    def _process_custom_schedule_request(
        self,
        request: Dict[str, Any],
        context: ConversationContext,
        db: Session,
        phone: str
    ) -> Optional[str]:
        """Processa uma solicitação de agendamento personalizada interpretada do texto do usuário."""
        if not request:
            return None
        
        date_str = request.get("date")
        weekday_index = request.get("weekday")
        
        inferred_from_weekday = False
        
        if not date_str and weekday_index is not None:
            next_date = self._get_next_available_date_for_weekday(weekday_index)
            if not next_date:
                return "❌ Não consegui encontrar datas disponíveis para esse dia da semana. Pode informar uma data no formato DD/MM/AAAA?"
            date_str = format_date_br(next_date)
            inferred_from_weekday = True
        
        if not date_str:
            return None
        
        if context:
            if not context.flow_data:
                context.flow_data = {}
            context.flow_data.pop("alternative_slots", None)
            context.flow_data["alternatives_offered"] = False
            context.flow_data["awaiting_custom_date"] = False
            db.commit()
        
        if request.get("time"):
            return self._handle_confirm_time_slot(
                {"date": date_str, "time": request["time"]},
                db,
                phone
            )
        
        return self._handle_validate_date_and_show_slots(
            {
                "date": date_str,
                "auto_adjust_to_future": inferred_from_weekday or request.get("auto_adjust_to_future")
            },
            db,
            phone
        )

    def _detect_insurance_change_intent(self, message: str) -> bool:
        """
        Detecta se a mensagem indica intenção de mudar o convênio.
        
        Returns:
            True se detectar intenção de mudar convênio, False caso contrário
        """
        message_lower = message.lower().strip()
        
        # Palavras-chave que indicam mudança de convênio
        insurance_change_keywords = [
            "trocar convênio", "trocar convenio", "mudar convênio", "mudar convenio",
            "alterar convênio", "alterar convenio", "quero particular", "prefiro particular",
            "quero cabergs", "prefiro cabergs", "quero ipe", "prefiro ipe",
            "é particular", "eh particular", "será particular", "sera particular",
            "vou particular", "mudar para particular", "trocar para particular",
            "mudar para cabergs", "trocar para cabergs", "mudar para ipe", "trocar para ipe",
            "convênio errado", "convenio errado", "convênio está errado", "convenio esta errado"
        ]
        
        # Verificar se contém alguma palavra-chave
        for keyword in insurance_change_keywords:
            if keyword in message_lower:
                return True
        
        return False

    def _detect_insurance_in_message(self, message: str, context: Optional[ConversationContext] = None) -> Optional[str]:
        """
        Resolve o convênio mencionado em uma mensagem utilizando o mini prompt do Claude.
        Mantém uma detecção regex simples apenas como fallback emergencial.
        """
        if not message:
            return None
        
        resolved = self._resolve_insurance_with_claude(message, context=context)
        if resolved:
            return resolved
        
        return self._detect_insurance_with_regex(message)

    def _detect_insurance_with_regex(self, message: str) -> Optional[str]:
        """
        Fallback mínimo baseado em regex para identificar convênio em casos óbvios.
        Deve ser usado apenas quando o Claude não conseguir interpretar a mensagem.
        """
        if not message:
            return None
        
        message_lower = message.lower()
        
        if "cabergs" in message_lower:
            return "CABERGS"
        
        if re.search(r'\bipe\b', message_lower):
            return "IPE"
        
        negative_phrases = [
            "não tenho", "nao tenho", "não possuo", "nao possuo",
            "sem convênio", "sem convenio", "não tenho convênio", "nao tenho convenio",
            "não possuo convênio", "nao possuo convenio",
            "sem plano", "não uso", "nao uso", "particular"
        ]
        
        if any(phrase in message_lower for phrase in negative_phrases):
            return "Particular"
        
        return None

    def _resolve_insurance_with_claude(
        self,
        message: str,
        context: Optional[ConversationContext] = None,
        *,
        extra_metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        Resolve o convênio usando um mini prompt dedicado no Claude e retorna o valor normalizado.
        """
        if not message:
            return None
        
        try:
            recent_context = ""
            if context and context.messages:
                # Considerar apenas últimas 2 interações (assistant + user) para dar mínimo contexto
                last_turns = []
                for msg in reversed(context.messages):
                    if msg.get("role") == "assistant":
                        last_turns.append(f"Secretária: {msg.get('content', '').strip()}")
                    elif msg.get("role") == "user":
                        last_turns.append(f"Paciente: {msg.get('content', '').strip()}")
                    if len(last_turns) >= 4:
                        break
                last_turns.reverse()
                recent_context = "\n".join(last_turns)
            
            metadata_hint = ""
            if extra_metadata:
                try:
                    metadata_hint = json.dumps(extra_metadata, ensure_ascii=False)
                except Exception:
                    metadata_hint = ""
            
            instructions = f"""Você é responsável por identificar o convênio médico mencionado pelo paciente.
Analise a mensagem mais recente considerando estas regras:
- Dê prioridade para afirmações positivas como "só CABERGS", "apenas CABERGS", "mas tenho CABERGS".
- Se o paciente negar um convênio, mas afirmar outro, retorne o afirmado.
- Se o paciente reforçar que não possui convênio ou quer pagar por conta, retorne "Particular".
- Caso não haja informação suficiente ou a mensagem seja ambígua, retorne null.
- Não invente nomes de convênios fora da lista.

Convênios aceitos: CABERGS, IPE, Particular (sem convênio).

Histórico recente (caso exista):
{recent_context or '[sem histórico adicional]'}

Mensagem atual do paciente:
\"\"\"{message}\"\"\"

Metadados opcionais:
{metadata_hint or '[sem metadados]'}

Responda EXCLUSIVAMENTE com um JSON válido no formato:
{{
  "insurance_plan": "CABERGS|IPE|Particular|null",
  "confidence": "low|medium|high",
  "justification": "explicação curta em português"
}}
"""
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=200,
                temperature=0.1,
                messages=[{"role": "user", "content": instructions}]
            )
            
            raw_output = ""
            if response and response.content:
                for content_block in response.content:
                    text_block = getattr(content_block, "text", None)
                    if text_block:
                        raw_output += text_block.strip() + "\n"
            raw_output = raw_output.strip()
            
            if not raw_output:
                logger.warning("⚠️ Claude não retornou conteúdo ao resolver convênio.")
                return None
            
            payload_str = raw_output
            code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_output, flags=re.DOTALL | re.IGNORECASE)
            if code_block_match:
                payload_str = code_block_match.group(1)
            else:
                # Tentar isolar JSON caso haja texto extra fora do bloco
                first_brace = raw_output.find("{")
                last_brace = raw_output.rfind("}")
                if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                    payload_str = raw_output[first_brace:last_brace + 1]
            
            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError:
                logger.warning(f"⚠️ Falha ao converter resposta do Claude em JSON: {raw_output}")
                return None
            
            plan_value = payload.get("insurance_plan")
            normalized_plan = self._normalize_insurance_candidate(plan_value)
            
            confidence = payload.get("confidence")
            justification = payload.get("justification")
            logger.info(
                "🤖 Claude mini prompt para convênio",
                extra={
                    "user_message": message,
                    "raw_output": raw_output,
                    "normalized_plan": normalized_plan,
                    "confidence": confidence,
                    "justification": justification
                }
            )
            
            return normalized_plan
        except Exception as exc:
            logger.error(f"❌ Erro ao resolver convênio com Claude: {exc}")
            return None

    def _normalize_insurance_candidate(self, plan_value: Optional[Any]) -> Optional[str]:
        """Normaliza o valor retornado pelo Claude para os convênios suportados."""
        if plan_value is None:
            return None
        
        if isinstance(plan_value, str):
            normalized_text = plan_value.strip().lower()
            if not normalized_text:
                return None
        else:
            # Se o tipo não for string, tentar converter
            normalized_text = str(plan_value).strip().lower()
        
        normalized_text = normalized_text.replace('"', "").replace("'", "")
        
        mapping = {
            "cabergs": "CABERGS",
            "ipe": "IPE",
            "particular": "Particular",
            "null": None,
            "none": None
        }
        
        return mapping.get(normalized_text)

    def _should_auto_trigger_slot_search(self, context: ConversationContext) -> bool:
        if not context or not context.flow_data:
            return False
        
        flow = context.flow_data
        if flow.get("menu_choice") != "booking":
            return False
        
        plan = flow.get("insurance_plan")
        if not plan:
            return False
        
        if flow.get("auto_slot_last_plan") == plan:
            return False
        
        required_keys = ["patient_name", "patient_birth_date", "consultation_type"]
        if not all(flow.get(key) for key in required_keys):
            return False
        
        blocking_flags = [
            "awaiting_patient_name",
            "awaiting_patient_birth_date",
            "awaiting_consultation_type",
            "awaiting_custom_date",
            "awaiting_home_address",
        ]
        if any(flow.get(flag) for flag in blocking_flags):
            return False
        
        return True

    def _trigger_auto_slot_search(self, context: ConversationContext, db: Session, phone: str) -> Optional[str]:
        if not self._should_auto_trigger_slot_search(context):
            return None
        
        flow = context.flow_data
        plan = flow.get("insurance_plan")
        
        logger.info(f"🚀 Disparando busca automática de horários após captura do convênio: {plan}")
        
        flow["auto_slot_last_plan"] = plan
        flow.pop("appointment_date", None)
        flow.pop("appointment_time", None)
        flow.pop("alternative_slots", None)
        flow["alternatives_offered"] = False
        flow["pending_confirmation"] = False
        flag_modified(context, "flow_data")
        
        return self._handle_find_next_available_slot({}, db, phone)

    def _extract_insurance_from_message(self, message: str, context: ConversationContext) -> Optional[str]:
        """
        Extrai o novo convênio mencionado na mensagem usando o mini prompt centralizado.
        """
        return self._resolve_insurance_with_claude(message, context=context)

    def _detect_main_menu_choice(self, message: str, context: ConversationContext) -> Optional[str]:
        """Detecta se a mensagem corresponde a uma escolha do menu principal."""
        if not message:
            return None

        if context and context.flow_data:
            flow = context.flow_data
            if flow.get("awaiting_patient_name") or flow.get("awaiting_patient_birth_date"):
                return None
            if flow.get("alternative_slots") or flow.get("pending_confirmation") or flow.get("awaiting_custom_date"):
                return None
            if flow.get("awaiting_consultation_type"):
                return None
            if flow.get("menu_choice") is not None:
                return None

        normalized = message.strip().lower()
        if not normalized:
            return None

        normalized = normalized.replace("opção", "opcao").replace("opções", "opcoes")
        digits_only = "".join(ch for ch in normalized if ch.isdigit())
        if digits_only in {"1", "2", "3", "4"} and len(normalized) <= 4:
            return {
                "1": "booking",
                "2": "home_visit",
                "3": "reschedule",
                "4": "prescription"
            }[digits_only]

        if any(keyword in normalized for keyword in ["marcar consulta", "agendar", "nova consulta", "quero marcar", "agendamento"]):
            return "booking"
        if any(keyword in normalized for keyword in ["domicílio", "domicilio", "domiciliar", "visita em casa", "atendimento em casa"]):
            return "home_visit"
        if any(keyword in normalized for keyword in ["remarcar", "cancelar", "cancelamento", "remarcação", "remarcacao", "desmarcar"]):
            return "reschedule"
        if any(keyword in normalized for keyword in ["receita", "receitas", "prescrição", "prescricao"]):
            return "prescription"

        return None

    def _start_identity_collection(self, context: ConversationContext, menu_choice: str):
        """Inicia fluxo de coleta de identidade (nome e data) após seleção de menu."""
        if not context.flow_data:
            context.flow_data = {}

        flow = context.flow_data
        flow["menu_choice"] = menu_choice
        flow["awaiting_patient_name"] = True
        flow["awaiting_patient_birth_date"] = False
        flow.pop("patient_name", None)
        flow.pop("patient_birth_date", None)
        flow.pop("consultation_type", None)
        flow.pop("patient_address", None)
        flow.pop("pending_home_address", None)
        flow.pop("pending_doctor_notification", None)
        flow.pop("awaiting_birth_date_correction", None)
        flow.pop("pending_confirmation", None)
        flow.pop("alternative_slots", None)
        flow["alternatives_offered"] = False
        flow.pop("awaiting_custom_date", None)
        if menu_choice == "home_visit":
            flow["consultation_type"] = "domiciliar"
        flow.pop("awaiting_consultation_type", None)
        flow.pop("awaiting_prescription_details", None)
        flow.pop("awaiting_prescription_address", None)
        flow.pop("prescription_details", None)
        flow.pop("prescription_address", None)
        flow.pop("prescription_notified", None)
        context.current_flow = menu_choice
        flag_modified(context, "flow_data")

    def _build_name_prompt(self, menu_choice: str) -> str:
        """Retorna mensagem adequada para solicitar o nome completo."""
        prompts = {
            "booking": "Perfeito! Para começarmos, me informe seu nome completo, por favor.",
            "home_visit": "Perfeito! Vamos organizar o atendimento domiciliar. Pode me informar seu nome completo, por favor?",
            "reschedule": "Claro! Para localizar o atendimento, me informe o nome completo do paciente, por favor.",
            "prescription": "Combinado! Para seguir com as receitas, me informe o nome completo do paciente, por favor."
        }
        return prompts.get(menu_choice, "Para continuarmos, me informe seu nome completo, por favor.")

    def _build_post_identity_prompt(self, menu_choice: str) -> str:
        """Mensagem padrão para a próxima etapa após captar nome e data."""
        if menu_choice == "booking":
            return (
                "Perfeito! Agora me conte qual consulta você prefere:\n\n"
                "• Clínica Geral – R$ 300\n"
                "• Geriatria Clínica e Preventiva – R$ 300\n\n"
                "Escreva o nome da opção desejada."
            )
        if menu_choice == "home_visit":
            return (
                "Perfeito! Para o atendimento domiciliar, preciso do seu endereço completo. Por favor, me informe:\n\n"
                "📍 Cidade\n"
                "🏘️ Bairro\n"
                "🛣️ Rua\n"
                "🏠 Número da casa\n\n"
                "Você pode enviar tudo junto ou separado, como preferir!"
            )
        if menu_choice == "reschedule":
            return (
                "Obrigada! Localizei seu cadastro. Qual consulta você deseja remarcar ou cancelar? "
                "Se puder, me informe a data ou horário que lembra."
            )
        if menu_choice == "prescription":
            return (
                "Perfeito! Para preparar sua receita, envie em UMA única mensagem as informações abaixo:\n\n"
                "• Nome dos remédios que você usa\n"
                "• Receita atual ou indicação médica\n"
                "• Modo de uso (frequência e horários)\n"
                "• Dosagem ou miligramagem\n\n"
                "Por favor, envie tudo de uma vez para que eu possa prosseguir."
            )
        return "Obrigada! Como posso te ajudar a seguir?"

    def _record_interaction(
        self,
        context: ConversationContext,
        user_message: str,
        assistant_message: str,
        db: Session,
        flow_modified: bool = False
    ):
        """Registra interação interceptada (usuário + assistente) e sincroniza o banco."""
        timestamp = datetime.utcnow().isoformat()
        context.messages.append({
            "role": "user",
            "content": user_message,
            "timestamp": timestamp
        })
        context.messages.append({
            "role": "assistant",
            "content": assistant_message,
            "timestamp": datetime.utcnow().isoformat()
        })
        flag_modified(context, "messages")
        if flow_modified:
            flag_modified(context, "flow_data")
        context.last_activity = datetime.utcnow()
        db.commit()

    def _generate_updated_summary(self, context: ConversationContext, db: Session) -> str:
        """
        Gera resumo atualizado com os dados do flow_data.
        
        Args:
            context: Contexto da conversa
            db: Sessão do banco de dados
            
        Returns:
            String formatada com resumo completo
        """
        if not context or not context.flow_data:
            return "Erro ao gerar resumo: dados não disponíveis."
        
        # Extrair dados do flow_data
        patient_name = context.flow_data.get("patient_name", "")
        appointment_date = context.flow_data.get("appointment_date", "")
        appointment_time = context.flow_data.get("appointment_time", "")
        consultation_type = context.flow_data.get("consultation_type", "clinica_geral")
        insurance_plan = context.flow_data.get("insurance_plan", "particular")
        
        # Normalizar convênio
        if insurance_plan.lower() == "ipe":
            insurance_plan = "IPE"
        elif insurance_plan.lower() == "cabergs":
            insurance_plan = "CABERGS"
        elif insurance_plan.lower() in ["particular", "particula"]:
            insurance_plan = "Particular"
        
        # Buscar nome formatado do convênio
        convenios_aceitos = self.clinic_info.get('convenios_aceitos', {})
        convenio_data = convenios_aceitos.get(insurance_plan, {})
        convenio_nome = convenio_data.get('nome', insurance_plan)
        
        # Mapear tipo de consulta
        tipo_map = {
            "clinica_geral": "Clínica Geral",
            "geriatria": "Geriatria Clínica e Preventiva",
            "domiciliar": "Atendimento Domiciliar"
        }
        tipo_nome = tipo_map.get(consultation_type, consultation_type)
        
        # Montar resumo
        msg = "✅ Resumo atualizado da consulta:\n\n"
        msg += "📋 *Resumo da consulta:*\n"
        if patient_name:
            msg += f"👤 Nome: {patient_name}\n"
        if appointment_date:
            msg += f"📅 Data: {appointment_date}\n"
        if appointment_time:
            msg += f"⏰ Horário: {appointment_time}\n"
        msg += f"🏥 Tipo: {tipo_nome}\n"
        msg += f"💳 Convênio: {convenio_nome}\n"
        
        return msg

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
            
            # 2. Verificação de timeout removida - agora é proativa via scheduler
            
            # 3. Decidir se deve encerrar contexto por resposta negativa
            if self._should_end_context(context, message):
                logger.info(f"🔚 Encerrando contexto para {phone} por resposta negativa do usuário")
                db.delete(context)
                db.commit()
                return "Foi um prazer atender você! Até logo! 😊"

            # 4. Verificar se há alternativas salvas e usuário escolheu uma (1, 2 ou 3)
            if not context.flow_data:
                context.flow_data = {}
                flag_modified(context, "flow_data")
            flow_data = context.flow_data

            # Detectar solicitações naturais de data/horário personalizadas
            custom_request = None
            if flow_data and (
                flow_data.get("pending_confirmation")
                or flow_data.get("awaiting_custom_date")
                or flow_data.get("alternatives_offered")
            ):
                custom_request = self._detect_custom_schedule_request(message)
                if custom_request and (custom_request.get("date") or custom_request.get("weekday")):
                    logger.info(f"🗓️ Solicitação personalizada detectada: {custom_request}")
                    response = self._process_custom_schedule_request(custom_request, context, db, phone)
                    if response:
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

            # 3. Detectar seleção de menu e iniciar coleta sequencial de identidade
            menu_choice = None
            if flow_data.get("menu_choice") is None and not flow_data.get("awaiting_patient_name") and not flow_data.get("awaiting_patient_birth_date"):
                menu_choice = self._detect_main_menu_choice(message, context)

            if menu_choice:
                logger.info(f"🧭 Menu option '{menu_choice}' selecionada para {phone}")
                if not context.flow_data:
                    context.flow_data = {}
                flow_ref = context.flow_data
                if menu_choice == "reschedule":
                    lower_msg = message.lower()
                    if "cancel" in lower_msg and "remarc" not in lower_msg:
                        flow_ref["cancel_intent"] = "cancel"
                    elif "remarc" in lower_msg:
                        flow_ref["cancel_intent"] = "reschedule"
                    else:
                        flow_ref["cancel_intent"] = "cancel"
                    flow_ref.pop("pending_appointments_map", None)
                    flow_ref.pop("awaiting_cancel_choice", None)
                    flow_ref.pop("awaiting_cancel_reason", None)
                    flow_ref.pop("selected_appointment", None)
                    flag_modified(context, "flow_data")
                self._start_identity_collection(context, menu_choice)
                prompt = self._build_name_prompt(menu_choice)
                self._record_interaction(context, message, prompt, db, flow_modified=True)
                return prompt

            if flow_data.get("menu_choice") == "booking" and flow_data.get("awaiting_consultation_type"):
                normalized = message.strip().lower()
                if normalized in {"1", "2", "opcao 1", "opção 1", "opcao 2", "opção 2"}:
                    reminder = (
                        "Para escolher o tipo de consulta, escreva o nome completo da opção, por exemplo: "
                        "\"Clínica Geral\" ou \"Geriatria Clínica e Preventiva\"."
                    )
                    self._record_interaction(context, message, reminder, db)
                    return reminder

            if flow_data.get("awaiting_patient_name"):
                name_extraction = self._extrair_nome_e_data_robusto(message)
                captured_name = name_extraction.get("nome")

                if captured_name:
                    flow_data["patient_name"] = captured_name
                    flow_data["awaiting_patient_name"] = False
                    flow_data["awaiting_patient_birth_date"] = True
                    flag_modified(context, "flow_data")
                    first_name = captured_name.split()[0]
                    response = (
                        f"Muito obrigada, {first_name}! Agora, para manter o cadastro certinho, "
                        "me informe sua data de nascimento no formato DD/MM/AAAA."
                    )
                    logger.info(f"👤 Nome registrado para {phone}: {captured_name}")
                    self._record_interaction(context, message, response, db, flow_modified=True)
                    return response

                error_msg = name_extraction.get("erro_nome") or "Para continuar, preciso do seu nome completo (nome e sobrenome)."
                response = f"{error_msg.strip().rstrip('.')}. Pode me informar seu nome completo, por favor?"
                logger.warning(f"⚠️ Nome inválido informado por {phone}: {message}")
                self._record_interaction(context, message, response, db)
                return response

            if flow_data.get("awaiting_patient_birth_date"):
                birth_extraction = self._extrair_nome_e_data_robusto(message)
                birth_date = birth_extraction.get("data")

                if birth_date:
                    flow_data["patient_birth_date"] = birth_date
                    flow_data["awaiting_patient_birth_date"] = False
                    flow_data.pop("awaiting_birth_date_correction", None)
                    if flow_data.get("menu_choice") == "prescription":
                        flow_data["awaiting_prescription_details"] = True
                        flow_data["prescription_details"] = {}
                        flow_data.pop("prescription_address", None)
                        flow_data["awaiting_prescription_address"] = False
                        flag_modified(context, "flow_data")
                    flag_modified(context, "flow_data")
                    logger.info(f"📅 Data de nascimento registrada para {phone}: {birth_date}")

                    if flow_data.get("menu_choice") == "reschedule":
                        appointments_map: Dict[str, Dict[str, Any]] = {}
                        search_response = self._handle_search_appointments(
                            {
                                "phone": phone,
                                "name": flow_data.get("patient_name"),
                                "birth_date": birth_date,
                                "consultation_type": flow_data.get("consultation_type"),
                                "insurance_plan": flow_data.get("insurance_plan"),
                                "only_future": True,
                                "flow_map": appointments_map
                            },
                            db
                        )

                        if appointments_map:
                            flow_data["pending_appointments_map"] = appointments_map
                            flow_data["awaiting_cancel_choice"] = True
                            prompt = (
                                search_response
                                + "\nPor favor, digite o número da consulta que deseja cancelar ou remarcar."
                            )
                            self._record_interaction(context, message, prompt, db, flow_modified=True)
                            return prompt

                        flow_data.pop("pending_appointments_map", None)
                        flow_data.pop("awaiting_cancel_choice", None)
                        no_result_prompt = (
                            search_response
                            + "\nSe esses dados estiverem corretos, posso pedir para nossa secretária verificar manualmente."
                        )
                        self._record_interaction(context, message, no_result_prompt, db, flow_modified=True)
                        return no_result_prompt

                    menu_choice = flow_data.get("menu_choice")
                    if menu_choice == "booking":
                        flow_data["awaiting_consultation_type"] = True
                        flag_modified(context, "flow_data")

                    next_prompt = self._build_post_identity_prompt(menu_choice)
                    self._record_interaction(context, message, next_prompt, db, flow_modified=True)
                    return next_prompt
                else:
                    error_msg = birth_extraction.get("erro_data") or "Não consegui identificar sua data de nascimento."
                    response = f"{error_msg.strip().rstrip('.')}. Pode enviar no formato DD/MM/AAAA?"
                    logger.warning(f"⚠️ Data de nascimento inválida informada por {phone}: {message}")
                    self._record_interaction(context, message, response, db)
                    return response

            if flow_data.get("awaiting_prescription_details"):
                analysis = self._analyze_prescription_message_with_claude(message)
                fields = analysis.get("fields", {})
                provided = []
                missing = []

                for field, data in fields.items():
                    status = data.get("status", "missing")
                    if status == "provided":
                        provided.append(field)
                    elif status == "missing":
                        missing.append(field)

                def _humanize(field_key: str) -> str:
                    mapping = {
                        "medications": "nome dos remédios",
                        "current_prescription": "receita/diagnóstico",
                        "usage": "modo de uso",
                        "dosage": "dosagem/miligramagem"
                    }
                    return mapping.get(field_key, field_key)

                essential_provided = "medications" in provided and (
                    "usage" in provided or "dosage" in provided
                )

                if not essential_provided and missing:
                    missing_text = ", ".join(_humanize(field) for field in missing)
                    reminder = (
                        "Recebi suas informações, mas ainda preciso confirmar alguns itens: "
                        f"{missing_text}. Se algum deles não existir, é só me dizer; caso contrário, pode enviar tudo juntinho (remédios, diagnóstico, modo de uso e dosagem)."
                    )
                    self._record_interaction(context, message, reminder, db)
                    return reminder

                flow_data["prescription_details"] = fields
                flow_data["awaiting_prescription_details"] = False
                flow_data["awaiting_prescription_address"] = True
                flag_modified(context, "flow_data")

                address_prompt = self._build_prescription_address_prompt()
                self._record_interaction(context, message, address_prompt, db, flow_modified=True)
                return address_prompt

            if flow_data.get("awaiting_prescription_address"):
                address = message.strip()
                if not self._is_valid_address(address):
                    reminder = self._build_prescription_address_prompt(reminder=True)
                    self._record_interaction(context, message, reminder, db)
                    return reminder

                flow_data["prescription_address"] = address
                flow_data["awaiting_prescription_address"] = False
                flag_modified(context, "flow_data")
                db.commit()

                instructions = self._build_prescription_payment_message()
                self._record_interaction(context, message, instructions, db, flow_modified=True)

                try:
                    self._notify_doctor_prescription(context, db, phone)
                except Exception as notify_error:
                    logger.error(f"❌ Erro ao notificar doutora sobre receita: {notify_error}")

                # Pausar contato por 48 horas após enviar instruções de pagamento
                try:
                    self._pause_contact_for_prescription(db, phone)
                except Exception as pause_error:
                    logger.error(f"❌ Erro ao pausar contato após receita: {pause_error}")

                return instructions

            if flow_data.get("awaiting_cancel_choice"):
                selection = message.strip()
                mapping = flow_data.get("pending_appointments_map", {})
                if selection in mapping:
                    appointment_data = mapping[selection]
                    logger.info(f"🗑️ Usuário {phone} selecionou agendamento {selection}: {appointment_data}")

                    flow_data["selected_appointment"] = appointment_data
                    flow_data.pop("awaiting_cancel_choice", None)
                    flow_data.pop("pending_appointments_map", None)
                    flag_modified(context, "flow_data")
                    db.commit()

                    if flow_data.get("cancel_intent") == "cancel":
                        flow_data["awaiting_cancel_reason"] = True
                        flag_modified(context, "flow_data")
                        db.commit()
                        prompt = (
                            "Entendido. Pode me informar o motivo do cancelamento? "
                            "Assim consigo registrar tudo direitinho."
                        )
                        self._record_interaction(context, message, prompt, db, flow_modified=True)
                        return prompt
                    else:
                        flow_data["awaiting_reschedule_start"] = True
                        appointment_date = appointment_data.get("date")
                        appointment_time = appointment_data.get("time")
                        tipo = appointment_data.get("consultation_type")
                        conv = appointment_data.get("insurance_plan")

                        prompt = (
                            "Perfeito, vamos remarcar sua consulta. "
                            "Você prefere manter o mesmo tipo de consulta e convênio? "
                            "Se quiser alterar, me avise. Caso contrário, posso buscar novos horários."
                        )

                        if tipo:
                            context.flow_data["consultation_type"] = tipo
                        if conv:
                            context.flow_data["insurance_plan"] = conv.strip().lower()

                        context.flow_data["awaiting_custom_date"] = True
                        flag_modified(context, "flow_data")
                        db.commit()
                        self._record_interaction(context, message, prompt, db, flow_modified=True)
                        return prompt
                else:
                    reminder = (
                        "Não reconheci essa opção. Por favor, escolha o número da consulta que deseja "
                        "cancelar ou remarcar, conforme a lista anterior."
                    )
                    self._record_interaction(context, message, reminder, db)
                    return reminder

            if flow_data.get("awaiting_cancel_reason"):
                reason = message.strip()
                appointment_data = flow_data.get("selected_appointment")

                if not appointment_data:
                    flow_data.pop("awaiting_cancel_reason", None)
                    flag_modified(context, "flow_data")
                    db.commit()
                    return "Não consegui localizar o agendamento selecionado. Pode tentar novamente?"

                result_message = self._handle_cancel_appointment(
                    {
                        "appointment_id": appointment_data.get("id"),
                        "reason": reason or "Cancelado pelo paciente via WhatsApp"
                    },
                    db
                )

                flow_data.pop("awaiting_cancel_reason", None)
                flow_data.pop("selected_appointment", None)
                flow_data["pending_confirmation"] = False
                flow_data["alternatives_offered"] = False
                flow_data.pop("awaiting_custom_date", None)
                flow_data.pop("cancel_intent", None)
                flag_modified(context, "flow_data")
                db.commit()

                follow_up = result_message + "\n\nPosso ajudar com mais alguma coisa?"
                self._record_interaction(context, message, follow_up, db, flow_modified=True)
                return follow_up

            if flow_data.get("awaiting_reschedule_start"):
                flow_data.pop("awaiting_reschedule_start", None)
                flow_data["awaiting_custom_date"] = True
                flag_modified(context, "flow_data")
                db.commit()
                prompt = (
                    "Sem problemas! Qual dia funciona melhor para você? "
                    "Pode informar a data no formato DD/MM/AAAA ou dizer, por exemplo, "
                    "\"quinta-feira à tarde\"."
                )
                self._record_interaction(context, message, prompt, db, flow_modified=True)
                return prompt

            # 4. Verificar se há alternativas salvas e usuário escolheu uma (1, 2 ou 3)
            if context.flow_data and context.flow_data.get("alternative_slots"):
                message_stripped = message.strip()
                if message_stripped in ["1", "2", "3"]:
                    try:
                        option_index = int(message_stripped) - 1  # Converter para índice (0, 1, 2)
                        alternatives = context.flow_data.get("alternative_slots", [])
                        
                        if 0 <= option_index < len(alternatives):
                            selected_alt = alternatives[option_index]
                            logger.info(f"✅ Usuário {phone} escolheu alternativa {message_stripped}: {selected_alt}")
                            
                            # Atualizar flow_data com a alternativa escolhida
                            context.flow_data["appointment_date"] = selected_alt["date"]
                            context.flow_data["appointment_time"] = selected_alt["time"]
                            context.flow_data["pending_confirmation"] = True
                            context.flow_data.pop("alternative_slots", None)  # Limpar alternativas
                            context.flow_data["alternatives_offered"] = False
                            context.flow_data.pop("awaiting_custom_date", None)
                            db.commit()
                            
                            # Mostrar resumo e pedir confirmação final
                            patient_name = context.flow_data.get("patient_name", "")
                            consultation_type = context.flow_data.get("consultation_type", "clinica_geral")
                            insurance_plan = context.flow_data.get("insurance_plan", "particular")
                            
                            tipo_map = {
                                "clinica_geral": "Clínica Geral",
                                "geriatria": "Geriatria Clínica e Preventiva",
                                "domiciliar": "Atendimento Domiciliar ao Paciente Idoso"
                            }
                            tipo_nome = tipo_map.get(consultation_type, "Clínica Geral")
                            
                            tipos_consulta = self.clinic_info.get('tipos_consulta', {})
                            tipo_data = tipos_consulta.get(consultation_type, {})
                            tipo_valor = tipo_data.get('valor', 0)
                            
                            convenio_nome = insurance_plan if insurance_plan != "particular" else "Particular"
                            
                            dias_semana = ['segunda-feira', 'terça-feira', 'quarta-feira', 
                                          'quinta-feira', 'sexta-feira', 'sábado', 'domingo']
                            alt_date = parse_date_br(selected_alt["date"])
                            if alt_date:
                                dia_nome_completo = dias_semana[alt_date.weekday()]
                            else:
                                dia_nome_completo = ""
                            
                            response = f"Perfeito! Você escolheu a opção {message_stripped}.\n\n"
                            response += f"📋 *Resumo da consulta:*\n"
                            response += f"👤 Nome: {patient_name}\n"
                            response += f"🏥 Tipo: {tipo_nome} - R$ {tipo_valor}\n"
                            response += f"💳 Convênio: {convenio_nome}\n"
                            response += f"📅 Data: {selected_alt['date']} ({dia_nome_completo})\n"
                            response += f"⏰ Horário: {selected_alt['time']}\n\n"
                            response += f"Posso confirmar o agendamento?"
                            
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
                    except (ValueError, IndexError, KeyError) as e:
                        logger.error(f"Erro ao processar escolha de alternativa: {str(e)}")
                        # Continuar com processamento normal
                else:
                    alt_intent = self._detect_confirmation_intent(message)
                    if alt_intent == "negative":
                        logger.info(f"❌ Usuário {phone} recusou as alternativas sugeridas")
                        context.flow_data.pop("alternative_slots", None)
                        context.flow_data["alternatives_offered"] = False
                        context.flow_data["awaiting_custom_date"] = True
                        db.commit()

                        response = (
                            "Sem problemas! Qual dia funciona melhor para você? "
                            "Pode me informar uma data no formato DD/MM/AAAA ou dizer, por exemplo, "
                            "\"terça-feira pela manhã\"."
                        )

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
        
            # 5. Verificar se há confirmação pendente ANTES de processar com Claude
            if context.flow_data and context.flow_data.get("pending_confirmation"):
                # NOVA DETECÇÃO: Verificar se usuário quer mudar convênio especificamente
                if self._detect_insurance_change_intent(message):
                    logger.info(f"🔄 Usuário {phone} quer mudar convênio durante confirmação")
                    
                    # Extrair novo convênio mencionado
                    novo_convenio = self._extract_insurance_from_message(message, context)
                    
                    if novo_convenio:
                        # Atualizar flow_data
                        context.flow_data["insurance_plan"] = novo_convenio
                        db.commit()
                        logger.info(f"💾 Convênio atualizado no flow_data: {novo_convenio}")
                        
                        # Regenerar resumo com novo convênio
                        resumo_atualizado = self._generate_updated_summary(context, db)
                        
                        # Manter pending_confirmation para continuar o fluxo de confirmação
                        response = resumo_atualizado + "\n\nPosso confirmar o agendamento?"
                        
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
                    else:
                        logger.warning(f"⚠️ Não foi possível extrair novo convênio da mensagem")
                        # Continuar com fluxo normal (perguntar o que mudar)
                
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
                        context.flow_data = {}
                    context.flow_data["pending_confirmation"] = False
                    context.flow_data["alternatives_offered"] = False
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
                    logger.info(f"❌ Usuário {phone} recusou o horário sugerido")
                    if not context.flow_data:
                        context.flow_data = {}
                    alternatives_already_offered = context.flow_data.get("alternatives_offered", False)

                    if not alternatives_already_offered:
                        logger.info("🔁 Oferecendo alternativas automaticamente")
                        # Encerrar confirmação atual e apresentar alternativas
                        context.flow_data["pending_confirmation"] = False
                        context.flow_data["alternatives_offered"] = True
                        db.commit()

                        alternatives_message = self._handle_find_alternative_slots({}, db, phone)

                        context.messages.append({
                            "role": "user",
                            "content": message,
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        context.messages.append({
                            "role": "assistant",
                            "content": alternatives_message,
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        context.last_activity = datetime.utcnow()
                        db.commit()

                        return alternatives_message

                    logger.info("🗓️ Alternativas já oferecidas - solicitando nova disponibilidade")
                    context.flow_data["pending_confirmation"] = False
                    context.flow_data["awaiting_custom_date"] = True
                    # Limpar alternativas anteriores para evitar reapresentação
                    context.flow_data.pop("alternative_slots", None)
                    db.commit()

                    response = (
                        "Tudo bem! Qual dia fica melhor para você? "
                        "Você pode me informar o dia no formato DD/MM/AAAA ou dizer, por exemplo, "
                        "\"quinta-feira à tarde\"."
                    )

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
                temperature=0.3,
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
                            
                            # Se há tool_result anterior, usar como fallback (para outras tools)
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
                            
                            # Verificação especial para validate_and_check_availability
                            if content.name == "validate_and_check_availability":
                                if "disponível" in tool_result.lower() and "não" not in tool_result.lower():
                                    # Horário disponível, adicionar hint para Claude criar agendamento
                                    tool_result += "\n\n[SYSTEM: Execute create_appointment agora com os dados coletados: nome, data_nascimento, data_consulta, horario_consulta]"
                            
                            # Lógica especial: após request_home_address retornar sucesso, chamar notify_doctor_home_visit automaticamente
                            if content.name == "request_home_address" and "registrado" in tool_result.lower():
                                logger.info("🏠 request_home_address executada com sucesso - chamando notify_doctor_home_visit automaticamente")
                                
                                # Verificar se dados necessários estão no flow_data antes de chamar
                                context = db.query(ConversationContext).filter_by(phone=phone).first()
                                if context and context.flow_data:
                                    flow_data = context.flow_data
                                    has_name = flow_data.get("patient_name")
                                    has_birth_date = flow_data.get("patient_birth_date")
                                    has_address = flow_data.get("patient_address")
                                    
                                    if has_name and has_birth_date and has_address:
                                        # Chamar notify_doctor_home_visit diretamente
                                        notify_result = self._execute_tool("notify_doctor_home_visit", {}, db, phone)
                                        
                                        if "sucesso" in notify_result.lower() or "enviada" in notify_result.lower():
                                            # Notificação enviada com sucesso
                                            confirmation_message = "Perfeito! Registrei sua solicitação de atendimento domiciliar. A doutora vai entrar em contato com você em breve para agendar o melhor horário.\n\nPosso te ajudar com mais alguma coisa?"
                                            
                                            # Construir contexto completo para Claude processar a confirmação
                                            # Incluir: histórico + request_home_address tool_use + tool_result + notify_doctor_home_visit tool_use + tool_result + mensagem de confirmação
                                            current_response = self.client.messages.create(
                                                model="claude-sonnet-4-20250514",
                                                max_tokens=2000,
                                                temperature=0.3,
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
                                                    },
                                                    {
                                                        "role": "assistant",
                                                        "content": [{"type": "tool_use", "name": "notify_doctor_home_visit", "input": {}, "id": "auto_notify"}]
                                                    },
                                                    {
                                                        "role": "user",
                                                        "content": [
                                                            {
                                                                "type": "tool_result",
                                                                "tool_use_id": "auto_notify",
                                                                "content": notify_result
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "role": "user",
                                                        "content": f"[SYSTEM: Envie a seguinte mensagem ao paciente: {confirmation_message}]"
                                                    }
                                                ]
                                            )
                                            
                                            # Processar resposta do Claude
                                            if current_response.content and len(current_response.content) > 0:
                                                if current_response.content[0].type == "text":
                                                    bot_response = current_response.content[0].text
                                                    break
                                                elif current_response.content[0].type == "tool_use":
                                                    # Claude pode ter chamado uma tool (ex: end_conversation), continuar processamento
                                                    content = current_response.content[0]
                                                    continue
                                            
                                            # Se Claude não retornou nada, usar mensagem de confirmação diretamente
                                            bot_response = confirmation_message
                                            break
                                        else:
                                            # Erro ao enviar notificação, adicionar ao tool_result para Claude tratar
                                            tool_result += f"\n\n[ERRO: Falha ao enviar notificação para a doutora: {notify_result}]"
                                    else:
                                        # Dados faltando, adicionar ao tool_result para Claude tratar
                                        missing = []
                                        if not has_name: missing.append("nome")
                                        if not has_birth_date: missing.append("data de nascimento")
                                        if not has_address: missing.append("endereço")
                                        tool_result += f"\n\n[ERRO: Faltam informações para enviar notificação: {', '.join(missing)}]"
                            
                            logger.info(f"🔧 Iteration {iteration}: Tool {content.name} result: {tool_result[:200] if len(tool_result) > 200 else tool_result}")
                            
                            # Fazer follow-up com o resultado
                            current_response = self.client.messages.create(
                                model="claude-sonnet-4-20250514",
                                max_tokens=2000,
                                temperature=0.3,
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
                            
                            # Interceptação universal de respostas curtas
                            # Verificar se resposta é muito curta (< 100 chars) ou stop_reason é "end_turn"
                            content_text = ""
                            if current_response.content and len(current_response.content) > 0:
                                if current_response.content[0].type == "text":
                                    content_text = current_response.content[0].text
                            
                            is_short = len(content_text) < 100 or current_response.stop_reason == "end_turn"
                            
                            # NÃO interceptar extract_patient_data e request_home_address - são tools internas, Claude deve continuar o fluxo
                            if is_short and tool_result and content.name != "extract_patient_data" and content.name != "request_home_address":
                                logger.warning(f"⚠️ Resposta muito curta ou end_turn após {content.name}. Interceptando resposta.")
                                
                                # Lógica especial para find_next_available_slot
                                if content.name == "find_next_available_slot":
                                    palavras_chave = ["Nome", "Tipo", "Convênio", "Data", "Horário", "Resumo"]
                                    tem_palavras_chave = any(palavra in content_text for palavra in palavras_chave)
                                    
                                    if not tem_palavras_chave:
                                        # Adicionar resumo completo + pergunta de confirmação
                                        resposta_completa = tool_result + "\n\nPosso confirmar o agendamento?"
                                    else:
                                        # Já tem palavras-chave, apenas adicionar pergunta se não tiver
                                        if "confirmar" not in content_text.lower():
                                            resposta_completa = tool_result + "\n\nPosso confirmar o agendamento?"
                                        else:
                                            resposta_completa = tool_result
                                else:
                                    # Para outras tools, usar o resultado diretamente
                                    resposta_completa = tool_result
                                
                                # Criar objeto simples com type e text para substituir o conteúdo
                                class SimpleTextContent:
                                    def __init__(self, text):
                                        self.type = "text"
                                        self.text = text
                                
                                current_response.content = [SimpleTextContent(resposta_completa)]
                                logger.info(f"✅ Resposta interceptada e substituída pelo resultado da tool {content.name}")
                                
                                # Processar imediatamente o conteúdo interceptado
                                if current_response.content[0].type == "text":
                                    bot_response = current_response.content[0].text
                                    break
                            
                            # Verificar se Claude retornou texto após processar tool (iteração normal)
                            if current_response.content and len(current_response.content) > 0:
                                if current_response.content[0].type == "text":
                                    bot_response = current_response.content[0].text
                                    break
                            
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
                context.flow_data = {}
            
            # Extrair dados do histórico
            extracted = self._extract_appointment_data_from_messages(context.messages)
            
            # Salvar nome extraído automaticamente se encontrado
            if extracted.get("patient_name") and not context.flow_data.get("patient_name"):
                context.flow_data["patient_name"] = extracted["patient_name"]
                logger.info(f"💾 Nome extraído automaticamente e salvo no flow_data: {extracted['patient_name']}")
            
            # FALLBACK: Tentar extrair nome se não estiver no flow_data mas houver padrão claro nas mensagens
            if not context.flow_data.get("patient_name"):
                # Verificar últimas mensagens do usuário por padrões claros de nome
                import re
                name_patterns = [
                    r'(?:meu nome é|sou|me chamo|me chama|chamo-me)\s+([A-ZÁÉÍÓÚÂÊÔÇ][a-záéíóúâêôçãõ]+(?:\s+[A-ZÁÉÍÓÚÂÊÔÇ][a-záéíóúâêôçãõ]+)+)',
                    r'(?:nome|chamo)\s+([A-ZÁÉÍÓÚÂÊÔÇ][a-záéíóúâêôçãõ]+(?:\s+[A-ZÁÉÍÓÚÂÊÔÇ][a-záéíóúâêôçãõ]+)+)',
                ]
                
                # Verificar últimas 5 mensagens do usuário
                for msg in reversed(context.messages[-10:]):  # Últimas 10 mensagens
                    if msg.get("role") == "user":
                        content = (msg.get("content") or "").strip()
                        for pattern in name_patterns:
                            match = re.search(pattern, content, re.IGNORECASE)
                            if match:
                                candidate_name = match.group(1).strip()
                                # Validar se parece com nome real (mínimo 2 palavras, não é frase comum)
                                words = candidate_name.split()
                                if len(words) >= 2 and len(candidate_name) > 5:
                                    # Verificar se não é frase comum
                                    common_phrases = ["preciso marcar", "quero agendar", "preciso de", "gostaria de"]
                                    if not any(phrase in candidate_name.lower() for phrase in common_phrases):
                                        context.flow_data["patient_name"] = candidate_name
                                        logger.info(f"💾 Nome extraído automaticamente (fallback): {candidate_name}")
                                        break
                        if context.flow_data.get("patient_name"):
                            break
            
            # Se ainda não tem nome e Claude não chamou extract_patient_data, pode tentar usar a tool internamente
            # Mas isso só aconteceria se o usuário mencionou nome mas não foi extraído
            
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
                if (
                    context.flow_data.get("menu_choice") == "home_visit"
                    and tipo_anterior == "domiciliar"
                    and extracted["consultation_type"] != "domiciliar"
                ):
                    logger.info("↩️ Ignorando tipo de consulta extraído porque o fluxo atual é de atendimento domiciliar.")
                else:
                    context.flow_data["consultation_type"] = extracted["consultation_type"]
                    if context.flow_data.get("awaiting_consultation_type"):
                        context.flow_data["awaiting_consultation_type"] = False
                        flag_modified(context, "flow_data")
                if tipo_anterior:
                    logger.info(f"💾 Tipo consulta ATUALIZADO no flow_data: {tipo_anterior} → {extracted['consultation_type']}")
                else:
                    logger.info(f"💾 Tipo consulta salvo no flow_data: {extracted['consultation_type']}")
            
            # INTERCEPTAÇÃO: Fluxo domiciliar
            consultation_type = context.flow_data.get("consultation_type")
            if consultation_type == "domiciliar":
                patient_address = context.flow_data.get("patient_address")
                doctor_notified = context.flow_data.get("doctor_notified", False)
                
                # Se não tem endereço, instruir Claude a chamar request_home_address
                if not patient_address:
                    logger.info("🏠 Detectado atendimento domiciliar sem endereço - instruindo Claude a chamar request_home_address")
                    # Adicionar instrução no prompt para Claude chamar a tool
                    # Isso será feito via prompt, mas podemos adicionar uma flag no flow_data
                    context.flow_data["pending_home_address"] = True
                    flag_modified(context, "flow_data")
                    db.commit()
                # Se tem endereço mas não notificou, instruir Claude a chamar notify_doctor_home_visit
                elif patient_address and not doctor_notified:
                    logger.info("🏠 Detectado atendimento domiciliar com endereço mas sem notificação - instruindo Claude a chamar notify_doctor_home_visit")
                    context.flow_data["pending_doctor_notification"] = True
                    flag_modified(context, "flow_data")
                    db.commit()
            
            # SEMPRE atualizar convênio quando extraído (permite correção)
            if extracted.get("insurance_plan"):
                convenio_anterior = context.flow_data.get("insurance_plan")
                context.flow_data["insurance_plan"] = extracted["insurance_plan"]
                if convenio_anterior:
                    logger.info(f"💾 Convênio ATUALIZADO no flow_data: {convenio_anterior} → {extracted['insurance_plan']}")
                else:
                    logger.info(f"💾 Convênio salvo no flow_data: {extracted['insurance_plan']}")

                auto_response = self._trigger_auto_slot_search(context, db, phone)
                if auto_response:
                    self._record_interaction(context, message, auto_response, db, flow_modified=True)
                    return auto_response
            else:
                # NOVO: Se não encontrou via extração normal, verificar última mensagem do usuário
                # para detectar menções diretas de convênio (ex: "IPE", "CABERGS")
                if context.messages:
                    last_user_message = None
                    for msg in reversed(context.messages):
                        if msg.get("role") == "user":
                            last_user_message = msg.get("content", "").strip()
                            break
                    
                    if last_user_message:
                        detected_insurance = self._detect_insurance_in_message(last_user_message, context)
                        
                        if detected_insurance:
                            convenio_anterior = context.flow_data.get("insurance_plan")
                            
                            if convenio_anterior != detected_insurance:
                                context.flow_data["insurance_plan"] = detected_insurance
                                flag_modified(context, "flow_data")
                                db.commit()
                            
                            if convenio_anterior:
                                logger.info(f"💾 Convênio detectado na última mensagem e ATUALIZADO no flow_data: {convenio_anterior} → {detected_insurance}")
                            else:
                                logger.info(f"💾 Convênio detectado na última mensagem e salvo no flow_data: {detected_insurance}")

                            auto_response = self._trigger_auto_slot_search(context, db, phone)
                            if auto_response:
                                self._record_interaction(context, message, auto_response, db, flow_modified=True)
                                return auto_response
            
            # 8. FALLBACK: Verificar se Claude deveria ter chamado confirm_time_slot mas não chamou
            # Isso acontece quando: temos data + horário, mas não tem pending_confirmation
            # IMPORTANTE: NÃO executar se acabou de criar um agendamento com sucesso
            
            # Verificar se a última resposta do assistente indica que já criou agendamento
            should_skip_fallback = False
            
            # Verificar flag appointment_completed no flow_data
            appointment_completed_flag = context.flow_data.get("appointment_completed", False)
            if appointment_completed_flag:
                should_skip_fallback = True
                logger.info("⏭️ Pulando fallback - flag appointment_completed existe no flow_data")
            elif context.flow_data.get("pending_confirmation") is False:
                should_skip_fallback = True
                logger.info("⏭️ Pulando fallback - confirmação já resolvida (pending_confirmation=False)")
            
            # Verificar se última resposta foi erro de create_appointment
            last_assistant_msg = ""
            for msg in reversed(context.messages):
                if msg.get("role") == "assistant":
                    last_assistant_msg = msg.get("content", "")
                    break

            # Se última mensagem foi erro de validação, não executar fallback
            if "formato inválido" in last_assistant_msg.lower() or "erro ao criar" in last_assistant_msg.lower():
                should_skip_fallback = True
                logger.info("⏭️ Pulando fallback - última resposta foi erro de validação")
            
            if not should_skip_fallback and context.messages:
                last_assistant_msg = None
                for msg in reversed(context.messages):
                    if msg.get("role") == "assistant":
                        last_assistant_msg = msg.get("content", "")
                        break
                
                # Se a última mensagem contém sucesso de agendamento, pular fallback
                if last_assistant_msg and any(phrase in last_assistant_msg for phrase in [
                    "Agendamento realizado com sucesso",
                    "realizado com sucesso",
                    "agendado com sucesso"
                ]):
                    should_skip_fallback = True
                    logger.info("⏭️ Pulando fallback - agendamento já foi criado com sucesso")
            
            if (context.flow_data.get("appointment_date") and 
                context.flow_data.get("appointment_time") and 
                not context.flow_data.get("pending_confirmation") and
                not should_skip_fallback):
                
                # Validar horário antes de executar fallback
                time_str = context.flow_data["appointment_time"]
                import re
                is_valid = False
                if re.match(r'^\d{2}:\d{2}$', time_str):
                    hour, minute = time_str.split(':')
                    if minute == '00':
                        is_valid = True
                
                if not is_valid:
                    logger.warning(f"⚠️ FALLBACK bloqueado: horário inválido no flow_data ({time_str})")
                    # Limpar horário inválido
                    context.flow_data["appointment_time"] = None
                    db.commit()
                else:
                    logger.info("🔄 FALLBACK: Claude não chamou confirm_time_slot, chamando manualmente...")
                    logger.info(f"   Data: {context.flow_data['appointment_date']}")
                    logger.info(f"   Horário: {context.flow_data['appointment_time']}")
                    
                    # Chamar a tool manualmente
                    try:
                        confirmation_msg = self._handle_confirm_time_slot({
                            "date": context.flow_data["appointment_date"],
                            "time": context.flow_data["appointment_time"]
                        }, db, phone)
                        
                        # Substituir resposta do Claude pela confirmação
                        bot_response = confirmation_msg
                        logger.info("✅ Tool confirm_time_slot executada com sucesso via fallback")
                    except Exception as e:
                        logger.error(f"❌ Erro ao executar fallback de confirm_time_slot: {str(e)}")
                        # Manter resposta original do Claude
            
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
                return self._handle_get_clinic_info(tool_input, db, phone)
            elif tool_name == "validate_date_and_show_slots":
                return self._handle_validate_date_and_show_slots(tool_input, db, phone)
            elif tool_name == "confirm_time_slot":
                return self._handle_confirm_time_slot(tool_input, db, phone)
            elif tool_name == "create_appointment":
                return self._handle_create_appointment(tool_input, db, phone)
            elif tool_name == "search_appointments":
                return self._handle_search_appointments(tool_input, db)
            elif tool_name == "cancel_appointment":
                return self._handle_cancel_appointment(tool_input, db)
            elif tool_name == "find_next_available_slot":
                return self._handle_find_next_available_slot(tool_input, db, phone)
            elif tool_name == "find_alternative_slots":
                return self._handle_find_alternative_slots(tool_input, db, phone)
            elif tool_name == "request_human_assistance":
                return self._handle_request_human_assistance(tool_input, db, phone)
            elif tool_name == "extract_patient_data":
                return self._handle_extract_patient_data(tool_input, db, phone)
            elif tool_name == "request_home_address":
                return self._handle_request_home_address(tool_input, db, phone)
            elif tool_name == "notify_doctor_home_visit":
                return self._handle_notify_doctor_home_visit(tool_input, db, phone)
            elif tool_name == "end_conversation":
                return self._handle_end_conversation(tool_input, db, phone)
            
            # Tool não reconhecida
            logger.warning(f"❌ Tool não reconhecida: {tool_name}")
            return "Desculpe, ocorreu um problema técnico. Por favor, tente novamente."
        except Exception as e:
            logger.error(f"Erro ao executar tool {tool_name}: {str(e)}")
            return "Desculpe, ocorreu um erro ao processar sua solicitação. Por favor, tente novamente ou me informe o que você precisa."

    def _handle_find_next_available_slot(self, tool_input: Dict, db: Session, phone: str = None) -> str:
        """
        Tool: find_next_available_slot - Encontra automaticamente o próximo horário disponível
        respeitando 48h de antecedência mínima.
        """
        try:
            logger.info(f"🔍 Buscando próximo horário disponível para {phone}")
            
            # 1. Obter dados do contexto (flow_data)
            context = None
            if phone:
                context = db.query(ConversationContext).filter_by(phone=phone).first()
            
            # Remover flag appointment_completed ao iniciar novo agendamento
            if context and context.flow_data and context.flow_data.get("appointment_completed"):
                context.flow_data.pop("appointment_completed", None)
                flag_modified(context, "flow_data")
                db.commit()
                logger.info("🧹 Flag appointment_completed removida - novo agendamento iniciado")
            
            if not context or not context.flow_data:
                return "Para buscar o próximo horário disponível, preciso dos seus dados primeiro. Por favor, me informe seu nome completo."
            
            # Extrair dados coletados
            patient_name = context.flow_data.get("patient_name")
            consultation_type = context.flow_data.get("consultation_type", "clinica_geral")
            insurance_plan = context.flow_data.get("insurance_plan")

            if not insurance_plan or str(insurance_plan).strip().lower() == "particular":
                last_user_message = None
                if context.messages:
                    for msg in reversed(context.messages):
                        if msg.get("role") == "user":
                            last_user_message = msg.get("content", "")
                            if last_user_message:
                                break
                resolved_plan = None
                if last_user_message:
                    resolved_plan = self._detect_insurance_in_message(last_user_message, context)
                
                if not resolved_plan:
                    try:
                        extracted = self._extract_patient_data_with_claude(context)
                        resolved_plan = extracted.get("insurance_plan") if extracted else None
                    except Exception as e:
                        logger.warning(f"⚠️ Erro ao tentar extrair convênio para alternativas: {str(e)}")
                
                if resolved_plan:
                    insurance_plan = resolved_plan
                    context.flow_data["insurance_plan"] = insurance_plan
                    flag_modified(context, "flow_data")
                    db.commit()
                    logger.info(f"💾 Convênio identificado para alternativas: {insurance_plan}")

            if insurance_plan:
                normalized_plan = appointment_rules._normalize_plan(insurance_plan)
                if normalized_plan != insurance_plan:
                    context.flow_data["insurance_plan"] = normalized_plan
                    flag_modified(context, "flow_data")
                    db.commit()
                    logger.info(f"🔁 Convênio normalizado para alternativas: {insurance_plan} -> {normalized_plan}")
                insurance_plan = normalized_plan
            else:
                insurance_plan = "Particular"
            
            # SALVAMENTO AUTOMÁTICO: Se insurance_plan foi identificado por Claude mas não está no flow_data,
            # tentar extrair do histórico recente (pode ter sido mencionado na última mensagem)
            if not insurance_plan or insurance_plan == "particular":
                # Tentar extrair do histórico usando extract_patient_data
                try:
                    extracted = self._extract_patient_data_with_claude(context)
                    if extracted.get("insurance_plan"):
                        insurance_plan = extracted["insurance_plan"]
                        context.flow_data["insurance_plan"] = insurance_plan
                        flag_modified(context, "flow_data")
                        db.commit()
                        logger.info(f"💾 Convênio identificado e salvo no flow_data: {insurance_plan}")
                except Exception as e:
                    logger.warning(f"⚠️ Erro ao tentar extrair convênio: {str(e)}")
            
            # VERIFICAÇÃO AUTOMÁTICA: Se nome não estiver no flow_data, tentar extrair automaticamente
            if not patient_name:
                logger.info("⚠️ Nome não encontrado no flow_data, tentando extrair automaticamente...")
                
                # Primeiro: tentar usar _extract_appointment_data_from_messages (agora extrai nome também)
                extracted = self._extract_appointment_data_from_messages(context.messages)
                if extracted.get("patient_name"):
                    patient_name = extracted["patient_name"]
                    context.flow_data["patient_name"] = patient_name
                    db.commit()
                    logger.info(f"✅ Nome extraído automaticamente: {patient_name}")
                
                # Se ainda não encontrou, tentar usar extract_patient_data com Claude
                if not patient_name:
                    logger.info("🔍 Tentando usar extract_patient_data para extrair nome...")
                    try:
                        extracted_data = self._extract_patient_data_with_claude(context)
                        if extracted_data.get("patient_name"):
                            patient_name = extracted_data["patient_name"]
                            context.flow_data["patient_name"] = patient_name
                            db.commit()
                            logger.info(f"✅ Nome extraído via extract_patient_data: {patient_name}")
                    except Exception as e:
                        logger.warning(f"⚠️ Erro ao usar extract_patient_data: {str(e)}")
            
            if not patient_name:
                return "Para continuar com o agendamento, preciso do seu nome completo. Pode me informar?"
            
            # 2. Calcular data mínima (48h)
            minimum_datetime = get_minimum_appointment_datetime()
            logger.info(f"📅 Data/hora mínima: {minimum_datetime}")
            
            # 3. Buscar primeiro dia útil após data mínima
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
            dias_fechados = self.clinic_info.get('dias_fechados', [])
            
            # Começar a buscar a partir da data mínima
            current_date = minimum_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
            max_days_ahead = 90  # Limite de busca (90 dias)
            days_checked = 0
            
            first_slot = None
            found_date = None
            
            while days_checked < max_days_ahead:
                # Verificar se é dia útil (não domingo e não está em dias_fechados)
                weekday = current_date.weekday()
                
                # Pular domingo
                if weekday == 6:
                    current_date += timedelta(days=1)
                    days_checked += 1
                    continue
                
                # Verificar se está em dias_fechados ou em período especial de férias
                date_str_formatted = current_date.strftime('%d/%m/%Y')
                if date_str_formatted in dias_fechados or self._is_special_holiday_date(current_date):
                    current_date += timedelta(days=1)
                    days_checked += 1
                    continue

                allowed, reason = appointment_rules.is_plan_allowed_on_date(current_date, insurance_plan)
                if not allowed:
                    logger.info(f"⏭️ Alternativa pulada em {current_date.strftime('%d/%m/%Y')} - {reason}")
                    current_date += timedelta(days=1)
                    days_checked += 1
                    continue

                capacity_ok, capacity_reason = appointment_rules.has_capacity_for_insurance(current_date, insurance_plan, db)
                if not capacity_ok:
                    logger.info(f"⏭️ Alternativa pulada em {current_date.strftime('%d/%m/%Y')} - {capacity_reason}")
                    current_date += timedelta(days=1)
                    days_checked += 1
                    continue
                
                # Verificar regras específicas de convênio para o dia
                allowed, reason = appointment_rules.is_plan_allowed_on_date(current_date, insurance_plan)
                if not allowed:
                    logger.info(f"⏭️ Pulando {current_date.strftime('%d/%m/%Y')} - {reason}")
                    current_date += timedelta(days=1)
                    days_checked += 1
                    continue
                
                capacity_ok, capacity_reason = appointment_rules.has_capacity_for_insurance(current_date, insurance_plan, db)
                if not capacity_ok:
                    logger.info(f"⏭️ Pulando {current_date.strftime('%d/%m/%Y')} - {capacity_reason}")
                    current_date += timedelta(days=1)
                    days_checked += 1
                    continue
                
                # Verificar se funciona nesse dia
                dias_semana_pt = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
                dia_nome = dias_semana_pt[weekday]
                horarios = self.clinic_info.get('horario_funcionamento', {})
                horario_dia = horarios.get(dia_nome, "FECHADO")
                
                if horario_dia == "FECHADO":
                    current_date += timedelta(days=1)
                    days_checked += 1
                    continue
                
                # Preparar data base para buscar slots (usar primeiro horário do dia)
                inicio_str, _ = horario_dia.split('-')
                inicio_h, inicio_m = map(int, inicio_str.split(':'))
                temp_date = current_date.replace(hour=inicio_h, minute=inicio_m, second=0, microsecond=0)
                
                # Determinar se deve usar start_from_time baseado na data mínima
                # Se estiver no mesmo dia da data mínima, usar minimum_datetime como start_from_time
                # Caso contrário, não filtrar (buscar desde o primeiro horário do dia)
                start_from_time = None
                if current_date.date() == minimum_datetime.date():
                    # Mesmo dia - usar minimum_datetime como limite mínimo
                    start_from_time = minimum_datetime
                
                # Buscar primeiro slot disponível deste dia respeitando 48h
                try:
                    first_slot = appointment_rules._find_first_available_slot_in_day(
                        temp_date, duracao, db, start_from_time=start_from_time, insurance_plan=insurance_plan
                    )
                    
                    # Se encontrou slot, usar (já está garantido que é >= minimum_datetime se start_from_time foi passado)
                    if first_slot:
                        # Garantir timezone-aware para comparação final
                        if first_slot.tzinfo is None:
                            tz = get_brazil_timezone()
                            first_slot = tz.localize(first_slot)
                        
                        # Verificação adicional de segurança (mesmo que start_from_time já tenha filtrado)
                        if first_slot >= minimum_datetime:
                            found_date = current_date
                            break
                except TypeError as e:
                    # Erro específico de timezone: "can't compare offset-naive and offset-aware datetimes"
                    if "timezone" in str(e).lower() or "offset" in str(e).lower():
                        logger.error(f"⚠️ Erro de timezone ao buscar slots: {str(e)}")
                        logger.error(f"   Tentando normalizar timezones...")
                        # Tentar recuperação: normalizar temp_date antes de tentar novamente
                        try:
                            # Remover timezone de temp_date se presente
                            if temp_date.tzinfo is not None:
                                temp_date = temp_date.replace(tzinfo=None)
                            # Tentar novamente
                            first_slot = appointment_rules._find_first_available_slot_in_day(
                                temp_date, duracao, db, start_from_time=start_from_time, insurance_plan=insurance_plan
                            )
                            if first_slot:
                                if first_slot.tzinfo is None:
                                    tz = get_brazil_timezone()
                                    first_slot = tz.localize(first_slot)
                                if first_slot >= minimum_datetime:
                                    found_date = current_date
                                    break
                        except Exception as e2:
                            logger.error(f"❌ Erro ao tentar recuperação de timezone: {str(e2)}")
                            # Continuar para próximo dia
                            pass
                    else:
                        # Re-raise se não for erro de timezone
                        raise
                
                # Próximo dia
                current_date += timedelta(days=1)
                days_checked += 1
            
            if not first_slot or not found_date:
                return "❌ Não encontrei horários disponíveis nos próximos 30 dias. Por favor, entre em contato conosco para verificar outras opções."
            
            # 4. Salvar dados no flow_data para confirmação
            if context:
                if not context.flow_data:
                    context.flow_data = {}
                context.flow_data["appointment_date"] = format_date_br(found_date)
                context.flow_data["appointment_time"] = first_slot.strftime('%H:%M')
                context.flow_data["pending_confirmation"] = True
                context.flow_data["alternatives_offered"] = False
                db.commit()
                logger.info(f"💾 Dados salvos no flow_data para confirmação")
            
            # 5. Montar resumo formatado
            tipo_map = {
                "clinica_geral": "Clínica Geral",
                "geriatria": "Geriatria Clínica e Preventiva",
                "domiciliar": "Atendimento Domiciliar ao Paciente Idoso"
            }
            tipo_nome = tipo_map.get(consultation_type, "Clínica Geral")
            
            tipos_consulta = self.clinic_info.get('tipos_consulta', {})
            tipo_data = tipos_consulta.get(consultation_type, {})
            tipo_valor = tipo_data.get('valor', 0)
            
            if not insurance_plan or insurance_plan.lower() in {"particular", "particula"}:
                convenio_nome = "Particular"
            else:
                convenio_nome = insurance_plan.upper()
            
            dias_semana = ['segunda-feira', 'terça-feira', 'quarta-feira', 
                          'quinta-feira', 'sexta-feira', 'sábado', 'domingo']
            dia_nome_completo = dias_semana[found_date.weekday()]
            
            # Validar first_slot antes de formatar
            if not first_slot:
                logger.error(f"❌ first_slot é None ou inválido")
                return "❌ Erro ao buscar horário disponível. Por favor, tente novamente."
            
            # Verificar se first_slot é datetime válido
            if not isinstance(first_slot, datetime):
                logger.error(f"❌ first_slot não é datetime: {type(first_slot)}")
                return "❌ Erro ao buscar horário disponível. Por favor, tente novamente."
            
            # Formatar horário com validação
            try:
                horario_str = first_slot.strftime('%H:%M')
                logger.info(f"✅ Horário formatado: {horario_str}")
            except Exception as e:
                logger.error(f"❌ Erro ao formatar horário: {str(e)}")
                horario_str = "N/A"
            
            response = f"✅ Encontrei o próximo horário disponível para você!\n\n"
            response += f"📋 *Resumo da consulta:*\n"
            response += f"👤 Nome: {patient_name}\n"
            response += f"🏥 Tipo: {tipo_nome} - R$ {tipo_valor}\n"
            response += f"💳 Convênio: {convenio_nome}\n"
            response += f"📅 Data: {format_date_br(found_date)} ({dia_nome_completo})\n"
            response += f"⏰ Horário: {horario_str}\n"
            response += "\nPosso confirmar o agendamento?"
            
            return response
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ Erro ao buscar próximo horário disponível: {error_msg}", exc_info=True)
            
            # Mensagens específicas para erros conhecidos
            if "timezone" in error_msg.lower() or "offset" in error_msg.lower():
                logger.error("⚠️ Erro de timezone detectado. Isso pode indicar problema na normalização de datetimes.")
                return "Desculpe, ocorreu um problema técnico ao buscar horários disponíveis. Por favor, tente novamente ou entre em contato conosco."
            else:
                logger.error(f"❌ Erro inesperado: {error_msg}")
                return "Desculpe, ocorreu um erro ao processar sua solicitação. Por favor, tente novamente ou me informe o que você precisa."

    def _handle_find_alternative_slots(self, tool_input: Dict, db: Session, phone: str = None) -> str:
        """
        Tool: find_alternative_slots - Encontra 3 opções alternativas de agendamento
        (primeiro horário disponível de 3 dias diferentes) respeitando 48h de antecedência mínima.
        """
        try:
            logger.info(f"🔍 Buscando 3 alternativas de horários para {phone}")
            
            # 1. Obter dados do contexto
            context = None
            if phone:
                context = db.query(ConversationContext).filter_by(phone=phone).first()
            
            if not context or not context.flow_data:
                return "Para buscar o próximo horário disponível, preciso dos seus dados primeiro. Por favor, me informe seu nome completo."
            
            # Extrair dados coletados
            patient_name = context.flow_data.get("patient_name")
            consultation_type = context.flow_data.get("consultation_type", "clinica_geral")
            insurance_plan = context.flow_data.get("insurance_plan")
            
            if not insurance_plan or str(insurance_plan).strip().lower() == "particular":
                last_user_message = None
                if context.messages:
                    for msg in reversed(context.messages):
                        if msg.get("role") == "user":
                            last_user_message = msg.get("content", "")
                            if last_user_message:
                                break
                resolved_plan = None
                if last_user_message:
                    resolved_plan = self._detect_insurance_in_message(last_user_message, context)
                
                if not resolved_plan:
                    try:
                        extracted = self._extract_patient_data_with_claude(context)
                        resolved_plan = extracted.get("insurance_plan") if extracted else None
                    except Exception as e:
                        logger.warning(f"⚠️ Erro ao tentar extrair convênio para alternativas: {str(e)}")
                
                if resolved_plan:
                    insurance_plan = resolved_plan
                    context.flow_data["insurance_plan"] = insurance_plan
                    flag_modified(context, "flow_data")
                    db.commit()
                    logger.info(f"💾 Convênio atualizado para alternativas: {insurance_plan}")
            
            if insurance_plan:
                normalized_plan = appointment_rules._normalize_plan(insurance_plan)
                if normalized_plan != insurance_plan:
                    context.flow_data["insurance_plan"] = normalized_plan
                    flag_modified(context, "flow_data")
                    db.commit()
                    logger.info(f"🔁 Convênio normalizado para alternativas: {insurance_plan} -> {normalized_plan}")
                insurance_plan = normalized_plan
            else:
                insurance_plan = "Particular"
            
            if not patient_name:
                return "Para continuar com o agendamento, preciso do seu nome completo. Pode me informar?"
            
            # 2. Calcular data mínima (48h)
            minimum_datetime = get_minimum_appointment_datetime()
            
            # 3. Buscar 3 dias úteis diferentes após data mínima
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
            dias_fechados = self.clinic_info.get('dias_fechados', [])
            
            current_date = minimum_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
            max_days_ahead = 90
            days_checked = 0
            
            alternatives = []  # Lista de (datetime, date) - (slot, data)
            
            while len(alternatives) < 3 and days_checked < max_days_ahead:
                # Verificar se é dia útil
                weekday = current_date.weekday()
                
                # Pular domingo
                if weekday == 6:
                    current_date += timedelta(days=1)
                    days_checked += 1
                    continue
                
                # Verificar se está em dias_fechados ou período especial
                date_str_formatted = current_date.strftime('%d/%m/%Y')
                if date_str_formatted in dias_fechados or self._is_special_holiday_date(current_date):
                    current_date += timedelta(days=1)
                    days_checked += 1
                    continue
                
                # Verificar se funciona nesse dia
                dias_semana_pt = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
                dia_nome = dias_semana_pt[weekday]
                horarios = self.clinic_info.get('horario_funcionamento', {})
                horario_dia = horarios.get(dia_nome, "FECHADO")
                
                if horario_dia == "FECHADO":
                    current_date += timedelta(days=1)
                    days_checked += 1
                    continue
                
                # Preparar data base para buscar slots (usar primeiro horário do dia)
                inicio_str, _ = horario_dia.split('-')
                inicio_h, inicio_m = map(int, inicio_str.split(':'))
                temp_date = current_date.replace(hour=inicio_h, minute=inicio_m, second=0, microsecond=0)
                
                # Determinar se deve usar start_from_time baseado na data mínima
                # Se estiver no mesmo dia da data mínima, usar minimum_datetime como start_from_time
                # Caso contrário, não filtrar (buscar desde o primeiro horário do dia)
                start_from_time = None
                if current_date.date() == minimum_datetime.date():
                    # Mesmo dia - usar minimum_datetime como limite mínimo
                    start_from_time = minimum_datetime
                
                # Buscar primeiro slot disponível deste dia respeitando 48h
                first_slot = appointment_rules._find_first_available_slot_in_day(
                    temp_date, duracao, db, start_from_time=start_from_time, insurance_plan=insurance_plan
                )
                
                # Se encontrou slot, adicionar às alternativas (já está garantido que é >= minimum_datetime se start_from_time foi passado)
                if first_slot:
                    # Garantir timezone-aware para comparação final
                    if first_slot.tzinfo is None:
                        tz = get_brazil_timezone()
                        first_slot = tz.localize(first_slot)
                    
                    # Verificação adicional de segurança (mesmo que start_from_time já tenha filtrado)
                    if first_slot >= minimum_datetime:
                        alternatives.append((first_slot, current_date))
                        logger.info(f"✅ Alternativa {len(alternatives)}: {format_date_br(current_date)} às {first_slot.strftime('%H:%M')}")
                
                # Próximo dia
                current_date += timedelta(days=1)
                days_checked += 1
            
            if len(alternatives) == 0:
                return "❌ Não encontrei horários disponíveis nos próximos 30 dias. Por favor, entre em contato conosco."
            
            # 4. Salvar alternativas no flow_data para facilitar escolha do usuário
            if context:
                if not context.flow_data:
                    context.flow_data = {}
                context.flow_data["alternative_slots"] = [
                    {
                        "date": format_date_br(alt_date),
                        "time": slot.strftime('%H:%M'),
                        "datetime": slot.isoformat() if slot.tzinfo else slot.replace(tzinfo=get_brazil_timezone()).isoformat()
                    }
                    for slot, alt_date in alternatives
                ]
                db.commit()
                logger.info(f"💾 Alternativas salvas no flow_data: {len(alternatives)} opções")
            
            # 5. Montar resposta formatada com as 3 alternativas
            tipo_map = {
                "clinica_geral": "Clínica Geral",
                "geriatria": "Geriatria Clínica e Preventiva",
                "domiciliar": "Atendimento Domiciliar ao Paciente Idoso"
            }
            tipo_nome = tipo_map.get(consultation_type, "Clínica Geral")
            
            tipos_consulta = self.clinic_info.get('tipos_consulta', {})
            tipo_data = tipos_consulta.get(consultation_type, {})
            tipo_valor = tipo_data.get('valor', 0)
            
            convenio_nome = insurance_plan if insurance_plan != "particular" else "Particular"
            
            dias_semana = ['segunda-feira', 'terça-feira', 'quarta-feira', 
                          'quinta-feira', 'sexta-feira', 'sábado', 'domingo']
            
            response = f"✅ Encontrei {len(alternatives)} opção(ões) alternativa(s) para você:\n\n"
            
            for i, (slot, alt_date) in enumerate(alternatives, 1):
                dia_nome_completo = dias_semana[alt_date.weekday()]
                response += f"**Opção {i}:**\n"
                response += f"📅 {format_date_br(alt_date)} ({dia_nome_completo})\n"
                response += f"⏰ Horário: {slot.strftime('%H:%M')}\n\n"
            
            response += f"📋 *Resumo:*\n"
            response += f"👤 Nome: {patient_name}\n"
            response += f"🏥 Tipo: {tipo_nome} - R$ {tipo_valor}\n"
            response += f"💳 Convênio: {convenio_nome}\n\n"
            response += "Se nenhum desses horários funcionar, me indique uma data no formato DD/MM/AAAA ou descreva o período que prefere 😉\n\n"
            response += f"Qual opção você prefere? Digite o número (1, 2 ou 3) ou me diga se prefere outra data/horário."
            
            return response
            
        except Exception as e:
            logger.error(f"Erro ao buscar alternativas: {str(e)}", exc_info=True)
            return f"Erro ao buscar alternativas: {str(e)}"

    def _format_clinic_hours(self) -> str:
        """Formata os horários de funcionamento."""
        horarios = self.clinic_info.get('horario_funcionamento', {})
        dias_ordenados = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
        lines = []
        for dia in dias_ordenados:
            if dia in horarios:
                horario = horarios[dia]
                dia_formatado = dia.replace('terca', 'terça').replace('sabado', 'sábado')
                if horario != "FECHADO":
                    lines.append(f"• {dia_formatado.capitalize()}: {horario}")
                else:
                    lines.append(f"• {dia_formatado.capitalize()}: FECHADO")
        return "\n".join(lines)

    def _format_closed_days(self) -> str:
        """Formata os dias especiais fechados."""
        dias_fechados = self.clinic_info.get('dias_fechados', [])
        if not dias_fechados:
            return "Nenhum dia especial fechado informado."
        return "\n".join(f"• {dia}" for dia in dias_fechados)

    def _format_consultation_prices(self) -> str:
        tipos_consulta = self.clinic_info.get('tipos_consulta', {})
        if not tipos_consulta:
            return "Não há valores cadastrados no momento."
        lines = []
        for key, data in tipos_consulta.items():
            nome = data.get("nome", key.replace("_", " ").title())
            valor = data.get("valor", "Sob consulta")
            lines.append(f"• {nome}: R$ {valor:.2f}" if isinstance(valor, (int, float)) else f"• {nome}: {valor}")
        return "\n".join(lines)

    def _format_insurance_list(self) -> str:
        convenios = self.clinic_info.get('convenios_aceitos', {})
        if not convenios:
            return "Atendemos apenas consultas particulares no momento."
        linhas = []
        for _, dados in convenios.items():
            nome = dados.get("nome") or dados.get("codigo")
            if nome:
                linhas.append(f"• {nome}")
        return "\n".join(linhas) if linhas else "Convênios não informados."

    def _infer_clinic_info_intent(self, question: Optional[str]) -> Optional[str]:
        """Tenta identificar o tipo de informação de clínica solicitado pelo usuário."""
        if not question:
            return None

        normalized = unicodedata.normalize("NFD", question)
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn").lower()

        intent_keywords = {
            "prices": [
                "valor", "preco", "preços", "quanto custa", "custa", "custam", "valores",
                "preço", "cobram", "cobranca"
            ],
            "hours": [
                "horario", "horário", "funciona", "funcionamento", "que horas", "ate que horas",
                "abre", "fecha", "horas", "qual horario", "quando atende"
            ],
            "address": [
                "endereco", "endereço", "onde fica", "localizacao", "localização", "onde é",
                "como chegar", "mapa", "local", "ficam situados"
            ],
            "phones": [
                "telefone", "contato", "numero", "número", "whatsapp", "celular", "ligar",
                "falar com vcs"
            ],
            "insurances": [
                "convenio", "convênio", "planos", "plano", "aceita", "ipe", "cabergs",
                "particular", "unimed"
            ],
            "closed_days": [
                "feriado", "feriados", "ferias", "férias", "recesso", "dias fechados",
                "quando nao atende", "quando não atende", "dia fechado"
            ],
            "practice_locations": [
                "só no consultorio", "so no consultorio", "apenas no consultorio",
                "consultório apenas", "consulta presencial", "atende em casa",
                "domicilio", "domicílio", "visita domiciliar", "home care",
                "vai até", "vem até", "atende fora", "vai em casa", "vem em casa"
            ],
            "overview": [
                "tudo", "informacoes gerais", "informações gerais", "informacao completa",
                "informações completas", "sobre a clinica", "sobre a clínica", "fale da clinica",
                "detalhes da clinica"
            ],
        }

        matched = {intent for intent, keywords in intent_keywords.items() if any(word in normalized for word in keywords)}

        if not matched:
            return None

        if matched == {"overview"}:
            return "overview"

        matched.discard("overview")

        if len(matched) == 1:
            return matched.pop()

        return None

    def _handle_get_clinic_info(self, tool_input: Dict, db: Session, phone: Optional[str]) -> str:
        """Tool: get_clinic_info - Retorna informações da clínica conforme a intenção solicitada."""
        try:
            intent = (tool_input or {}).get("type") if isinstance(tool_input, dict) else None
            intent = (intent or "").lower()
            user_question = ""

            if isinstance(tool_input, dict):
                for key in ("question", "query", "prompt", "user_input", "original_text"):
                    if tool_input.get(key):
                        user_question = str(tool_input[key]).strip()
                        break

            if not user_question and db and phone:
                context = db.query(ConversationContext).filter_by(phone=phone).first()
                if context:
                    for message in reversed(context.messages or []):
                        if message.get("role") == "user":
                            user_question = (message.get("content") or "").strip()
                            if user_question:
                                break

            if intent not in {"prices", "hours", "address", "phones", "insurances", "closed_days", "overview"}:
                intent = ""

            inferred_intent = None
            if not intent or intent == "overview":
                inferred_intent = self._infer_clinic_info_intent(user_question)
                if inferred_intent and inferred_intent != "overview":
                    logger.info(
                        f"🎯 Ajustando chamada get_clinic_info para '{inferred_intent}' "
                        f"(pergunta: {user_question!r})"
                    )
                    intent = inferred_intent
                elif not intent:
                    intent = "overview"

            nome_clinica = self.clinic_info.get('nome_clinica', 'Clínica')
            endereco = self.clinic_info.get('endereco', 'Não informado')
            telefone = self.clinic_info.get('telefone', 'Não informado')

            if intent == "address":
                return (
                    f"🏥 {nome_clinica}\n"
                    f"📍 Endereço:\n{endereco}\n"
                    f"📞 Telefone:\n{telefone}"
                )

            if intent == "hours":
                return (
                    f"🕒 Horários de funcionamento:\n{self._format_clinic_hours()}"
                )

            if intent == "phones":
                telefone_principal = telefone
                telefones_extra = self.clinic_info.get("informacoes_adicionais", {}).get("telefones_secundarios", [])
                linhas = []
                if telefone_principal and telefone_principal.lower() != "não informado":
                    linhas.append(f"• Principal: {telefone_principal}")
                for idx, tel in enumerate(telefones_extra, start=1):
                    linhas.append(f"• Secundário {idx}: {tel}")
                if not linhas:
                    linhas.append("• Não temos telefone disponível no momento.")
                return "📞 Telefones para contato:\n" + "\n".join(linhas)

            if intent == "closed_days":
                return (
                    "🚫 Dias especiais em que estaremos fechados:\n"
                    f"{self._format_closed_days()}"
                )

            if intent == "prices":
                return (
                    "💰 Valores das consultas:\n"
                    f"{self._format_consultation_prices()}"
                )

            if intent == "insurances":
                return (
                    "💳 Convênios atendidos:\n"
                    f"{self._format_insurance_list()}"
                )

            if intent == "practice_locations":
                atendimento_domiciliar = self.clinic_info.get("informacoes_adicionais", {}).get("atendimento_domiciliar", False)
                if atendimento_domiciliar:
                    return (
                        "👩‍⚕️ Atendemos no consultório e também oferecemos atendimento domiciliar para casos específicos. "
                        "Podemos conversar sobre a disponibilidade caso você precise."
                    )
                return "👩‍⚕️ Atendemos apenas no consultório da doutora no momento."

            # Overview (ou fallback genérico)
            if intent == "overview" and user_question and not inferred_intent:
                return (
                    "Posso te ajudar com informações como horários, valores, endereço, convênios ou atendimento domiciliar. "
                    "Sobre o que exatamente você gostaria de saber?"
                )

            resposta = [
                f"🏥 {nome_clinica}",
                "",
                "📍 **Endereço**",
                endereco,
                "",
                "📞 **Telefone**",
                telefone,
                "",
                "🕒 **Horários de funcionamento**",
                self._format_clinic_hours()
            ]

            dias_fechados = self.clinic_info.get('dias_fechados', [])
            if dias_fechados:
                resposta.extend([
                    "",
                    "🚫 **Dias especiais sem atendimento**",
                    self._format_closed_days()
                ])

            info_pagamento = self.clinic_info.get("informacoes_adicionais", {}).get("formas_pagamento")
            if info_pagamento:
                resposta.extend([
                    "",
                    "💳 **Formas de pagamento**",
                    "\n".join(f"• {forma}" for forma in info_pagamento)
                ])

            convenios = self._format_insurance_list()
            if convenios and "Convênios não informados." not in convenios:
                resposta.extend([
                    "",
                    "💳 **Convênios atendidos**",
                    convenios
                ])

            return "\n".join(resposta)
            
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
            
            # Verificar se está em dias_fechados
            dias_fechados = self.clinic_info.get('dias_fechados', [])
            if date_str in dias_fechados:
                return f"❌ A clínica estará fechada em {date_str} por motivo especial."
            
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
    
    def _handle_validate_and_check_availability(self, tool_input: Dict, db: Session, phone: str = None) -> str:
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
                # Garantir que time_str é string
                if not isinstance(time_str, str):
                    logger.error(f"❌ time_str não é string: {type(time_str)} - {time_str}")
                    time_str = str(time_str)
                
                hora_consulta_original = datetime.strptime(time_str, '%H:%M').time()
                hora_inicio, hora_fim = horario_dia.split('-')
                
                # Garantir que são strings antes de fazer strptime
                if not isinstance(hora_inicio, str):
                    logger.error(f"❌ hora_inicio não é string: {type(hora_inicio)}")
                    hora_inicio = str(hora_inicio)
                if not isinstance(hora_fim, str):
                    logger.error(f"❌ hora_fim não é string: {type(hora_fim)}")
                    hora_fim = str(hora_fim)
                
                hora_inicio = datetime.strptime(hora_inicio.strip(), '%H:%M').time()
                hora_fim = datetime.strptime(hora_fim.strip(), '%H:%M').time()
                
                # Arredondar minuto para cima ao próximo múltiplo de 5
                appointment_datetime_tmp = datetime.combine(appointment_date.date(), hora_consulta_original).replace(tzinfo=None)
                hora_consulta_dt = round_up_to_next_5_minutes(appointment_datetime_tmp)
                hora_consulta = hora_consulta_dt.time()
                
                if not (hora_inicio <= hora_consulta <= hora_fim):
                    logger.warning(f"❌ Horário {time_str} fora do funcionamento")
                    return f"❌ Horário inválido! A clínica funciona das {hora_inicio.strftime('%H:%M')} às {hora_fim.strftime('%H:%M')} aos {weekday_pt}s.\n" + \
                           f"Por favor, escolha um horário entre {hora_inicio.strftime('%H:%M')} e {hora_fim.strftime('%H:%M')}."
                           
            except ValueError as ve:
                logger.error(f"❌ ValueError ao processar horário: {str(ve)}")
                logger.error(f"   time_str={time_str} (type: {type(time_str)})")
                logger.error(f"   horario_dia={horario_dia}")
                return "Formato de horário inválido. Use HH:MM (ex: 14:30)."
            except Exception as e:
                logger.error(f"❌ Erro inesperado ao processar horário: {str(e)}", exc_info=True)
                logger.warning(f"❌ Formato de horário inválido: {time_str}")
                return "Formato de horário inválido. Use HH:MM (ex: 14:30)."
            
            # 5. Verificar disponibilidade no banco de dados
            appointment_datetime = datetime.combine(appointment_date.date(), hora_consulta).replace(tzinfo=None)
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
            
            # Usar nova função para verificar disponibilidade
            is_available = appointment_rules.check_slot_availability(appointment_datetime, duracao, db)
            
            if is_available:
                ajuste_msg = ""
                if hora_consulta.strftime('%H:%M') != time_str:
                    ajuste_msg = f" (ajustado para {hora_consulta.strftime('%H:%M')})"
                logger.info(f"✅ Horário {hora_consulta.strftime('%H:%M')} disponível!{ajuste_msg}")
                
                # Salvar dados no flow_data para confirmação
                # Buscar contexto do usuário atual usando phone recebido
                context = None
                if phone:
                    context = db.query(ConversationContext).filter_by(phone=phone).first()
                    if context:
                        # CRÍTICO: Não sobrescrever dados já salvos no flow_data
                        if not context.flow_data:
                            context.flow_data = {}
                        
                        # Atualizar APENAS campos vazios (não sobrescrever)
                        nome_atual = context.flow_data.get("patient_name")
                        logger.info(f"🔍 DEBUG: Nome atual no flow_data: {nome_atual}")
                        
                        if not nome_atual:
                            logger.info(f"🔍 DEBUG: Nome está vazio, extraindo do histórico")
                            extracted = self._extract_appointment_data_from_messages(context.messages)
                            if extracted.get("patient_name"):
                                logger.info(f"🔍 DEBUG: Nome extraído: {extracted.get('patient_name')}")
                                context.flow_data["patient_name"] = extracted.get("patient_name")
                        else:
                            logger.info(f"🔍 DEBUG: Nome já existe ({nome_atual}), NÃO sobrescrevendo")
                        
                        if not context.flow_data.get("patient_birth_date"):
                            if 'extracted' not in locals():
                                extracted = self._extract_appointment_data_from_messages(context.messages)
                            if extracted.get("patient_birth_date"):
                                context.flow_data["patient_birth_date"] = extracted.get("patient_birth_date")
                        
                        if not context.flow_data.get("consultation_type"):
                            if 'extracted' not in locals():
                                extracted = self._extract_appointment_data_from_messages(context.messages)
                            if extracted.get("consultation_type"):
                                context.flow_data["consultation_type"] = extracted.get("consultation_type")
                        
                        if not context.flow_data.get("insurance_plan"):
                            if 'extracted' not in locals():
                                extracted = self._extract_appointment_data_from_messages(context.messages)
                            if extracted.get("insurance_plan"):
                                context.flow_data["insurance_plan"] = extracted.get("insurance_plan")
                        
                        # Sempre atualizar data/hora da consulta (podem mudar)
                        context.flow_data["appointment_date"] = date_str
                        context.flow_data["appointment_time"] = hora_consulta.strftime('%H:%M')
                        context.flow_data["pending_confirmation"] = True
                        
                        db.commit()
                        logger.info(f"💾 Dados salvos no flow_data para confirmação: {context.flow_data}")
                
                # Buscar tipo, convênio e nome do flow_data se disponível
                tipo_info = ""
                patient_name = ""
                if context and context.flow_data:
                    # Nome do paciente
                    nome = context.flow_data.get("patient_name")
                    if nome:
                        patient_name = f"👤 Paciente: {nome}\n"
                    
                    # Tipo de consulta
                    tipo = context.flow_data.get("consultation_type")
                    convenio = context.flow_data.get("insurance_plan")
                    
                    if tipo:
                        tipos_consulta = self.clinic_info.get('tipos_consulta', {})
                        tipo_data = tipos_consulta.get(tipo, {})
                        tipo_nome = tipo_data.get('nome', '')
                        tipo_valor = tipo_data.get('valor', 0)
                        tipo_info = f"💼 Tipo: {tipo_nome}\n💰 Valor: R$ {tipo_valor}\n"
                    
                    if convenio:
                        convenios_aceitos = self.clinic_info.get('convenios_aceitos', {})
                        convenio_data = convenios_aceitos.get(convenio, {})
                        convenio_nome = convenio_data.get('nome', '')
                        tipo_info += f"💳 Convênio: {convenio_nome}\n"
                
                # Retornar mensagem de confirmação
                return f"✅ Horário {hora_consulta.strftime('%H:%M')} disponível!{ajuste_msg}\n\n" \
                       f"📋 *Resumo da sua consulta:*\n" \
                       f"{patient_name}" \
                       f"{tipo_info}" \
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

            if self._is_special_holiday_date(appointment_date):
                logger.info(f"⛱️ check_availability detectou período de férias em {date_str} - encaminhando secretaria.")
                return self._handoff_due_to_holiday(db, phone=None)
            
            # Obter horários disponíveis
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 45)
            logger.info(f"⏱️ Duração da consulta: {duracao} minutos")
            
            insurance_plan = tool_input.get("insurance_plan", "Particular") if isinstance(tool_input, dict) else "Particular"
            
            available_slots = appointment_rules.get_available_slots(
                appointment_date,
                duracao,
                db,
                insurance_plan=insurance_plan
            )
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

    def _handle_validate_date_and_show_slots(self, tool_input: Dict, db: Session, phone: str = None) -> str:
        """
        Valida data e mostra horários disponíveis automaticamente.
        Combina validação + listagem em uma única etapa.
        """
        try:
            context: Optional[ConversationContext] = None
            insurance_plan = "Particular"
            # Limpar flag appointment_completed ao iniciar novo agendamento
            if phone:
                context = db.query(ConversationContext).filter_by(phone=phone).first()
                if context and context.flow_data:
                    if context.flow_data.get("appointment_completed"):
                        context.flow_data.pop("appointment_completed", None)
                        flag_modified(context, "flow_data")
                        db.commit()
                        logger.info("🧹 Flag appointment_completed removida - novo agendamento iniciado")
                    insurance_plan = context.flow_data.get("insurance_plan", insurance_plan)
                    if context.flow_data.pop("awaiting_custom_date", None):
                        flag_modified(context, "flow_data")
                        db.commit()
                        logger.info("🧹 awaiting_custom_date removido após nova data fornecida")
            
            date_str = tool_input.get("date")
            
            if not date_str:
                return "Para continuar, preciso da data da consulta. Por favor, informe no formato DD/MM/AAAA (exemplo: 15/01/2024)."
            
            # Validar data
            appointment_date = parse_date_br(date_str)
            if not appointment_date:
                return f"O formato da data '{date_str}' não está correto. Por favor, use o formato DD/MM/AAAA (exemplo: 15/01/2024)."
            
            logger.info(f"📅 Validando data e buscando slots: {date_str}")
            
            if self._is_special_holiday_date(appointment_date):
                logger.info(f"⛱️ Data solicitada {date_str} está em período de férias - encaminhando secretaria.")
                return self._handoff_due_to_holiday(db, phone)
            
            # ========== VALIDAÇÃO 0: DATA MÍNIMA (48 HORAS) ==========
            minimum_datetime = get_minimum_appointment_datetime()
            minimum_date = minimum_datetime.date()

            if appointment_date.date() < minimum_date:
                if tool_input.get("auto_adjust_to_future"):
                    logger.info(
                        "🔁 Data %s está antes do mínimo de 48 horas; ajustando automaticamente.",
                        date_str
                    )
                    while appointment_date.date() < minimum_date:
                        appointment_date += timedelta(days=7)
                    date_str = appointment_date.strftime('%d/%m/%Y')
                    logger.info("🔁 Nova data ajustada: %s", date_str)
                else:
                    next_available = minimum_datetime
                    horarios = self.clinic_info.get('horario_funcionamento', {})
                    dias_semana_pt = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']

                    while True:
                        nome_dia = dias_semana_pt[next_available.weekday()]
                        horario_dia = horarios.get(nome_dia, "FECHADO")
                        if horario_dia != "FECHADO":
                            break
                        next_available += timedelta(days=1)

                    return (
                        "❌ A data informada já passou ou não atende nossa regra de antecedência mínima de 48 horas.\n"
                        f"A partir de agora, a primeira data disponível é {next_available.strftime('%d/%m/%Y')}.\n"
                        "Pode me informar uma nova data por favor?"
                    )

            if self._is_special_holiday_date(appointment_date):
                logger.info(f"⛱️ Data ajustada {appointment_date.strftime('%d/%m/%Y')} está em período de férias - encaminhando secretaria.")
                return self._handoff_due_to_holiday(db, phone)

            # ========== VALIDAÇÃO DE CONVÊNIO (SEGUNDA-FEIRA / LIMITE IPE) ==========
            allowed_plan, reason_plan = appointment_rules.is_plan_allowed_on_date(appointment_date, insurance_plan)
            if not allowed_plan:
                return f"❌ {reason_plan}\nPor favor, escolha outra data."

            capacity_ok, capacity_message = appointment_rules.has_capacity_for_insurance(appointment_date, insurance_plan, db)
            if not capacity_ok:
                return f"❌ {capacity_message}\nPoderia escolher outra data, por favor?"
            
            # ========== VALIDAÇÃO 1: DIA DA SEMANA ==========
            weekday = appointment_date.weekday()  # 0=segunda, 6=domingo
            dias_semana_pt = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
            dia_nome = dias_semana_pt[weekday]
            
            # Verificar se funciona nesse dia
            horarios = self.clinic_info.get('horario_funcionamento', {})
            horario_dia = horarios.get(dia_nome, "FECHADO")
            
            if horario_dia == "FECHADO":
                # Montar mensagem de erro completa
                msg = f"❌ O dia {date_str} é {dia_nome.upper()} e a clínica não atende neste dia.\n\n"
                msg += "📅 Horários de funcionamento:\n"
                for dia, horario in horarios.items():
                    if horario != "FECHADO":
                        msg += f"• {dia.capitalize()}: {horario}\n"
                
                # Adicionar dias especiais
                dias_fechados = self.clinic_info.get('dias_fechados', [])
                if dias_fechados:
                    msg += "\n🚫 Dias especiais (férias/feriados):\n"
                    msg += format_closed_days(dias_fechados)
                
                msg += "\nPor favor, escolha outra data."
                return msg
            
            # ========== VALIDAÇÃO 2: DIAS ESPECIAIS ==========
            dias_fechados = self.clinic_info.get('dias_fechados', [])
            if date_str in dias_fechados:
                msg = f"❌ A clínica estará fechada em {date_str} (férias/feriado).\n\n"
                msg += "🚫 Dias especiais fechados:\n"
                msg += format_closed_days(dias_fechados)
                msg += "\nPor favor, escolha outra data disponível."
                return msg
            
            # ========== VALIDAÇÃO 3: CALCULAR SLOTS DISPONÍVEIS ==========
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
            
            # Pegar horário de funcionamento
            inicio_str, fim_str = horario_dia.split('-')
            inicio_time = datetime.strptime(inicio_str, '%H:%M').time()
            fim_time = datetime.strptime(fim_str, '%H:%M').time()
            
            # Buscar consultas já agendadas nesse dia
            date_str_formatted = appointment_date.strftime('%Y%m%d')  # YYYYMMDD
            existing_appointments = db.query(Appointment).filter(
                Appointment.appointment_date == date_str_formatted,
                Appointment.status == AppointmentStatus.AGENDADA
            ).all()
            
            # Gerar slots disponíveis (apenas horários INTEIROS)
            available_slots = []
            last_slot_time = fim_time
            current_time = inicio_time
            while current_time <= last_slot_time:
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
                    available_slots.append(current_time.strftime('%H:%M'))
                
                # Avançar 1 hora (apenas horários inteiros)
                current_time = (datetime.combine(appointment_date.date(), current_time) + 
                                timedelta(hours=1)).time()
            
            # Formatar mensagem
            dia_nome_completo = dias_semana_pt[weekday].upper()
            msg = f"✅ A data {date_str} é {dia_nome_completo}\n"
            msg += f"📅 Horário de atendimento: {horario_dia}\n"
            msg += f"⏰ Cada consulta dura {duracao} minutos\n\n"
            
            if available_slots:
                msg += "Horários disponíveis:\n"
                for slot in available_slots:
                    msg += f"• {slot}\n"
                msg += "\nQual horário você prefere?"
            else:
                msg += "❌ Não há horários disponíveis neste dia.\n"
                msg += "Por favor, escolha outra data."
            
            return msg
            
        except Exception as e:
            logger.error(f"Erro ao validar data e mostrar slots: {str(e)}")
            return f"Erro ao buscar horários disponíveis: {str(e)}"

    def _handle_confirm_time_slot(self, tool_input: Dict, db: Session, phone: str = None) -> str:
        """Validar e confirmar horário escolhido"""
        try:
            import re
            from app.utils import normalize_time_format
            
            context: Optional[ConversationContext] = None
            insurance_plan = "Particular"
            if phone:
                context = db.query(ConversationContext).filter_by(phone=phone).first()
                if context and context.flow_data:
                    insurance_plan = context.flow_data.get("insurance_plan", insurance_plan)
            
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
                
                # Validar dia da semana
                weekday = appointment_date.weekday()
                dias_semana_pt = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
                dia_nome = dias_semana_pt[weekday]
                
                horarios = self.clinic_info.get('horario_funcionamento', {})
                horario_dia = horarios.get(dia_nome, "FECHADO")
                
                if horario_dia == "FECHADO":
                    return f"❌ A clínica não atende em {dia_nome.capitalize()}. Por favor, escolha outra data."

                allowed_plan, reason_plan = appointment_rules.is_plan_allowed_on_date(appointment_date, insurance_plan)
                if not allowed_plan:
                    return f"❌ {reason_plan}\nPor favor, escolha outra data."

                capacity_ok, capacity_message = appointment_rules.has_capacity_for_insurance(appointment_date, insurance_plan, db)
                if not capacity_ok:
                    return f"❌ {capacity_message}\nPoderia escolher outra data, por favor?"
                
                # Calcular slots disponíveis
                inicio_str, fim_str = horario_dia.split('-')
                inicio_time = datetime.strptime(inicio_str, '%H:%M').time()
                fim_time = datetime.strptime(fim_str, '%H:%M').time()
                last_slot_time = fim_time
                
                # Buscar consultas já agendadas nesse dia
                date_str_formatted = appointment_date.strftime('%Y%m%d')  # YYYYMMDD
                existing_appointments = db.query(Appointment).filter(
                    Appointment.appointment_date == date_str_formatted,
                    Appointment.status == AppointmentStatus.AGENDADA
                ).all()
                
                # Gerar slots disponíveis (apenas horários INTEIROS)
                available_slots = []
                current_time = inicio_time
                while current_time <= last_slot_time:
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
                        available_slots.append(current_time.strftime('%H:%M'))
                    
                    # Avançar 1 hora (apenas horários inteiros)
                    current_time = (datetime.combine(appointment_date.date(), current_time) + 
                                    timedelta(hours=1)).time()
                
                # Montar mensagem com todos os horários disponíveis
                if available_slots:
                    msg = "❌ Por favor, escolha um horário inteiro (exemplo: 8:00, 14:00).\n\n"
                    msg += "Esses são os únicos horários disponíveis para esta data:\n"
                    for slot in available_slots:
                        msg += f"• {slot}\n"
                    return msg
                else:
                    return "❌ Por favor, escolha um horário inteiro (exemplo: 8:00, 14:00).\n\nNão há horários disponíveis para esta data."
            
            # Verificar disponibilidade no banco (segurança contra race condition)
            appointment_date = parse_date_br(date_str)
            if self._is_special_holiday_date(appointment_date):
                logger.info(f"⛱️ Horário solicitado para {date_str} está em período de férias - encaminhando secretaria.")
                return self._handoff_due_to_holiday(db, phone)
            allowed_plan, reason_plan = appointment_rules.is_plan_allowed_on_date(appointment_date, insurance_plan)
            if not allowed_plan:
                return f"❌ {reason_plan}\nPor favor, escolha outra data."

            capacity_ok, capacity_message = appointment_rules.has_capacity_for_insurance(appointment_date, insurance_plan, db)
            if not capacity_ok:
                return f"❌ {capacity_message}\nPoderia escolher outro dia, por favor?"

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
                        context.flow_data = {}
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
                if (not convenio or convenio == "particular"):
                    if extracted.get("insurance_plan"):
                        convenio = extracted["insurance_plan"]
                        logger.info(f"✅ Convênio encontrado no histórico: {convenio}")
                    else:
                        # FALLBACK: Usar Claude para buscar do histórico completo
                        try:
                            extracted_data = self._extract_patient_data_with_claude(context)
                            if extracted_data and extracted_data.get("insurance_plan"):
                                convenio = extracted_data["insurance_plan"]
                                # Normalizar valores
                                if convenio.lower() == "ipe":
                                    convenio = "IPE"
                                elif convenio.lower() == "cabergs":
                                    convenio = "CABERGS"
                                elif convenio.lower() in ["particular", "particula"]:
                                    convenio = "Particular"
                                
                                # IMPORTANTE: Salvar no flow_data para não perder novamente
                                context.flow_data["insurance_plan"] = convenio
                                db.commit()
                                logger.info(f"✅ Convênio recuperado via Claude e salvo: {convenio}")
                        except Exception as e:
                            logger.warning(f"⚠️ Erro ao buscar convênio com Claude: {e}")
                
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
            
            # Normalizar convênio antes de mostrar
            if convenio:
                if convenio.lower() == "ipe":
                    convenio = "IPE"
                elif convenio.lower() == "cabergs":
                    convenio = "CABERGS"
                elif convenio.lower() in ["particular", "particula"]:
                    convenio = "Particular"
                
                # Buscar nome formatado do clinic_info.json
                convenios_aceitos = self.clinic_info.get('convenios_aceitos', {})
                convenio_data = convenios_aceitos.get(convenio, {})
                convenio_nome = convenio_data.get('nome', convenio)
                msg += f"💳 Convênio: {convenio_nome}\n"
            
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
                        else:
                            # Tentar extrair do histórico usando extract_patient_data se não encontrou em flow_data
                            try:
                                extracted = self._extract_patient_data_with_claude(context)
                                if extracted.get("insurance_plan"):
                                    insurance_plan = extracted["insurance_plan"]
                                    # Salvar no flow_data para próximas interações
                                    context.flow_data["insurance_plan"] = insurance_plan
                                    db.commit()
                                    logger.info(f"💾 Convênio identificado e salvo no flow_data: {insurance_plan}")
                            except Exception as e:
                                logger.warning(f"⚠️ Erro ao tentar extrair convênio: {str(e)}")
            
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
            
            # SALVAMENTO AUTOMÁTICO: Após validação e normalização, salvar no flow_data para garantir persistência
            if insurance_plan and phone:
                context = db.query(ConversationContext).filter_by(phone=phone).first()
                if context:
                    if not context.flow_data:
                        context.flow_data = {}
                    convenio_anterior = context.flow_data.get("insurance_plan")
                    if convenio_anterior != insurance_plan:
                        context.flow_data["insurance_plan"] = insurance_plan
                        db.commit()
                        if convenio_anterior:
                            logger.info(f"💾 Convênio atualizado no flow_data: {convenio_anterior} → {insurance_plan}")
                        else:
                            logger.info(f"💾 Convênio salvo no flow_data: {insurance_plan}")
            
            # Log detalhado antes da validação
            logger.info(f"🔍 Validando dados para criar agendamento:")
            logger.info(f"   patient_name: {patient_name}")
            logger.info(f"   patient_phone: {patient_phone}")
            logger.info(f"   patient_birth_date: {patient_birth_date}")
            logger.info(f"   appointment_date: {appointment_date}")
            logger.info(f"   appointment_time: {appointment_time}")
            logger.info(f"   consultation_type: {consultation_type}")
            logger.info(f"   insurance_plan: {insurance_plan}")
            
            # Tentar extrair dados faltantes do flow_data antes de retornar erro
            if phone:
                context = db.query(ConversationContext).filter_by(phone=phone).first()
                if context and context.flow_data:
                    if not patient_name:
                        patient_name = context.flow_data.get("patient_name")
                    if not patient_birth_date:
                        patient_birth_date = context.flow_data.get("patient_birth_date")
                    if not appointment_date:
                        appointment_date = context.flow_data.get("appointment_date")
                    if not appointment_time:
                        appointment_time = context.flow_data.get("appointment_time")
            
            # Verificar quais campos estão faltando e listar especificamente
            missing_fields = []
            if not patient_name:
                missing_fields.append("nome completo")
            if not patient_birth_date:
                missing_fields.append("data de nascimento")
            if not appointment_date:
                missing_fields.append("data da consulta")
            if not appointment_time:
                missing_fields.append("horário da consulta")
            if not patient_phone:
                missing_fields.append("telefone")
            
            if missing_fields:
                logger.error(f"❌ VALIDAÇÃO FALHOU - Dados incompletos: {missing_fields}")
                if len(missing_fields) == 1:
                    return f"Para finalizar o agendamento, ainda preciso do seu {missing_fields[0]}. Pode me informar?"
                else:
                    fields_list = ", ".join(missing_fields[:-1]) + f" e {missing_fields[-1]}"
                    return f"Para finalizar o agendamento, ainda preciso de: {fields_list}. Pode me informar?"
            
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
                            context.flow_data = {}
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
                return f"❌ Horário {appointment_time} não está disponível. Use a tool check_availability para ver horários disponíveis."
            
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
                    flag_modified(context, "flow_data")
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
            
            # Formatar data e horário para exibição
            dias_semana = ['segunda-feira', 'terça-feira', 'quarta-feira', 
                          'quinta-feira', 'sexta-feira', 'sábado', 'domingo']
            appointment_datetime_obj = parse_date_br(appointment_date)
            if appointment_datetime_obj:
                dia_nome_completo = dias_semana[appointment_datetime_obj.weekday()]
                data_formatada = f"{dia_nome_completo}, {format_date_br(appointment_datetime_obj)}"
            else:
                data_formatada = appointment_date
            
            # Buscar endereço e informações adicionais
            endereco = self.clinic_info.get('endereco', 'Endereço não informado')
            info_adicionais = self.clinic_info.get('informacoes_adicionais', {})
            cadeira_rodas = info_adicionais.get('cadeira_rodas_disponivel', False)
            
            message_lines = [
                "✅ Agendamento confirmado com sucesso!",
                "",
                f"A consulta do {patient_name} está marcada para *{data_formatada} às {appointment_time}*.",
                "",
                "📋 Informações importantes:",
                "",
                "• Por favor, traga os últimos exames realizados",
                "• Traga também a lista de medicações que ele está tomando atualmente",
                f"• Nossa clínica fica na {endereco}",
            ]
            if cadeira_rodas:
                message_lines.append("• Temos cadeira de rodas disponível se necessário")
            message_lines.append("• Você receberá uma mensagem de lembrete no dia da consulta")
            message_lines.append("")
            message_lines.append("Posso te ajudar com mais alguma coisa?")

            return "\n".join(message_lines)
                   
        except Exception as e:
            logger.error(f"Erro ao criar agendamento: {str(e)}")
            db.rollback()
            return f"Erro ao criar agendamento: {str(e)}"

    def _handle_search_appointments(self, tool_input: Dict, db: Session) -> str:
        """Tool: search_appointments"""
        try:
            phone = tool_input.get("phone")
            name = tool_input.get("name")
            birth_date = tool_input.get("birth_date")
            consultation_type = tool_input.get("consultation_type")
            insurance_plan = tool_input.get("insurance_plan")
            only_future = tool_input.get("only_future", True)
            
            if not phone and not name and not birth_date:
                return "Preciso de pelo menos telefone, nome ou data de nascimento para localizar o agendamento."
            
            def _normalize(text: str) -> str:
                import unicodedata
                return ''.join(
                    ch for ch in unicodedata.normalize('NFD', text.lower())
                    if unicodedata.category(ch) != 'Mn'
                )
            
            filters_applied = []
            normalized_phone = normalize_phone(phone) if phone else None
            normalized_name = _normalize(name) if name else None
            normalized_birth = birth_date.strip() if isinstance(birth_date, str) and birth_date.strip() else None
            
            base_query = db.query(Appointment)
            if only_future:
                today_str = now_brazil().strftime('%Y%m%d')
                base_query = base_query.filter(Appointment.appointment_date >= today_str)
            
            if normalized_phone:
                filters_applied.append("telefone")
                appointments = base_query.filter(Appointment.patient_phone == normalized_phone).all()
            else:
                appointments = []
            
            if not appointments and normalized_name and normalized_birth:
                filters_applied.append("nome + nascimento")
                
                candidates = base_query.filter(
                    Appointment.patient_birth_date == normalized_birth
                ).all()
                
                appointments = []
                for apt in candidates:
                    stored_name = apt.patient_name or ""
                    if _normalize(stored_name).startswith(normalized_name.split()[0]):
                        from difflib import SequenceMatcher
                        score = SequenceMatcher(None, _normalize(stored_name), normalized_name).ratio()
                        if score >= 0.65:
                            appointments.append(apt)
                
                if not appointments:
                    for apt in candidates:
                        stored_name = apt.patient_name or ""
                        if _normalize(stored_name).startswith(normalized_name.split()[0]):
                            appointments.append(apt)
                            break
            
            if not appointments and normalized_name:
                filters_applied.append("nome aproximado")
                candidates = base_query.filter(
                    Appointment.patient_name.ilike(f"%{name}%")
                ).all()
                appointments = candidates
            
            if consultation_type:
                appointments = [
                    apt for apt in appointments
                    if (apt.consultation_type or "").strip().lower() == consultation_type.strip().lower()
                ]
            if insurance_plan:
                appointments = [
                    apt for apt in appointments
                    if (apt.insurance_plan or "").strip().lower() == insurance_plan.strip().lower()
                ]
            
            if not appointments:
                filtros = ", ".join(filters_applied) if filters_applied else "nenhum filtro aplicável"
                return f"Não encontrei agendamentos usando {filtros}. Pode confirmar os dados ou falar com nossa secretária?"
            
            appointments = sorted(
                appointments,
                key=lambda apt: (apt.appointment_date, apt.appointment_time)
            )
            
            if not appointments:
                return "Nenhum agendamento encontrado."
            
            response = f"📅 **Agendamentos encontrados:**\n\n"
            mapping = {}
            
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
                mapping[str(i)] = {
                    "id": apt.id,
                    "status": apt.status.value,
                    "date": app_date_formatted,
                    "time": app_time_str,
                    "consultation_type": apt.consultation_type,
                    "insurance_plan": apt.insurance_plan
                }
            
            flow_map = tool_input.get("flow_map")
            if isinstance(flow_map, dict):
                flow_map.update(mapping)
            
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
                return "No momento nossa secretária não está disponível (clínica fechada). Mas eu posso te ajudar com agendamentos, consultas e outras informações! Como posso te auxiliar?"
            
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
            
            # 5. Criar pausa para atendimento humano
            paused_until = datetime.utcnow() + timedelta(hours=24)
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
                temperature=0.3,
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
                context.flow_data = {}
            
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

    def _send_doctor_notification(self, patient_name: str, patient_birth_date: str, patient_address: str, patient_phone: str) -> bool:
        """Função auxiliar para enviar notificação à doutora sobre atendimento domiciliar"""
        try:
            # Buscar telefone da doutora do clinic_info
            doctor_phone = self.clinic_info.get("informacoes_adicionais", {}).get("telefone_doutora")
            if not doctor_phone:
                logger.error("❌ Telefone da doutora não encontrado no clinic_info.json")
                return False
            
            # Normalizar telefone
            doctor_phone = normalize_phone(doctor_phone)
            
            # Formatar mensagem
            message = f"""🏠 NOVA SOLICITAÇÃO DE ATENDIMENTO DOMICILIAR

👤 Paciente: {patient_name}
📅 Data Nascimento: {patient_birth_date}
📍 Endereço: {patient_address}
📞 Contato: {patient_phone}"""
            
            # Enfileirar task de envio
            from app.main import send_message_task
            send_message_task.delay(doctor_phone, message)
            
            logger.info(f"✅ Notificação enfileirada para doutora ({doctor_phone})")
            return True
            
        except Exception as e:
            logger.error(f"❌ Erro ao enviar notificação para doutora: {str(e)}")
            return False

    def _handle_request_home_address(self, tool_input: Dict, db: Session, phone: str) -> str:
        """Tool: request_home_address - Extrai e salva endereço do paciente"""
        try:
            logger.info(f"🏠 Tool request_home_address chamada para {phone}")
            
            # Buscar contexto
            context = db.query(ConversationContext).filter_by(phone=phone).first()
            if not context:
                return "Erro: contexto não encontrado."
            
            # Buscar última mensagem do usuário
            last_user_message = ""
            for msg in reversed(context.messages):
                if msg.get("role") == "user":
                    last_user_message = msg.get("content", "")
                    break
            
            if not last_user_message or len(last_user_message.strip()) < 10:
                return "Por favor, forneça seu endereço completo:\n\n📍 Cidade\n🏘️ Bairro\n🛣️ Rua\n🏠 Número da casa"
            
            # Validar se a mensagem parece ser um endereço (não é tipo de consulta)
            last_message_lower = last_user_message.lower()
            
            # Palavras que indicam que NÃO é um endereço (é tipo de consulta ou outra coisa)
            invalid_keywords = [
                "atendimento domiciliar", "domiciliar", "opção 3", "opcao 3", 
                "consulta", "tipo", "marcar", "agendar", "preciso", "quero"
            ]
            
            if any(keyword in last_message_lower for keyword in invalid_keywords):
                return "Por favor, forneça seu endereço completo:\n\n📍 Cidade\n🏘️ Bairro\n🛣️ Rua\n🏠 Número da casa\n\nApenas o endereço, não o tipo de consulta."
            
            # Se tem menos de 15 caracteres, provavelmente não é um endereço completo
            if len(last_user_message.strip()) < 15:
                return "Por favor, forneça seu endereço completo:\n\n📍 Cidade\n🏘️ Bairro\n🛣️ Rua\n🏠 Número da casa"
            
            # Salvar endereço no flow_data
            if not context.flow_data:
                context.flow_data = {}
            
            context.flow_data["patient_address"] = last_user_message.strip()
            flag_modified(context, "flow_data")
            db.commit()
            
            logger.info(f"💾 Endereço salvo no flow_data: {last_user_message.strip()[:50]}...")
            
            return "Endereço registrado! Agora vou enviar sua solicitação para a doutora."
            
        except Exception as e:
            logger.error(f"Erro ao processar endereço: {str(e)}")
            db.rollback()
            return f"Erro ao processar endereço: {str(e)}"

    def _handle_notify_doctor_home_visit(self, tool_input: Dict, db: Session, phone: str) -> str:
        """Tool: notify_doctor_home_visit - Envia notificação para a doutora"""
        try:
            logger.info(f"📞 Tool notify_doctor_home_visit chamada para {phone}")
            
            # Buscar contexto
            context = db.query(ConversationContext).filter_by(phone=phone).first()
            if not context:
                return "Erro: contexto não encontrado."
            
            # Buscar dados do flow_data
            flow_data = context.flow_data or {}
            
            patient_name = flow_data.get("patient_name")
            patient_birth_date = flow_data.get("patient_birth_date")
            patient_address = flow_data.get("patient_address")
            patient_phone = phone
            
            # Validar que todos os dados estão presentes
            missing_fields = []
            if not patient_name:
                missing_fields.append("nome")
            if not patient_birth_date:
                missing_fields.append("data de nascimento")
            if not patient_address:
                missing_fields.append("endereço")
            
            if missing_fields:
                return f"Erro: faltam informações: {', '.join(missing_fields)}. Por favor, forneça todas as informações necessárias."
            
            # Enviar notificação
            success = self._send_doctor_notification(
                patient_name, 
                patient_birth_date, 
                patient_address, 
                patient_phone
            )
            
            if success:
                # Marcar que notificação foi enviada
                flow_data["doctor_notified"] = True
                context.flow_data = flow_data
                flag_modified(context, "flow_data")
                db.commit()
                
                logger.info("✅ Notificação enviada com sucesso para a doutora")
                return "Notificação enviada com sucesso para a doutora!"
            else:
                return "Erro ao enviar notificação. Por favor, tente novamente."
            
        except Exception as e:
            logger.error(f"Erro ao notificar doutora: {str(e)}")
            db.rollback()
            return f"Erro ao notificar doutora: {str(e)}"

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