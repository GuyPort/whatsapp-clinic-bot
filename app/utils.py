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
    # Remove tudo que não é dígito
    clean = re.sub(r'\D', '', phone)
    
    # Garantir que tem código do país (55) para Brasil
    if not clean.startswith('55') and len(clean) >= 10:
        clean = '55' + clean
    
    return clean


# Funções de extração removidas - Claude agora gerencia isso


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


# Funções de detecção removidas - Claude agora gerencia isso

