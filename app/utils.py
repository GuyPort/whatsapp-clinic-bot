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
        Número limpo (apenas dígitos) com código do país
    """
    clean = re.sub(r'\D', '', phone)
    
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

