"""
Modelos de banco de dados para o bot da clínica.
Versão simplificada - apenas uma tabela de consultas.
"""
from datetime import datetime, date, time
from sqlalchemy import Column, Integer, String, DateTime, Date, Time, Index
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Appointment(Base):
    """
    Modelo simplificado de consulta agendada.
    Única tabela com apenas os campos essenciais.
    """
    __tablename__ = "appointments"
    
    # ID único
    id = Column(Integer, primary_key=True, index=True)
    
    # Dados essenciais da consulta
    patient_name = Column(String(200), nullable=False, index=True)  # Nome do paciente
    patient_birth_date = Column(String(10), nullable=False)  # Data de nascimento (DD/MM/AAAA)
    appointment_date = Column(Date, nullable=False, index=True)  # Dia da consulta
    appointment_time = Column(Time, nullable=False)  # Horário da consulta
    
    # Timestamp de criação
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Índice composto para otimizar queries de conflito de horários
    __table_args__ = (
        Index('idx_appointment_date_time', 'appointment_date', 'appointment_time'),
    )
    
    def __repr__(self):
        return f"<Appointment(id={self.id}, patient='{self.patient_name}', date='{self.appointment_date}', time='{self.appointment_time}')>"
