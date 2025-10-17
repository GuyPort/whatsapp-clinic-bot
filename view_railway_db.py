"""
Script para visualizar dados do banco no Railway.
Execute: python view_railway_db.py
"""
import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Adicionar o diretório raiz ao path
sys.path.insert(0, '.')

from app.simple_config import DATABASE_URL
from app.models import Appointment, AppointmentStatus

def view_database():
    """Visualiza todos os dados da tabela appointments"""
    
    print("🗄️ VISUALIZADOR DO BANCO DE DADOS")
    print("=" * 50)
    print(f"📊 Banco: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else 'SQLite'}")
    print("=" * 50)
    
    try:
        # Criar engine
        engine = create_engine(DATABASE_URL)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        
        with SessionLocal() as db:
            # Contar total de registros
            total = db.query(Appointment).count()
            print(f"📈 Total de consultas: {total}")
            
            if total == 0:
                print("📭 Nenhuma consulta encontrada.")
                return
            
            print("\n📋 TODAS AS CONSULTAS:")
            print("-" * 80)
            
            # Buscar todas as consultas
            appointments = db.query(Appointment).order_by(Appointment.created_at.desc()).all()
            
            for apt in appointments:
                print(f"🆔 ID: {apt.id}")
                print(f"👤 Paciente: {apt.patient_name}")
                print(f"📞 Telefone: {apt.patient_phone}")
                print(f"🎂 Nascimento: {apt.patient_birth_date}")
                print(f"📅 Data: {apt.appointment_date}")
                print(f"⏰ Horário: {apt.appointment_time}")
                print(f"⏱️ Duração: {apt.duration_minutes} min")
                print(f"📊 Status: {apt.status.value}")
                
                if apt.notes:
                    print(f"📝 Observações: {apt.notes}")
                
                if apt.cancelled_at:
                    print(f"❌ Cancelada em: {apt.cancelled_at}")
                    print(f"💬 Motivo: {apt.cancelled_reason}")
                
                print(f"📅 Criada em: {apt.created_at}")
                print(f"🔄 Atualizada em: {apt.updated_at}")
                print("-" * 80)
            
            # Estatísticas por status
            print("\n📊 ESTATÍSTICAS POR STATUS:")
            print("-" * 30)
            
            for status in AppointmentStatus:
                count = db.query(Appointment).filter(Appointment.status == status).count()
                print(f"{status.value.upper()}: {count}")
            
            # Consultas recentes (últimas 5)
            print(f"\n🕒 ÚLTIMAS 5 CONSULTAS:")
            print("-" * 50)
            
            recent = db.query(Appointment).order_by(Appointment.created_at.desc()).limit(5).all()
            for apt in recent:
                print(f"• {apt.patient_name} - {apt.appointment_date} {apt.appointment_time} ({apt.status.value})")
                
    except Exception as e:
        print(f"❌ Erro ao acessar banco: {str(e)}")
        return False
    
    print("\n✅ Visualização concluída!")
    return True

if __name__ == "__main__":
    try:
        view_database()
    except KeyboardInterrupt:
        print("\n\n👋 Visualização interrompida")
    except Exception as e:
        print(f"\n❌ Erro: {str(e)}")
        import traceback
        traceback.print_exc()
