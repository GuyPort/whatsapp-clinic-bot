"""
Modelos de banco de dados para o bot da clínica.
"""
from datetime import datetime, date, time
from sqlalchemy import Column, Integer, String, DateTime, Date, Time, ForeignKey, Text, Enum, Float
from sqlalchemy.orm import relationship, declarative_base
import enum

Base = declarative_base()


class AppointmentStatus(enum.Enum):
    """Status possíveis de uma consulta"""
    SCHEDULED = "scheduled"  # Agendada
    COMPLETED = "completed"  # Realizada
    CANCELLED = "cancelled"  # Cancelada
    NO_SHOW = "no_show"      # Paciente não compareceu


class ConversationState(enum.Enum):
    """Estados possíveis da conversa"""
    IDLE = "idle"                           # Sem conversa ativa
    BOAS_VINDAS = "boas_vindas"             # Mensagem de boas-vindas
    COLETANDO_NOME = "coletando_nome"       # Coletando nome completo
    COLETANDO_NASCIMENTO = "coletando_nascimento"  # Coletando data de nascimento
    MENU_PRINCIPAL = "menu_principal"       # Mostrando menu principal
    MARCAR_CONSULTA = "marcar_consulta"     # Processo de marcar consulta
    PROCESSANDO_AGENDAMENTO = "processando_agendamento"  # Processando data/horário fornecido
    REMARCAR_CANCELAR = "remarcar_cancelar" # Processo de remarcar/cancelar
    TIRAR_DUVIDAS = "tirar_duvidas"        # Processo de tirar dúvidas
    CONFIRMANDO = "confirmando"             # Confirmando agendamento
    FINALIZANDO = "finalizando"             # Finalizando conversa
    CONVERSA_ENCERRADA = "conversa_encerrada"  # Conversa totalmente encerrada
    ESCALATED = "escalated"                 # Escalado para humano


class Patient(Base):
    """Modelo de paciente"""
    __tablename__ = "patients"
    
    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String(20), unique=True, index=True, nullable=False)
    name = Column(String(200), nullable=False)
    birth_date = Column(String(10), nullable=False)  # Formato: DD/MM/AAAA
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relacionamentos
    appointments = relationship("Appointment", back_populates="patient")
    conversations = relationship("ConversationContext", back_populates="patient")
    
    def __repr__(self):
        return f"<Patient(id={self.id}, name='{self.name}', phone='{self.phone}')>"


class Appointment(Base):
    """Modelo de consulta/agendamento"""
    __tablename__ = "appointments"
    
    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    
    # Informações da consulta
    appointment_date = Column(Date, nullable=False, index=True)
    appointment_time = Column(Time, nullable=False)
    
    # Status e rastreamento
    status = Column(Enum(AppointmentStatus), default=AppointmentStatus.SCHEDULED, nullable=False)
    
    # Notas e observações
    notes = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relacionamentos
    patient = relationship("Patient", back_populates="appointments")
    
    def __repr__(self):
        return f"<Appointment(id={self.id}, patient_id={self.patient_id}, date='{self.appointment_date}', status='{self.status}')>"


class ConversationContext(Base):
    """Contexto da conversa com o paciente"""
    __tablename__ = "conversation_contexts"
    
    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String(20), index=True, nullable=False)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True)
    
    # Estado da conversa
    state = Column(Enum(ConversationState), default=ConversationState.IDLE, nullable=False)
    context_data = Column(Text, nullable=True)  # JSON com dados temporários da conversa
    
    # Rastreamento de mensagens
    last_message_at = Column(DateTime, default=datetime.utcnow)
    message_count = Column(Integer, default=0)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relacionamentos
    patient = relationship("Patient", back_populates="conversations")
    
    def __repr__(self):
        return f"<ConversationContext(id={self.id}, phone='{self.phone}', state='{self.state}')>"

