"""
Regras e validações para agendamento de consultas.
"""
from datetime import datetime, timedelta, time
from typing import List, Dict, Any, Tuple, Optional
import logging

from sqlalchemy.orm import Session

from app.models import Appointment, AppointmentStatus
from app.utils import now_brazil, format_time_br, load_clinic_info, get_brazil_timezone, parse_date_br

logger = logging.getLogger(__name__)


class AppointmentRules:
    """Gerenciador de regras de agendamento"""
    
    def __init__(self):
        self.clinic_info = load_clinic_info()
        self.rules = self.clinic_info.get('regras_agendamento', {})
        self.timezone = get_brazil_timezone()
    
    def reload_clinic_info(self):
        """Recarrega informações da clínica"""
        self.clinic_info = load_clinic_info()
        self.rules = self.clinic_info.get('regras_agendamento', {})
    
    def get_interval_between_appointments(self) -> int:
        """Retorna intervalo mínimo entre consultas em minutos"""
        return self.rules.get('intervalo_entre_consultas_minutos', 15)
    
    def is_valid_appointment_date(self, appointment_date: datetime) -> Tuple[bool, str]:
        """
        Valida se uma data/hora é válida para agendamento.
        
        Args:
            appointment_date: Data/hora proposta
            
        Returns:
            (válido, mensagem_erro)
        """
        now = now_brazil()
        
        # 1. Data não pode ser no passado
        # Converter para timezone-aware se necessário
        if appointment_date.tzinfo is None:
            appointment_date = self.timezone.localize(appointment_date)
        if now.tzinfo is None:
            now = self.timezone.localize(now)
            
        if appointment_date <= now:
            return False, "A data deve ser no futuro."
        
        # 2. Verificar se data está em dias fechados especiais
        dias_fechados = self.clinic_info.get('dias_fechados', [])
        if dias_fechados:
            try:
                closed_dates = {
                    datetime.strptime(dia_str, '%d/%m/%Y').date()
                    for dia_str in dias_fechados
                }
            except ValueError:
                closed_dates = set()
        else:
            closed_dates = set()
        
        if appointment_date.date() in closed_dates:
            return False, "A clínica estará fechada nessa data."
        
        # 3. Verificar dia da semana
        weekday = appointment_date.weekday()  # 0=segunda, 6=domingo
        dias_semana = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
        dia_nome = dias_semana[weekday]
        
        # 4. Verificar se o dia está aberto
        horarios_disponiveis = self.clinic_info.get('horarios_disponiveis', {})
        horarios_do_dia = horarios_disponiveis.get(dia_nome, [])
        
        if not horarios_do_dia:
            return False, f"A clínica não atende às {dias_semana[weekday]}s."
        
        # 5. Verificar se o horário solicitado está na lista de horários disponíveis
        hora_consulta_str = appointment_date.strftime('%H:%M')
        
        if hora_consulta_str not in horarios_do_dia:
            horarios_formatados = ', '.join(horarios_do_dia)
            return False, f"Horário inválido. Horários disponíveis para {dias_semana[weekday]}: {horarios_formatados}"
        
        return True, ""

    def find_next_available_slots(
        self,
        start_after: datetime,
        db: Session,
        limit: int = 3,
        max_days: int = 90
    ) -> List[datetime]:
        """Busca próximos horários disponíveis respeitando carência mínima."""

        if limit <= 0:
            return []

        tz = self.timezone

        # Garantir timezone-aware
        if start_after.tzinfo is None:
            start_after = tz.localize(start_after)
        else:
            start_after = start_after.astimezone(tz)

        start_after_naive = start_after.replace(tzinfo=None)

        duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
        available_slots: List[datetime] = []

        dias_fechados = self.clinic_info.get('dias_fechados', [])
        closed_dates: set = set()
        for dia_str in dias_fechados:
            try:
                closed_dates.add(datetime.strptime(dia_str, '%d/%m/%Y').date())
            except ValueError:
                continue

        for day_offset in range(max_days):
            day_candidate = (start_after_naive + timedelta(days=day_offset)).date()

            if day_candidate in closed_dates:
                continue

            day_reference = datetime.combine(day_candidate, time.min)
            day_slots = self.get_available_slots(day_reference, duracao, db)

            for slot in sorted(day_slots):
                if slot < start_after_naive:
                    continue
                available_slots.append(slot)
                if len(available_slots) >= limit:
                    return available_slots

        return available_slots

    def get_slots_for_specific_date(
        self,
        target_date: datetime,
        db: Session,
        start_after: Optional[datetime] = None
    ) -> List[datetime]:
        """Retorna horários disponíveis para uma data específica, respeitando carência opcional."""

        duracao = self.clinic_info.get('regras_agendamento', {}).get('duracao_consulta_minutos', 60)
        slots = self.get_available_slots(target_date, duracao, db)

        if start_after is None:
            return slots

        tz = self.timezone
        if start_after.tzinfo is None:
            start_after = tz.localize(start_after)
        else:
            start_after = start_after.astimezone(tz)

        threshold = start_after.replace(tzinfo=None)
        return [slot for slot in slots if slot >= threshold]
    
    def get_available_slots(
        self,
        target_date: datetime,
        consultation_duration: int,
        db: Session,
        limit: int = None
    ) -> List[datetime]:
        """
        Retorna horários disponíveis para uma data específica.
        
        Args:
            target_date: Data alvo (só a data importa, hora será ignorada)
            consultation_duration: Duração da consulta em minutos
            db: Sessão do banco de dados
            limit: Número máximo de horários a retornar
            
        Returns:
            Lista de datetime com horários disponíveis
        """
        available_slots = []
        
        # Obter horários disponíveis para o dia da semana
        weekday = target_date.weekday()
        dias_semana = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
        dia_nome = dias_semana[weekday]
        
        horarios_disponiveis = self.clinic_info.get('horarios_disponiveis', {})
        horarios_do_dia = horarios_disponiveis.get(dia_nome, [])
        
        if not horarios_do_dia:
            return []
        
        # Buscar consultas já agendadas no banco - USAR FORMATO STRING
        target_date_str = target_date.strftime('%Y%m%d')  # "20251015"
        
        existing_appointments = db.query(Appointment).filter(
            Appointment.appointment_date == target_date_str,  # Comparação STRING
            Appointment.status == AppointmentStatus.AGENDADA  # Apenas consultas ativas
        ).all()
        
        # Gerar slots baseados na lista de horários fixos
        for horario_str in horarios_do_dia:
            # Limitar quantidade se especificado
            if limit is not None and len(available_slots) >= limit:
                break
            
            # Converter horário string para datetime
            hora, minuto = map(int, horario_str.split(':'))
            slot_datetime = target_date.replace(hour=hora, minute=minuto, second=0, microsecond=0)
            
            # Verificar se o slot é válido (data não passada, dia da semana válido)
            is_valid, _ = self.is_valid_appointment_date(slot_datetime)
            
            if not is_valid:
                continue
            
            # Verificar conflitos com consultas já agendadas
            has_conflict = False
            slot_end = slot_datetime + timedelta(minutes=consultation_duration)
            
            for appointment in existing_appointments:
                # Converter STRING para datetime
                app_date_str = appointment.appointment_date
                app_date = datetime.strptime(app_date_str, '%Y%m%d').date()
                
                # Converter time string para time object
                if isinstance(appointment.appointment_time, str):
                    app_time = datetime.strptime(appointment.appointment_time, '%H:%M').time()
                else:
                    app_time = appointment.appointment_time
                
                app_start = datetime.combine(app_date, app_time).replace(tzinfo=None)
                app_end = app_start + timedelta(minutes=appointment.duration_minutes)
                
                # Verificar sobreposição
                if not (slot_end <= app_start or slot_datetime >= app_end):
                    has_conflict = True
                    break
            
            if not has_conflict:
                available_slots.append(slot_datetime)
        
        return available_slots
    
    def format_available_slots_message(self, slots: List[datetime], target_date: datetime = None) -> str:
        """
        Formata lista de horários disponíveis em mensagem amigável.
        
        Args:
            slots: Lista de datetime
            target_date: Data alvo para adicionar contexto (opcional)
            
        Returns:
            Mensagem formatada
        """
        if not slots:
            return "Infelizmente não há horários disponíveis para esse dia. Poderia me informar outra data?"
        
        message = ""
        
        # Adicionar contexto do dia se fornecido
        if target_date:
            dias_semana = ['segunda-feira', 'terça-feira', 'quarta-feira', 
                           'quinta-feira', 'sexta-feira', 'sábado', 'domingo']
            dia_nome = dias_semana[target_date.weekday()]
            
            # Buscar horário de funcionamento
            horarios = self.clinic_info.get('horario_funcionamento', {})
            dias_semana_key = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
            horario_dia = horarios.get(dias_semana_key[target_date.weekday()], "FECHADO")
            
            message += f"📅 {target_date.strftime('%d/%m/%Y')} é {dia_nome}\n"
            message += f"🕒 Horário de funcionamento: {horario_dia}\n"
            message += f"⏱️ Duração da consulta: 1 hora\n\n"
        
        message += "✅ Horários disponíveis:\n\n"
        
        # Agrupar horários consecutivos em faixas
        grouped_slots = self._group_consecutive_slots(slots)
        
        for i, (start, end) in enumerate(grouped_slots, 1):
            if start == end:
                message += f"{i}. {start.strftime('%H:%M')}\n"
            else:
                message += f"{i}. {start.strftime('%H:%M')} às {end.strftime('%H:%M')}\n"
        
        message += "\nEscolha o horário desejado informando o número da opção."
        return message
    
    def _group_consecutive_slots(self, slots: List[datetime]) -> List[Tuple[datetime, datetime]]:
        """
        Agrupa horários consecutivos em faixas.
        
        Exemplo:
        [08:00, 08:05, 08:10, 08:15] → [(08:00, 08:15)]
        [08:00, 08:05, 10:00, 10:05] → [(08:00, 08:05), (10:00, 10:05)]
        
        Args:
            slots: Lista de datetime ordenada
            
        Returns:
            Lista de tuplas (início, fim) das faixas
        """
        if not slots:
            return []
        
        grouped = []
        current_start = slots[0]
        current_end = slots[0]
        
        for i in range(1, len(slots)):
            # Se o próximo slot é 5 min após o atual, estender faixa
            if slots[i] == current_end + timedelta(minutes=5):
                current_end = slots[i]
            else:
                # Nova faixa
                grouped.append((current_start, current_end))
                current_start = slots[i]
                current_end = slots[i]
        
        # Adicionar última faixa
        grouped.append((current_start, current_end))
        return grouped
    
    def check_slot_availability(
        self,
        target_datetime: datetime,
        consultation_duration: int,
        db: Session
    ) -> bool:
        """
        Verifica se um horário específico está disponível.
        
        Args:
            target_datetime: Data e hora exata da consulta desejada
            consultation_duration: Duração da consulta em minutos
            db: Sessão do banco de dados
            
        Returns:
            True se disponível, False se conflita
        """
        # 1. Validar se está dentro do horário de funcionamento e se é um horário fixo válido
        is_valid, error_msg = self.is_valid_appointment_date(target_datetime)
        if not is_valid:
            return False
        
        # 2. Validar que minutos são 00 (horários fixos sempre têm minutos == 0)
        if target_datetime.minute != 0:
            return False
        
        # 3. Buscar consultas do dia - USAR FORMATO COM HÍFEN
        target_date_str = target_datetime.strftime('%Y%m%d')  # Formato YYYYMMDD "20251022"
        
        existing_appointments = db.query(Appointment).filter(
            Appointment.appointment_date == target_date_str,
            Appointment.status == AppointmentStatus.AGENDADA
        ).all()
        
        # 4. Calcular fim da nova consulta
        slot_end = target_datetime + timedelta(minutes=consultation_duration)
        
        # 5. Verificar conflitos - CONVERTER STRINGS PARA DATETIME
        had_errors = False
        for appointment in existing_appointments:
            try:
                # Converter appointment_date para formato DD/MM/YYYY
                app_date_str = appointment.appointment_date
                if len(app_date_str) == 8 and app_date_str.isdigit():
                    # Formato YYYYMMDD -> DD/MM/YYYY
                    app_date_str = f"{app_date_str[6:8]}/{app_date_str[4:6]}/{app_date_str[:4]}"
                
                # Converter string para datetime
                app_date = parse_date_br(app_date_str)
                
                # Garantir conversão correta de appointment_time
                if isinstance(appointment.appointment_time, time):
                    # Já é time object, usar direto
                    app_time = appointment.appointment_time
                else:
                    # É string - remover segundos se existir (ex: "15:00:00" → "15:00")
                    app_time_str = str(appointment.appointment_time)
                    if app_time_str.count(':') == 2:
                        # Tem segundos, remover
                        app_time_str = ':'.join(app_time_str.split(':')[:2])
                    app_time = datetime.strptime(app_time_str, '%H:%M').time()
                
                app_start = datetime.combine(app_date.date(), app_time).replace(tzinfo=None)
                app_end = app_start + timedelta(minutes=appointment.duration_minutes)
                
                # Verificar sobreposição: novo slot NÃO deve sobrepor consulta existente
                if not (slot_end <= app_start or target_datetime >= app_end):
                    logger.info(f"⚠️ Conflito encontrado: Nova consulta {target_datetime.strftime('%H:%M')} conflita com consulta existente {app_start.strftime('%H:%M')}-{app_end.strftime('%H:%M')}")
                    return False
                    
            except Exception as e:
                logger.error(f"Erro ao verificar conflito de agendamento: {str(e)}")
                logger.error(f"  appointment_time: {appointment.appointment_time} (type: {type(appointment.appointment_time)})")
                had_errors = True
                # NÃO fazer continue - marcar erro mas continuar verificando
        
        # Se houve erros, rejeitar por segurança
        if had_errors:
            logger.error("⛔ Rejeitando horário por segurança devido a erros na verificação")
            return False
        
        return True

# Instância global
appointment_rules = AppointmentRules()

