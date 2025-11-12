"""
Funções utilitárias e helpers.
"""
from datetime import datetime, timedelta, time
import logging
import re
import json
import pytz
from typing import Optional, Dict, Any, List, Mapping

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


def parse_appointment_datetime(
    appointment_date: Optional[str],
    appointment_time: Optional[str],
    *,
    timezone: Optional[pytz.BaseTzInfo] = None
) -> Optional[datetime]:
    """
    Converte os campos `appointment_date` (YYYYMMDD) e `appointment_time` (HH:MM)
    em um datetime timezone-aware.
    """
    if not appointment_date or not appointment_time:
        return None
    
    timezone = timezone or get_brazil_timezone()
    
    if isinstance(appointment_time, time):
        appointment_time = appointment_time.strftime("%H:%M:%S")
    
    date_candidates = [appointment_date]
    if "-" in appointment_date:
        date_candidates.append(appointment_date.replace("-", ""))
    
    time_candidates = [appointment_time]
    if len(appointment_time) == 8 and appointment_time.endswith(":00"):
        time_candidates.append(appointment_time[:5])
    
    naive_dt = None
    for date_value in date_candidates:
        for time_value in time_candidates:
            try:
                naive_dt = datetime.strptime(
                    f"{date_value}{time_value}",
                    "%Y%m%d%H:%M"
                )
                break
            except (ValueError, TypeError):
                continue
        if naive_dt:
            break
    if not naive_dt:
        return None
    
    if naive_dt.tzinfo:
        return naive_dt.astimezone(timezone)
    
    return timezone.localize(naive_dt)


def format_pre_appointment_reminder(
    patient_name: str,
    appointment_dt: datetime,
    *,
    clinic_info: Optional[Mapping[str, Any]] = None
) -> str:
    """
    Monta o texto padrão de lembrete pré-consulta 24 horas antes.
    """
    if appointment_dt.tzinfo is None:
        appointment_dt = get_brazil_timezone().localize(appointment_dt)
    else:
        appointment_dt = appointment_dt.astimezone(get_brazil_timezone())
    
    clinic_info = clinic_info or load_clinic_info()
    address = clinic_info.get(
        "endereco",
        "Rua Dr. Edmundo Lauffer, 299 - Bom Pastor - Igrejinha/RS - CEP 95650-000"
    )
    
    date_str = appointment_dt.strftime("%d/%m/%Y")
    time_str = appointment_dt.strftime("%H:%M")
    
    message_lines = [
        f"Olá, {patient_name}! Passando para confirmar sua consulta referente ao dia {date_str} às {time_str}.",
        "Compareça 15 minutos antes, traga seus últimos exames e, se possível, uma lista com as medicações que você usa.",
        f"Endereço: {address}",
        "Caso precise de cadeira de rodas na chegada, é só nos avisar!",
        "",
        "Esta é uma mensagem automática de confirmação — por favor, não responda."
    ]
    
    return "\n".join(message_lines)


def log_event(
    event_name: str,
    details: Optional[Mapping[str, Any]] = None,
    *,
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Emite um log estruturado para observabilidade do agente.

    Args:
        event_name: Nome do evento a ser registrado (snake_case).
        details: Dicionário com informações adicionais do evento.
        logger: Logger específico. Quando omitido utiliza logging.getLogger(__name__).
    """
    target_logger = logger or logging.getLogger(__name__)
    payload = {"event": event_name}
    if details:
        try:
            # Garantir serialização segura para logs
            serializable = json.loads(json.dumps(details, default=str))
            payload.update(serializable)
        except (TypeError, ValueError):
            payload["details_serialization_error"] = True
    target_logger.info(payload)


def compute_missing_fields(flow_data: Optional[Dict[str, Any]]) -> List[str]:
    """
    Calcula os campos obrigatórios ainda não preenchidos no flow_data.

    Args:
        flow_data: Dicionário persistido no contexto da conversa.

    Returns:
        Lista de campos pendentes.
    """
    required_keys = [
        "patient_name",
        "patient_birth_date",
        "consultation_type",
        "insurance_plan",
        "appointment_date",
        "appointment_time",
    ]
    if not flow_data:
        return required_keys.copy()
    missing = []
    for key in required_keys:
        value = flow_data.get(key)
        if value in (None, "", []):
            missing.append(key)
    return missing