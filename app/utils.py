"""
Funções utilitárias e helpers.
"""
from datetime import datetime, timedelta
import re
import json
import pytz
from typing import Optional, Dict, Any

from app.simple_config import settings


def get_brazil_timezone():
    """Retorna o timezone do Brasil"""
    return pytz.timezone(settings.timezone)


def now_brazil() -> datetime:
    """Retorna a data/hora atual no timezone do Brasil"""
    return datetime.now(get_brazil_timezone())


def parse_date_br(date_str: str) -> Optional[datetime]:
    """
    Parse de data no formato brasileiro DD/MM/AAAA
    
    Args:
        date_str: String da data no formato DD/MM/AAAA
        
    Returns:
        datetime object ou None se inválido
    """
    try:
        # Aceitar tanto DD/MM/AAAA quanto DD/MM/AA
        if len(date_str) == 8:  # DD/MM/AA
            return datetime.strptime(date_str, "%d/%m/%y")
        else:  # DD/MM/AAAA
            return datetime.strptime(date_str, "%d/%m/%Y")
    except ValueError:
        return None


def format_date_br(dt: datetime) -> str:
    """Formata datetime para DD/MM/AAAA"""
    return dt.strftime("%d/%m/%Y")


def format_datetime_br(dt: datetime) -> str:
    """Formata datetime para DD/MM/AAAA HH:MM"""
    return dt.strftime("%d/%m/%Y às %H:%M")


def format_time_br(dt: datetime) -> str:
    """Formata datetime para HH:MM"""
    return dt.strftime("%H:%M")


def normalize_phone(phone: str) -> str:
    """
    Normaliza número de telefone removendo caracteres especiais.
    
    Args:
        phone: Número de telefone com ou sem formatação
        
    Returns:
        Número limpo (apenas dígitos)
    """
    # Remove tudo que não é dígito
    clean = re.sub(r'\D', '', phone)
    
    # Remove código do país se presente (55)
    if clean.startswith('55') and len(clean) > 11:
        clean = clean[2:]
    
    return clean


def extract_name_from_message(message: str) -> Optional[str]:
    """
    Tenta extrair um nome de uma mensagem.
    Procura por padrões como "meu nome é X" ou apenas um nome próprio.
    """
    message = message.strip()
    
    # Padrões comuns
    patterns = [
        r'(?:meu nome é|me chamo|sou (?:o|a)?) ([A-ZÁÀÂÃÉÈÊÍÏÓÔÕÖÚÇÑ][a-záàâãéèêíïóôõöúçñ]+(?: [A-ZÁÀÂÃÉÈÊÍÏÓÔÕÖÚÇÑ][a-záàâãéèêíïóôõöúçñ]+)+)',
        r'^([A-ZÁÀÂÃÉÈÊÍÏÓÔÕÖÚÇÑ][a-záàâãéèêíïóôõöúçñ]+(?: [A-ZÁÀÂÃÉÈÊÍÏÓÔÕÖÚÇÑ][a-záàâãéèêíïóôõöúçñ]+)+)$',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(1).title()
    
    return None


def extract_date_from_message(message: str) -> Optional[str]:
    """
    Tenta extrair uma data no formato DD/MM/AAAA ou DD/MM/AA de uma mensagem.
    """
    # Procura por padrão de data
    patterns = [
        r'\b(\d{2}/\d{2}/\d{4})\b',  # DD/MM/AAAA
        r'\b(\d{2}/\d{2}/\d{2})\b',   # DD/MM/AA
    ]
    
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            return match.group(1)
    
    return None


def parse_weekday_from_message(message: str) -> Optional[int]:
    """
    Extrai dia da semana de uma mensagem.
    
    Returns:
        0-6 (segunda a domingo) ou None
    """
    message = message.lower()
    
    weekdays = {
        'segunda': 0,
        'terça': 1,
        'terca': 1,
        'quarta': 2,
        'quinta': 3,
        'sexta': 4,
        'sábado': 5,
        'sabado': 5,
        'domingo': 6,
    }
    
    for day_name, day_num in weekdays.items():
        if day_name in message:
            return day_num
    
    return None


def is_valid_birth_date(date_str: str) -> bool:
    """
    Valida se a data de nascimento é válida e razoável.
    Deve ser uma data passada e a pessoa deve ter entre 0 e 120 anos.
    """
    date = parse_date_br(date_str)
    if not date:
        return False
    
    today = now_brazil().date()
    birth_date = date.date()
    
    # Data não pode ser no futuro
    if birth_date > today:
        return False
    
    # Idade deve ser razoável (0-120 anos)
    age = (today - birth_date).days / 365.25
    if age < 0 or age > 120:
        return False
    
    return True


def load_clinic_info() -> Dict[str, Any]:
    """
    Carrega informações da clínica do arquivo JSON.
    
    Returns:
        Dicionário com informações da clínica
    """
    try:
        with open('data/clinic_info.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        raise Exception("Arquivo data/clinic_info.json não encontrado!")
    except json.JSONDecodeError:
        raise Exception("Erro ao ler data/clinic_info.json - JSON inválido!")


def format_currency(value: float) -> str:
    """Formata valor monetário para o padrão brasileiro"""
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def calculate_age(birth_date_str: str) -> Optional[int]:
    """Calcula idade a partir da data de nascimento"""
    birth_date = parse_date_br(birth_date_str)
    if not birth_date:
        return None
    
    today = now_brazil().date()
    return int((today - birth_date.date()).days / 365.25)


def detect_frustration_keywords(message: str) -> bool:
    """
    Detecta palavras-chave que indicam frustração ou necessidade de escalação.
    """
    message_lower = message.lower()
    
    frustration_keywords = [
        'não entend',
        'não consigo',
        'não está funcionando',
        'falar com',
        'atendente',
        'humano',
        'pessoa',
        'não resolve',
        'problema',
        'reclamação',
        'urgente',
        'emergência',
        'irritado',
        'chato',
    ]
    
    return any(keyword in message_lower for keyword in frustration_keywords)


def detect_inappropriate_language(message: str) -> bool:
    """
    Detecta linguagem inapropriada ou ofensiva.
    """
    message_lower = message.lower()
    
    inappropriate_words = [
        'idiota',
        'burro',
        'imbecil',
        'merda',
        # Adicione mais conforme necessário, mas com cuidado
    ]
    
    return any(word in message_lower for word in inappropriate_words)

