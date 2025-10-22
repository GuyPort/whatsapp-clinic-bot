"""
Modelos de banco de dados para o bot da clínica.
Versão completa com todos os campos necessários para o agente Claude.
"""
from datetime import datetime, date, time
from sqlalchemy import Column, Integer, String, DateTime, Date, Time, Text, Index, Enum, JSON
from sqlalchemy.orm import declarative_base
import enum

Base = declarative_base()


class AppointmentStatus(enum.Enum):
    """Status possíveis de uma consulta"""
    AGENDADA = "agendada"
    CANCELADA = "cancelada"
    REALIZADA = "realizada"


class Appointment(Base):
    """
    Modelo completo de consulta agendada com todos os campos necessários.
    """
    __tablename__ = "appointments"
    
    # ID único
    id = Column(Integer, primary_key=True, index=True)
    
    # Dados do paciente (coletados via WhatsApp)
    patient_name = Column(String(200), nullable=False, index=True)  # Nome do paciente
    patient_phone = Column(String(20), nullable=False, index=True)  # Telefone WhatsApp normalizado
    patient_birth_date = Column(String(10), nullable=False)  # Data de nascimento (DD/MM/AAAA)
    
    # Dados da consulta - USAR STRING para evitar problemas de timezone
    appointment_date = Column(String(10), nullable=False, index=True)  # Data da consulta (YYYYMMDD)
    appointment_time = Column(String(5), nullable=False)  # Horário da consulta (HH:MM)
    duration_minutes = Column(Integer, default=60, nullable=False)  # Duração em minutos
    
    # Status e controle
    status = Column(Enum(AppointmentStatus), default=AppointmentStatus.AGENDADA, nullable=False, index=True)
    notes = Column(Text, nullable=True)  # Observações adicionais
    
    # Campos de cancelamento
    cancelled_at = Column(DateTime, nullable=True)  # Quando foi cancelada
    cancelled_reason = Column(String(500), nullable=True)  # Motivo do cancelamento
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Índices para otimizar queries do agente
    __table_args__ = (
        Index('idx_appointment_date_time_status', 'appointment_date', 'appointment_time', 'status'),
        Index('idx_patient_phone_status', 'patient_phone', 'status'),
        Index('idx_status_created', 'status', 'created_at'),
    )
    
    def __init__(self, **kwargs):
        # Garantir que appointment_date seja sempre string
        if 'appointment_date' in kwargs and kwargs['appointment_date']:
            kwargs['appointment_date'] = str(kwargs['appointment_date'])
        super().__init__(**kwargs)
    
    def __repr__(self):
        return f"<Appointment(id={self.id}, patient='{self.patient_name}', date='{self.appointment_date}', time='{self.appointment_time}', status='{self.status.value}')>"


class PausedContact(Base):
    """
    Contatos pausados para atendimento humano.
    Gerencia pausas temporárias quando usuário solicita atendimento humano.
    """
    __tablename__ = "paused_contacts"
    
    # Chave primária: número de telefone do WhatsApp
    phone = Column(String(20), primary_key=True, index=True)
    
    # Controle de pausa
    paused_until = Column(DateTime, nullable=False, index=True)  # Quando a pausa expira
    reason = Column(String(100), nullable=True)  # Motivo da pausa (opcional)
    
    # Timestamps
    paused_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    
    def __repr__(self):
        return f"<PausedContact(phone='{self.phone}', paused_until='{self.paused_until}')>"


class ConversationContext(Base):
    """
    Contexto de conversa para manter histórico entre mensagens do WhatsApp.
    Permite que o bot "lembre" do que foi dito anteriormente.
    """
    __tablename__ = "conversation_contexts"
    
    # Chave primária: número de telefone do WhatsApp
    phone = Column(String(20), primary_key=True, index=True)
    
    # Histórico de mensagens (JSON array)
    messages = Column(JSON, nullable=False, default=list)  # [{role, content, timestamp}]
    
    # Estado atual do fluxo
    current_flow = Column(String(50), nullable=True)  # "agendamento" | "cancelamento" | "duvidas"
    flow_data = Column(JSON, nullable=False, default=dict)  # Dados coletados no fluxo
    # Estrutura esperada em flow_data:
    # {
    #     "patient_name": "...",
    #     "patient_birth_date": "...", 
    #     "appointment_date": "...",
    #     "appointment_time": "...",
    #     "pending_confirmation": True/False  # Flag para confirmação pendente
    # }
    
    # Status e controle
    status = Column(String(20), nullable=False, default="active")  # "active" | "expired"
    
    # Timestamps
    last_activity = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    
    def __repr__(self):
        return f"<ConversationContext(phone='{self.phone}', status='{self.status}', messages={len(self.messages)})>"
