#!/usr/bin/env python3
"""
Script para investigar o problema de timezone no PostgreSQL.
"""
import os
import sys
from datetime import datetime, date, time
import pytz
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Adicionar o diretório app ao path
sys.path.append('app')

from app.simple_config import settings
from app.models import Appointment, AppointmentStatus

def test_timezone_issue():
    """Testa o problema de timezone diretamente no banco"""
    
    print("🔍 INVESTIGAÇÃO DE TIMEZONE")
    print("=" * 50)
    
    # 1. Verificar configuração do banco
    print(f"📊 DATABASE_URL: {settings.database_url}")
    print(f"🌍 TIMEZONE: {settings.timezone}")
    
    # 2. Criar engine
    engine = create_engine(settings.database_url)
    SessionLocal = sessionmaker(bind=engine)
    
    with SessionLocal() as db:
        # 3. Verificar timezone do PostgreSQL
        try:
            result = db.execute(text("SHOW timezone;"))
            pg_timezone = result.scalar()
            print(f"🗄️ PostgreSQL timezone: {pg_timezone}")
        except Exception as e:
            print(f"❌ Erro ao verificar timezone do PostgreSQL: {e}")
        
        # 4. Testar inserção direta
        print("\n🧪 TESTE DE INSERÇÃO DIRETA")
        print("-" * 30)
        
        # Criar datetime local
        tz_brazil = pytz.timezone(settings.timezone)
        test_date = date(2025, 10, 23)
        test_time = time(15, 0, 0)
        
        print(f"📅 Data de teste: {test_date}")
        print(f"⏰ Horário de teste: {test_time}")
        
        # Criar appointment de teste
        test_appointment = Appointment(
            patient_name="Teste Timezone",
            patient_phone="555123456789",
            patient_birth_date="01/01/2000",
            appointment_date=test_date,
            appointment_time=test_time,
            duration_minutes=60,
            status=AppointmentStatus.AGENDADA
        )
        
        try:
            db.add(test_appointment)
            db.commit()
            print("✅ Appointment inserido com sucesso!")
            
            # Verificar o que foi salvo
            saved_appointment = db.query(Appointment).filter(
                Appointment.patient_name == "Teste Timezone"
            ).first()
            
            if saved_appointment:
                print(f"💾 Dados salvos:")
                print(f"   appointment_date: {saved_appointment.appointment_date}")
                print(f"   appointment_time: {saved_appointment.appointment_time}")
                print(f"   Tipo appointment_date: {type(saved_appointment.appointment_date)}")
                print(f"   Tipo appointment_time: {type(saved_appointment.appointment_time)}")
                
                # Verificar se há diferença
                if saved_appointment.appointment_date != test_date:
                    print(f"❌ PROBLEMA: Data salva ({saved_appointment.appointment_date}) != Data esperada ({test_date})")
                else:
                    print(f"✅ Data salva corretamente!")
                    
                if saved_appointment.appointment_time != test_time:
                    print(f"❌ PROBLEMA: Horário salvo ({saved_appointment.appointment_time}) != Horário esperado ({test_time})")
                else:
                    print(f"✅ Horário salvo corretamente!")
                    
                # Limpar teste
                db.delete(saved_appointment)
                db.commit()
                print("🧹 Appointment de teste removido")
            else:
                print("❌ Não foi possível recuperar o appointment salvo")
                
        except Exception as e:
            print(f"❌ Erro ao inserir appointment: {e}")
            db.rollback()
        
        # 5. Testar query SQL direta
        print("\n🔍 TESTE DE QUERY SQL DIRETA")
        print("-" * 30)
        
        try:
            # Inserir via SQL direto
            db.execute(text("""
                INSERT INTO appointments 
                (patient_name, patient_phone, patient_birth_date, appointment_date, appointment_time, duration_minutes, status, created_at, updated_at)
                VALUES 
                ('Teste SQL', '555987654321', '01/01/2000', '2025-10-23', '15:00:00', 60, 'agendada', NOW(), NOW())
            """))
            db.commit()
            print("✅ Inserção SQL direta realizada!")
            
            # Verificar o que foi salvo
            result = db.execute(text("""
                SELECT appointment_date, appointment_time 
                FROM appointments 
                WHERE patient_name = 'Teste SQL'
            """))
            
            row = result.fetchone()
            if row:
                print(f"💾 Dados salvos via SQL:")
                print(f"   appointment_date: {row[0]} (tipo: {type(row[0])})")
                print(f"   appointment_time: {row[1]} (tipo: {type(row[1])})")
                
                # Limpar
                db.execute(text("DELETE FROM appointments WHERE patient_name = 'Teste SQL'"))
                db.commit()
                print("🧹 Dados de teste SQL removidos")
            else:
                print("❌ Não foi possível recuperar dados via SQL")
                
        except Exception as e:
            print(f"❌ Erro no teste SQL: {e}")
            db.rollback()

if __name__ == "__main__":
    test_timezone_issue()
