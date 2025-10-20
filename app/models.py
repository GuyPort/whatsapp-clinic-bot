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
    appointment_date = Column(String(10), nullable=False, index=True)  # Data da consulta (DD/MM/AAAA)
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
    
    def __repr__(self):
        return f"<Appointment(id={self.id}, patient='{self.patient_name}', date='{self.appointment_date}', time='{self.appointment_time}', status='{self.status.value}')>"


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
    
    # Status e controle
    status = Column(String(20), nullable=False, default="active")  # "active" | "paused_human" | "expired"
    paused_until = Column(DateTime, nullable=True)  # Quando a pausa para humano expira
    
    # Timestamps
    last_activity = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    
    def __repr__(self):
        return f"<ConversationContext(phone='{self.phone}', status='{self.status}', messages={len(self.messages)})>"
