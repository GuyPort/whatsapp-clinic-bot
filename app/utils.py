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
    if not date_str or not isinstance(date_str, str):
        return None
    
    try:
        # Regex rigorosa: exatamente 2 dígitos dia, 2 mês, 4 ano
        match = re.match(r'^(\d{2})/(\d{2})/(\d{4})$', date_str)
        if not match:
            return None
        
        day, month, year = match.groups()
        
        # Validar se é uma data real
        date_obj = datetime(int(year), int(month), int(day))
        
        return date_obj
        
    except (ValueError, AttributeError):
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
        Número limpo (apenas dígitos) com código do país
    """
    # Validar entrada
    if not phone or not isinstance(phone, str):
        return ""
    
    clean = re.sub(r'\D', '', phone)
    
    # Validar tamanho (máximo 15 dígitos conforme padrão internacional)
    if len(clean) > 15:
        return ""
    
    # Garantir que tem código do país (55) para Brasil
    if not clean.startswith('55') and len(clean) >= 10:
        clean = '55' + clean
    
    return clean


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


def round_up_to_next_5_minutes(dt: datetime) -> datetime:
    """Arredonda para cima ao próximo múltiplo de 5 minutos, zerando segundos/micros.
    Se já for múltiplo de 5, retorna o próprio horário normalizado.
    """
    minute_mod = dt.minute % 5
    if minute_mod == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt
    add = 0 if minute_mod == 0 else (5 - minute_mod)
    rounded = dt.replace(second=0, microsecond=0) + timedelta(minutes=add)
    if minute_mod == 0:
        rounded = dt.replace(second=0, microsecond=0)
    return rounded


def validate_time_format(time_str: str) -> bool:
    """
    Valida se o formato de horário está correto (HH:MM) e se é um horário válido.
    
    Args:
        time_str: String do horário no formato HH:MM
        
    Returns:
        True se válido, False caso contrário
    """
    if not time_str or not isinstance(time_str, str):
        return False
    
    # Verificar formato HH:MM
    import re
    if not re.match(r'^\d{2}:\d{2}$', time_str):
        return False
    
    try:
        hour, minute = time_str.split(':')
        hour_int = int(hour)
        minute_int = int(minute)
        
        # Validar hora (00-23) e minuto (00-59)
        if not (0 <= hour_int <= 23) or not (0 <= minute_int <= 59):
            return False
        
        # Só aceitar horários inteiros (minutos == 00)
        if minute_int != 0:
            return False
        
        return True
    except (ValueError, IndexError):
        return False


def normalize_time_format(time_str: str) -> Optional[str]:
    """
    Normaliza formato de horário para HH:MM.
    Aceita: "8:00", "8", "8:0", "08:00"
    Retorna: "08:00" ou None se inválido
    """
    if not time_str or not isinstance(time_str, str):
        return None
    
    time_str = time_str.strip()
    
    # Padrão: H:MM ou HH:MM ou H:M ou HH:M
    match = re.match(r'^(\d{1,2})(?::(\d{1,2}))?$', time_str)
    if not match:
        return None
    
    hour_str, minute_str = match.groups()
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    
    # Validar ranges
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    
    return f"{hour:02d}:{minute:02d}"


def get_minimum_appointment_datetime() -> datetime:
    """
    Calcula a data/hora mínima para agendamento (48 horas a partir de agora).
    
    Returns:
        datetime object representando data/hora atual + 48 horas no timezone do Brasil
    """
    now = now_brazil()
    minimum_datetime = now + timedelta(hours=48)
    return minimum_datetime