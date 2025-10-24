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
from anthropic import Anthropic

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.simple_config import settings
from app.models import Appointment, AppointmentStatus, ConversationContext, PausedContact
from app.utils import (
    load_clinic_info, normalize_phone, parse_date_br, 
    format_datetime_br, now_brazil, get_brazil_timezone, round_up_to_next_5_minutes
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

2. IMPORTANTE SOBRE EXTRAÇÃO:
   - Se receber AMBOS (nome + data completa): extraia e confirme, depois vá para tipo de consulta
   - Se receber APENAS NOME: agradeça e peça "E sua data de nascimento (DD/MM/AAAA)?"
   - Se receber APENAS DATA: agradeça e peça "E seu nome completo?"
   - Se NENHUM for extraído: "Não consegui entender. Por favor, me informe seu nome completo."
   
   VALIDAÇÕES OBRIGATÓRIAS:
   - NOME: Deve ter no mínimo 2 palavras (nome + sobrenome)
   - DATA: Deve ser completa (dia + mês + ano) no formato DD/MM/AAAA
   - Se nome tiver apenas 1 palavra: "Para o cadastro médico, preciso do nome completo (nome e sobrenome)"
   - Se data incompleta: "Preciso da data completa (dia, mês e ano). Ex: 07/08/2003"

   VALIDAÇÃO DE DATA:
   - Você apenas EXTRAI a data da mensagem do usuário
   - A função Python valida automaticamente se é válida (não futura)
   - Você apenas COMUNICA o resultado:
     • Se houver erro_data → Peça a data correta
     • Se não houver erro_data → Aceite e continue
   - NUNCA faça validação manual de datas

3. Após receber a data de nascimento:
   "Perfeito! Agora me informe qual tipo de consulta você deseja:
   
   1️⃣ Clínica Geral - R$ 300
   2️⃣ Geriatria Clínica e Preventiva - R$ 300
   3️⃣ Atendimento Domiciliar ao Paciente Idoso - R$ 500
   
   Digite o número da opção desejada:"

4. Após receber o tipo (1, 2 ou 3):
   "Ótimo! Possui convênio médico?
   
   • CABERGS
   • IPE
   
   Digite o nome do convênio ou 'não' se não tiver."
   
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

5. Após receber o convênio (1, 2 ou 3):
   "Agora me informe o dia que gostaria de marcar a consulta (DD/MM/AAAA - ex: 25/11/2025):"

6. Após receber a data desejada:
   "Ótimo! E que horário você prefere? (HH:MM - ex: 14:30):"

7. **FLUXO CRÍTICO - Após receber horário:**
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
                "consultation_type": None,
                "insurance_plan": None
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
                
                # 2. EXTRAÇÃO DE NOME E DATA - Usar nova função robusta
                resultado = self._extrair_nome_e_data_robusto(content)
                
                # Atualizar nome se extraído com sucesso
                if resultado["nome"] and not data["patient_name"]:
                    data["patient_name"] = resultado["nome"]
                    logger.info(f"📝 Nome extraído: {resultado['nome']}")
                
                # Atualizar data nascimento se extraída com sucesso
                if resultado["data"] and not data["patient_birth_date"]:
                    data["patient_birth_date"] = resultado["data"]
                    logger.info(f"📅 Data nascimento extraída: {resultado['data']}")
                
                # 3. EXTRAÇÃO DE DATAS DE CONSULTA - Buscar em qualquer parte da mensagem
                date_pattern = r'(\d{1,2})/(\d{1,2})/(\d{4})'
                date_matches = re.findall(date_pattern, content)
                for match in date_matches:
                    day, month, year = match
                    full_date = f"{day.zfill(2)}/{month.zfill(2)}/{year}"
                    y = int(year)
                    if y >= 2010 and not data["appointment_date"]:
                        data["appointment_date"] = full_date
                
                # 4. EXTRAÇÃO DE TIPO DE CONSULTA - Detectar escolha numérica
                if not data["consultation_type"]:
                    # Se mensagem é só "1", "2" ou "3" (escolha de tipo)
                    if content in ["1", "2", "3"]:
                        type_map = {"1": "clinica_geral", "2": "geriatria", "3": "domiciliar"}
                        data["consultation_type"] = type_map[content]
                
                # 5. EXTRAÇÃO DE CONVÊNIO - Casos óbvios (o resto o Claude decide)
                if not data["insurance_plan"]:
                    content_lower = content.lower().strip()
                    
                    # Apenas detectar menções diretas de convênios específicos
                    if "cabergs" in content_lower:
                        data["insurance_plan"] = "CABERGS"
                    elif "ipe" in content_lower:
                        data["insurance_plan"] = "IPE"
                    # Compatibilidade numérica (quando usuário responde só "1" ou "2")
                    elif content in ["1", "2"]:
                        insurance_map = {"1": "CABERGS", "2": "IPE"}
                        data["insurance_plan"] = insurance_map[content]
                    
                    # Para tudo mais (incluindo respostas negativas), deixar Claude classificar
                    # Claude vai entender a intenção e agir conforme instruções do system prompt
            
            logger.info(f"📋 Extração concluída: {data}")
            return data
        except Exception as e:
            logger.error(f"Erro ao extrair dados do histórico: {e}", exc_info=True)
            return {}

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
                
                # Validar se data é futura
                if data_obj.date() > datetime.now().date():
                    resultado["erro_data"] = (
                        f"A data {dia}/{mes}/{ano} está no futuro. "
                        f"Por favor, informe sua data de nascimento (não pode ser futura)."
                    )
                # Validar idade máxima (120 anos)
                elif (datetime.now() - data_obj).days / 365.25 > 120:
                    resultado["erro_data"] = "Data de nascimento parece incorreta (mais de 120 anos)"
                else:
                    resultado["data"] = f"{dia}/{mes}/{ano}"
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
                    
                    # Validar se data é futura
                    if data_obj.date() > datetime.now().date():
                        resultado["erro_data"] = (
                            f"A data {dia}/{mes}/{ano} está no futuro. "
                            f"Por favor, informe sua data de nascimento (não pode ser futura)."
                        )
                    # Validar idade máxima (120 anos)
                    elif (datetime.now() - data_obj).days / 365.25 > 120:
                        resultado["erro_data"] = "Data de nascimento parece incorreta (mais de 120 anos)"
                    else:
                        resultado["data"] = f"{dia}/{mes}/{ano}"
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
                        
                        # Validar se data é futura
                        if data_obj.date() > datetime.now().date():
                            resultado["erro_data"] = (
                                f"A data {dia}/{mes}/{ano} está no futuro. "
                                f"Por favor, informe sua data de nascimento (não pode ser futura)."
                            )
                        # Validar idade máxima (120 anos)
                        elif (datetime.now() - data_obj).days / 365.25 > 120:
                            resultado["erro_data"] = "Data de nascimento parece incorreta (mais de 120 anos)"
                        else:
                            resultado["data"] = f"{dia}/{mes_num}/{ano}"
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
                            
                            # Validar se data é futura
                            if data_obj.date() > datetime.now().date():
                                resultado["erro_data"] = (
                                    f"A data {dia}/{mes}/{ano} está no futuro. "
                                    f"Por favor, informe sua data de nascimento (não pode ser futura)."
                                )
                            # Validar idade máxima (120 anos)
                            elif (datetime.now() - data_obj).days / 365.25 > 120:
                                resultado["erro_data"] = "Data de nascimento parece incorreta (mais de 120 anos)"
                            else:
                                resultado["data"] = f"{dia}/{mes_num}/{ano}"
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
                
                if len(palavras_validas) >= 2:
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
            negative_triggers = [
                "nao", "não", "só isso", "so isso", "obrigado", "obrigada", "encerrar", "finalizar",
                "nada", "por enquanto nao", "por enquanto não"
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

            # Encerrar se negativa após pergunta final ou negativa sem fluxo ativo
            if is_negative and (last_assistant_asks_more or not context.current_flow):
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

            # 4. Verificar se há confirmação pendente ANTES de processar com Claude
            if context.flow_data and context.flow_data.get("pending_confirmation"):
                intent = self._detect_confirmation_intent(message)
                
                if intent == "positive":
                    # Usuário confirmou! Executar agendamento
                    logger.info(f"✅ Usuário {phone} confirmou agendamento")
                    
                    # Usar dados do flow_data (NÃO re-extrair do histórico)
                    data = context.flow_data or {}
                    
                    # Se faltar nome ou data de nascimento, extrair do histórico APENAS UMA VEZ
                    if not data.get("patient_name") or not data.get("patient_birth_date"):
                        logger.info(f"🔍 Dados incompletos no flow_data, extraindo do histórico: {data}")
                        extracted = self._extract_appointment_data_from_messages(context.messages)
                        data["patient_name"] = data.get("patient_name") or extracted.get("patient_name")
                        data["patient_birth_date"] = data.get("patient_birth_date") or extracted.get("patient_birth_date")
                        logger.info(f"🔍 Dados após extração: {data}")
                    
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
                        context.flow_data = {}
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
                model="claude-3-5-sonnet-20241022",
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
                                # Se tool_result indica disponibilidade, tentar criar agendamento automaticamente
                                if "disponível" in tool_result.lower():
                                    # Extrair dados das mensagens e criar agendamento diretamente
                                    logger.warning("⚠️ Claude não criou agendamento, fazendo fallback automático")
                                    try:
                                        # Extrair dados do histórico de mensagens
                                        appointment_data = self._extract_appointment_data_from_messages(context.messages) or {}
                                        
                                        # Adicionar telefone do paciente (disponível no contexto phone)
                                        appointment_data["patient_phone"] = phone
                                        
                                        logger.info(f"📋 Dados extraídos: {appointment_data}")
                                        
                                        # Validar se todos os dados foram extraídos
                                        required = [
                                            "patient_name","patient_birth_date","appointment_date","appointment_time","patient_phone"
                                        ]
                                        missing = [k for k in required if not appointment_data.get(k)]
                                        if not missing:
                                            appointment_result = self._handle_create_appointment(appointment_data, db)
                                            bot_response = f"Perfeito! {appointment_result}"
                                        else:
                                            logger.error(f"❌ Dados incompletos extraídos: {appointment_data}")
                                            bot_response = (
                                                "Quase lá! Preciso só de: " + ", ".join(missing) + ". "
                                                "Por favor, me informe para concluir o agendamento."
                                            )
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
            
            # 7.5. Persistir dados incrementalmente no flow_data
            # Após cada resposta do Claude, verificar se coletou nome ou data nascimento
            # e salvar no flow_data imediatamente (não sobrescrever dados existentes)
            if not context.flow_data:
                context.flow_data = {}
            
            # Extrair dados do histórico
            extracted = self._extract_appointment_data_from_messages(context.messages)
            
            # Salvar no flow_data APENAS os campos que ainda não existem
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
            
            if extracted.get("insurance_plan") and not context.flow_data.get("insurance_plan"):
                context.flow_data["insurance_plan"] = extracted["insurance_plan"]
                logger.info(f"💾 Convênio salvo no flow_data: {extracted['insurance_plan']}")
            
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
                return self._handle_validate_and_check_availability(tool_input, db, phone)
            elif tool_name == "create_appointment":
                return self._handle_create_appointment(tool_input, db, phone)
            elif tool_name == "search_appointments":
                return self._handle_search_appointments(tool_input, db)
            elif tool_name == "cancel_appointment":
                return self._handle_cancel_appointment(tool_input, db)
            elif tool_name == "request_human_assistance":
                return self._handle_request_human_assistance(tool_input, db, phone)
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
                        # Extrair dados do histórico ANTES de salvar no flow_data
                        extracted = self._extract_appointment_data_from_messages(context.messages)
                        
                        # Preservar dados já existentes, adicionar novos
                        if not context.flow_data:
                            context.flow_data = {}
                        context.flow_data.update({
                            "patient_name": context.flow_data.get("patient_name") or extracted.get("patient_name"),
                            "patient_birth_date": context.flow_data.get("patient_birth_date") or extracted.get("patient_birth_date"),
                            "appointment_date": date_str,
                            "appointment_time": hora_consulta.strftime('%H:%M'),
                            "pending_confirmation": True
                        })
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
            if phone:
                context = db.query(ConversationContext).filter_by(phone=phone).first()
                if context and context.flow_data:
                    # Usar dados do contexto como fallback
                    if not patient_phone:
                        patient_phone = context.flow_data.get("patient_phone") or phone
                    # Priorizar dados do flow_data se disponíveis
                    if context.flow_data.get("consultation_type"):
                        consultation_type = context.flow_data.get("consultation_type")
                    if context.flow_data.get("insurance_plan"):
                        insurance_plan = context.flow_data.get("insurance_plan")
            
            # Validar tipo de consulta
            valid_types = ["clinica_geral", "geriatria", "domiciliar"]
            if consultation_type not in valid_types:
                consultation_type = "clinica_geral"  # Fallback
            
            # Validar convênio
            valid_insurance = ["CABERGS", "IPE", "particular"]
            if insurance_plan not in valid_insurance:
                insurance_plan = "particular"  # Fallback
            
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
            duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
            is_available = appointment_rules.check_slot_availability(appointment_datetime_local, duracao, db)
            
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
            
            # Buscar informações do tipo de consulta e convênio
            tipos_consulta = self.clinic_info.get('tipos_consulta', {})
            tipo_info = tipos_consulta.get(consultation_type, {})
            tipo_nome = tipo_info.get('nome', 'Clínica Geral')
            tipo_valor = tipo_info.get('valor', 300)
            
            convenios_aceitos = self.clinic_info.get('convenios_aceitos', {})
            convenio_info = convenios_aceitos.get(insurance_plan, {})
            convenio_nome = convenio_info.get('nome', 'Particular')
            
            return f"✅ **Agendamento realizado com sucesso!**\n\n" + \
                   "Obrigado por confiar em nossa clínica! 😊\n" + \
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