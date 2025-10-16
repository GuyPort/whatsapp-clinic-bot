"""
Script de migração de SQLite para PostgreSQL
Uso: python migrate_to_postgres.py

Este script migra dados existentes do SQLite local para PostgreSQL no Railway.
"""
import sqlite3
import sys
import os
from datetime import datetime

# Adicionar app ao path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal, init_db
from app.models import Patient, Appointment, ConversationContext, AppointmentStatus, ConversationState


def migrate():
    """Migra dados do SQLite para PostgreSQL"""
    print("=" * 60)
    print("🔄 Iniciando migração SQLite → PostgreSQL")
    print("=" * 60)
    
    # Verificar se existe banco SQLite
    sqlite_path = 'data/appointments.db'
    if not os.path.exists(sqlite_path):
        print("✅ Nenhum banco SQLite encontrado. Pulando migração.")
        print("   As tabelas serão criadas vazias no PostgreSQL.")
        init_db()
        return
    
    try:
        sqlite_conn = sqlite3.connect(sqlite_path)
        sqlite_conn.row_factory = sqlite3.Row  # Permite acessar colunas por nome
        cursor = sqlite_conn.cursor()
        
        # Contar registros
        cursor.execute("SELECT COUNT(*) FROM patients")
        patient_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM appointments")
        appointment_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM conversation_contexts")
        conversation_count = cursor.fetchone()[0]
        
        print(f"\n📊 Dados encontrados no SQLite:")
        print(f"   - Pacientes: {patient_count}")
        print(f"   - Consultas: {appointment_count}")
        print(f"   - Conversas: {conversation_count}")
        
        if patient_count == 0 and appointment_count == 0:
            print("\n✅ Nenhum dado para migrar.")
            sqlite_conn.close()
            init_db()
            return
        
        print(f"\n🚀 Iniciando migração...")
        
        # Criar tabelas no PostgreSQL
        print("   1. Criando tabelas no PostgreSQL...")
        init_db()
        
        # Criar sessão PostgreSQL
        pg_session = SessionLocal()
        
        try:
            # Migrar pacientes
            if patient_count > 0:
                print(f"   2. Migrando {patient_count} pacientes...")
                cursor.execute("SELECT * FROM patients")
                patients = cursor.fetchall()
                
                for row in patients:
                    patient = Patient(
                        id=row['id'],
                        phone=row['phone'],
                        name=row['name'],
                        birth_date=row['birth_date'],
                        created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else datetime.utcnow(),
                        updated_at=datetime.fromisoformat(row['updated_at']) if row['updated_at'] else datetime.utcnow()
                    )
                    pg_session.add(patient)
                
                pg_session.commit()
                print(f"      ✅ {patient_count} pacientes migrados")
            
            # Migrar consultas
            if appointment_count > 0:
                print(f"   3. Migrando {appointment_count} consultas...")
                cursor.execute("SELECT * FROM appointments")
                appointments = cursor.fetchall()
                
                for row in appointments:
                    appointment = Appointment(
                        id=row['id'],
                        patient_id=row['patient_id'],
                        appointment_date=datetime.fromisoformat(row['appointment_date']).date(),
                        appointment_time=datetime.strptime(row['appointment_time'], '%H:%M:%S').time(),
                        consultation_type=row.get('consultation_type', 'Consulta Geral'),
                        duration_minutes=row.get('duration_minutes', 30),
                        status=AppointmentStatus(row['status']),
                        notes=row['notes'],
                        created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else datetime.utcnow(),
                        updated_at=datetime.fromisoformat(row['updated_at']) if row['updated_at'] else datetime.utcnow()
                    )
                    pg_session.add(appointment)
                
                pg_session.commit()
                print(f"      ✅ {appointment_count} consultas migradas")
            
            # Migrar contextos de conversa (opcional, podem ser recriados)
            if conversation_count > 0:
                print(f"   4. Migrando {conversation_count} conversas...")
                cursor.execute("SELECT * FROM conversation_contexts")
                conversations = cursor.fetchall()
                
                for row in conversations:
                    context = ConversationContext(
                        id=row['id'],
                        phone=row['phone'],
                        patient_id=row['patient_id'],
                        state=ConversationState(row['state']),
                        context_data=row['context_data'],
                        last_message_at=datetime.fromisoformat(row['last_message_at']) if row['last_message_at'] else None,
                        message_count=row['message_count'],
                        created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else datetime.utcnow(),
                        updated_at=datetime.fromisoformat(row['updated_at']) if row['updated_at'] else datetime.utcnow()
                    )
                    pg_session.add(context)
                
                pg_session.commit()
                print(f"      ✅ {conversation_count} conversas migradas")
            
            print("\n" + "=" * 60)
            print("✅ Migração concluída com sucesso!")
            print("=" * 60)
            print(f"\n📊 Resumo:")
            print(f"   ✅ {patient_count} pacientes")
            print(f"   ✅ {appointment_count} consultas")
            print(f"   ✅ {conversation_count} conversas")
            print(f"\n💾 Dados agora estão no PostgreSQL do Railway")
            print(f"🔒 Backup do SQLite mantido em: {sqlite_path}")
            
        except Exception as e:
            pg_session.rollback()
            print(f"\n❌ Erro durante migração: {str(e)}")
            raise
        finally:
            pg_session.close()
            sqlite_conn.close()
            
    except Exception as e:
        print(f"\n❌ Erro ao acessar banco SQLite: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        migrate()
    except KeyboardInterrupt:
        print("\n\n⚠️  Migração interrompida pelo usuário")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Erro fatal: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

