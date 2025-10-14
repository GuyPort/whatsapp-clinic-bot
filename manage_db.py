"""
Script de gerenciamento do banco de dados.
"""
import sys
import os
from datetime import datetime
import shutil
import json

sys.path.insert(0, '.')

from app.database import init_db, get_db
from app.models import Patient, Appointment, ConversationContext, AppointmentStatus


def backup_database():
    """Faz backup do banco de dados"""
    db_path = "data/appointments.db"
    
    if not os.path.exists(db_path):
        print("❌ Banco de dados não encontrado!")
        return
    
    # Criar pasta de backup se não existir
    os.makedirs("data/backups", exist_ok=True)
    
    # Nome do backup com timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"data/backups/appointments_backup_{timestamp}.db"
    
    # Copiar arquivo
    shutil.copy2(db_path, backup_path)
    
    print(f"✅ Backup criado: {backup_path}")


def restore_database(backup_file):
    """Restaura banco de dados de um backup"""
    db_path = "data/appointments.db"
    
    if not os.path.exists(backup_file):
        print(f"❌ Arquivo de backup não encontrado: {backup_file}")
        return
    
    # Fazer backup do atual antes de restaurar
    if os.path.exists(db_path):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_backup = f"data/appointments_before_restore_{timestamp}.db"
        shutil.copy2(db_path, temp_backup)
        print(f"⚠️  Backup do banco atual: {temp_backup}")
    
    # Restaurar
    shutil.copy2(backup_file, db_path)
    print(f"✅ Banco restaurado de: {backup_file}")


def show_stats():
    """Mostra estatísticas do banco"""
    print("\n📊 Estatísticas do Banco de Dados")
    print("=" * 50)
    
    try:
        with get_db() as db:
            # Pacientes
            total_patients = db.query(Patient).count()
            print(f"\n👥 Pacientes: {total_patients}")
            
            # Consultas por status
            print("\n📅 Consultas:")
            for status in AppointmentStatus:
                count = db.query(Appointment).filter(
                    Appointment.status == status
                ).count()
                print(f"  - {status.value}: {count}")
            
            total_appointments = db.query(Appointment).count()
            print(f"  - Total: {total_appointments}")
            
            # Conversas
            total_conversations = db.query(ConversationContext).count()
            print(f"\n💬 Conversas: {total_conversations}")
            
            # Próximas consultas
            from app.utils import now_brazil
            now = now_brazil()
            upcoming = db.query(Appointment).filter(
                Appointment.appointment_date > now,
                Appointment.status == AppointmentStatus.SCHEDULED
            ).count()
            print(f"\n🔜 Consultas agendadas: {upcoming}")
            
    except Exception as e:
        print(f"\n❌ Erro ao obter estatísticas: {str(e)}")


def list_upcoming_appointments():
    """Lista próximas consultas"""
    print("\n📅 Próximas Consultas")
    print("=" * 50)
    
    try:
        with get_db() as db:
            from app.utils import now_brazil, format_datetime_br
            now = now_brazil()
            
            appointments = db.query(Appointment).filter(
                Appointment.appointment_date > now,
                Appointment.status == AppointmentStatus.SCHEDULED
            ).order_by(Appointment.appointment_date).limit(10).all()
            
            if not appointments:
                print("\nNenhuma consulta agendada.")
                return
            
            for apt in appointments:
                print(f"\n• ID: {apt.id}")
                print(f"  Paciente: {apt.patient.name}")
                print(f"  Telefone: {apt.patient.phone}")
                print(f"  Data: {format_datetime_br(apt.appointment_date)}")
                print(f"  Tipo: {apt.consultation_type}")
                print(f"  Duração: {apt.duration_minutes} min")
                
    except Exception as e:
        print(f"\n❌ Erro: {str(e)}")


def export_patients():
    """Exporta lista de pacientes para JSON"""
    print("\n📤 Exportando pacientes...")
    
    try:
        with get_db() as db:
            patients = db.query(Patient).all()
            
            data = []
            for patient in patients:
                data.append({
                    "id": patient.id,
                    "name": patient.name,
                    "phone": patient.phone,
                    "birth_date": patient.birth_date,
                    "total_appointments": len(patient.appointments)
                })
            
            # Salvar JSON
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"data/patients_export_{timestamp}.json"
            
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            print(f"✅ Pacientes exportados: {filename}")
            print(f"   Total: {len(data)} pacientes")
            
    except Exception as e:
        print(f"❌ Erro: {str(e)}")


def reset_database():
    """Reseta o banco de dados (CUIDADO!)"""
    print("\n⚠️  ATENÇÃO: Isso vai DELETAR TODOS OS DADOS!")
    confirm = input("Digite 'CONFIRMAR' para continuar: ")
    
    if confirm != "CONFIRMAR":
        print("❌ Operação cancelada.")
        return
    
    db_path = "data/appointments.db"
    
    # Fazer backup antes de deletar
    if os.path.exists(db_path):
        backup_database()
        os.remove(db_path)
    
    # Recriar banco
    init_db()
    print("✅ Banco de dados resetado!")


def main():
    """Menu principal"""
    print("\n🗄️  Gerenciador de Banco de Dados")
    print("=" * 50)
    print("1. Ver estatísticas")
    print("2. Listar próximas consultas")
    print("3. Fazer backup")
    print("4. Restaurar backup")
    print("5. Exportar pacientes (JSON)")
    print("6. Resetar banco (CUIDADO!)")
    print("7. Sair")
    
    choice = input("\nEscolha uma opção: ").strip()
    
    if choice == "1":
        show_stats()
    elif choice == "2":
        list_upcoming_appointments()
    elif choice == "3":
        backup_database()
    elif choice == "4":
        backup_file = input("Caminho do arquivo de backup: ").strip()
        restore_database(backup_file)
    elif choice == "5":
        export_patients()
    elif choice == "6":
        reset_database()
    elif choice == "7":
        print("👋 Até logo!")
        return
    else:
        print("❌ Opção inválida!")
    
    # Perguntar se quer fazer outra operação
    print("\n")
    again = input("Fazer outra operação? (s/n): ").strip().lower()
    if again == 's':
        main()


if __name__ == "__main__":
    try:
        # Inicializar banco se não existir
        if not os.path.exists("data/appointments.db"):
            print("⚠️  Banco não encontrado. Inicializando...")
            init_db()
        
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Operação interrompida")
    except Exception as e:
        print(f"\n❌ Erro: {str(e)}")
        import traceback
        traceback.print_exc()

