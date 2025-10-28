"""
Regras e validações para agendamento de consultas.
"""
from datetime import datetime, timedelta, time
from typing import List, Dict, Any, Tuple
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
        
        # 2. Verificar dia da semana
        weekday = appointment_date.weekday()  # 0=segunda, 6=domingo
        
        # Domingo sempre fechado
        if weekday == 6:
            return False, "A clínica não atende aos domingos."
        
        # 4. Verificar horário de funcionamento
        horarios = self.clinic_info.get('horario_funcionamento', {})
        dias_semana = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
        dia_nome = dias_semana[weekday]
        
        horario_dia = horarios.get(dia_nome, "FECHADO")
        if horario_dia == "FECHADO":
            return False, f"A clínica não atende às {dias_semana[weekday]}s."
        
        # 5. Verificar se está dentro do horário de funcionamento
        if '-' in horario_dia:
            inicio_str, fim_str = horario_dia.split('-')
            inicio_h, inicio_m = map(int, inicio_str.split(':'))
            fim_h, fim_m = map(int, fim_str.split(':'))
            
            inicio = time(inicio_h, inicio_m)
            fim = time(fim_h, fim_m)
            
            hora_consulta = appointment_date.time()
            
            if not (inicio <= hora_consulta <= fim):
                return False, f"Horário fora do expediente. Horário de atendimento: {horario_dia}"
        
        # 6. Sábado: verificar se não é tarde
        if weekday == 5:  # Sábado
            ultima_hora_sabado = self.rules.get('horario_ultima_consulta_sabado', '11:30')
            h, m = map(int, ultima_hora_sabado.split(':'))
            if appointment_date.time() > time(h, m):
                return False, f"No sábado, a última consulta é às {ultima_hora_sabado}."
        
        return True, ""
    
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
        
        # Definir horário de início e fim para o dia
        weekday = target_date.weekday()
        horarios = self.clinic_info.get('horario_funcionamento', {})
        dias_semana = ['segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
        dia_nome = dias_semana[weekday]
        horario_dia = horarios.get(dia_nome, "FECHADO")
        
        if horario_dia == "FECHADO":
            return []
        
        # Parse horário
        inicio_str, fim_str = horario_dia.split('-')
        inicio_h, inicio_m = map(int, inicio_str.split(':'))
        fim_h, fim_m = map(int, fim_str.split(':'))
        
        # Criar datetime para início e fim
        start_time = target_date.replace(hour=inicio_h, minute=inicio_m, second=0, microsecond=0)
        end_time = target_date.replace(hour=fim_h, minute=fim_m, second=0, microsecond=0)
        
        # Ajustar para sábado se necessário
        if weekday == 5:
            ultima_hora_sabado = self.rules.get('horario_ultima_consulta_sabado', '11:30')
            h, m = map(int, ultima_hora_sabado.split(':'))
            end_time = target_date.replace(hour=h, minute=m, second=0, microsecond=0)
        
        # Buscar consultas já agendadas no banco - USAR FORMATO STRING
        target_date_str = target_date.strftime('%Y%m%d')  # "20251015"
        
        existing_appointments = db.query(Appointment).filter(
            Appointment.appointment_date == target_date_str,  # Comparação STRING
            Appointment.status == AppointmentStatus.AGENDADA  # Apenas consultas ativas
        ).all()
        
        # Gerar slots usando o intervalo configurado
        current = start_time
        slot_step = 5  # Slots a cada 5 minutos
        
        while current < end_time and (limit is None or len(available_slots) < limit):
            slot_end = current + timedelta(minutes=consultation_duration)
            
            # Verificar se o slot é válido
            is_valid, _ = self.is_valid_appointment_date(current)
            
            if is_valid and slot_end <= end_time:
                # Verificar conflitos com consultas no banco
                has_conflict = False
                
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
                    if not (slot_end <= app_start or current >= app_end):
                        has_conflict = True
                        break
                
                if not has_conflict:
                    available_slots.append(current)
            
            # Avançar para o próximo slot
            current += timedelta(minutes=slot_step)
        
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
    
    def can_modify_appointment(
        self,
        appointment: Appointment,
        patient_name: str,
        patient_birth_date: str
    ) -> Tuple[bool, str]:
        """
        Verifica se um paciente pode modificar (cancelar/remarcar) uma consulta.
        
        Args:
            appointment: Consulta a ser modificada
            patient_name: Nome fornecido pelo paciente
            patient_birth_date: Data de nascimento fornecida
            
        Returns:
            (pode_modificar, mensagem_erro)
        """
        # Verificar se os dados coincidem
        if (appointment.patient.name.lower() != patient_name.lower() or
            appointment.patient.birth_date != patient_birth_date):
            return False, "Os dados fornecidos não correspondem ao agendamento."
        
        # Verificar se a consulta já passou
        if appointment.appointment_date <= now_brazil():
            return False, "Não é possível modificar uma consulta que já passou."
        
        # Verificar se já foi cancelada
        # Como não temos mais status, assumimos que todas as consultas estão ativas
        return True, "Consulta válida."
    
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
        # 1. Validar se está dentro do horário de funcionamento
        is_valid, error_msg = self.is_valid_appointment_date(target_datetime)
        if not is_valid:
            return False
        
        # 2. Validar minutos (deve ser múltiplo de 5)
        if target_datetime.minute % 5 != 0:
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

