"""
Script de migração do banco de dados.
Atualiza a estrutura para suportar o agente Claude com Tools.
"""
import os
import sys
from datetime import datetime
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker

# Adicionar o diretório raiz ao path
sys.path.insert(0, '.')

from app.models import Base, Appointment, AppointmentStatus
from app.simple_config import settings


def backup_existing_data():
    """Faz backup dos dados existentes antes da migração"""
    try:
        engine = create_engine(settings.database_url)
        
        # Verificar se a tabela appointments existe
        inspector = inspect(engine)
        if 'appointments' not in inspector.get_table_names():
            print("✅ Tabela 'appointments' não existe. Migração será uma criação limpa.")
            return []
        
        # Buscar dados existentes
        with engine.connect() as conn:
            result = conn.execute(text("SELECT * FROM appointments"))
            existing_data = result.fetchall()
            
            if existing_data:
                print(f"📦 Encontrados {len(existing_data)} registros existentes.")
                print("⚠️  ATENÇÃO: Estes dados serão perdidos na migração!")
                print("   Se precisar preservar, faça backup manual antes de continuar.")
                return existing_data
            else:
                print("✅ Tabela vazia. Migração será limpa.")
                return []
                
    except Exception as e:
        print(f"❌ Erro ao verificar dados existentes: {e}")
        return []


def drop_existing_tables():
    """Remove tabelas existentes para recriar com nova estrutura"""
    try:
        engine = create_engine(settings.database_url)
        
        # Verificar tabelas existentes
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()
        
        if existing_tables:
            print(f"🗑️  Removendo {len(existing_tables)} tabelas existentes...")
            
            with engine.connect() as conn:
                # Remover tabelas em ordem reversa (dependências)
                for table_name in reversed(existing_tables):
                    print(f"   - Removendo tabela: {table_name}")
                    conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
                    conn.commit()
            
            print("✅ Tabelas removidas com sucesso.")
        else:
            print("✅ Nenhuma tabela existente encontrada.")
            
    except Exception as e:
        print(f"❌ Erro ao remover tabelas: {e}")
        raise


def create_new_tables():
    """Cria as novas tabelas com a estrutura atualizada"""
    try:
        engine = create_engine(settings.database_url)
        
        print("🏗️  Criando novas tabelas...")
        
        # Criar todas as tabelas definidas nos models
        Base.metadata.create_all(bind=engine)
        
        print("✅ Tabelas criadas com sucesso!")
        
        # Verificar tabelas criadas
        inspector = inspect(engine)
        new_tables = inspector.get_table_names()
        print(f"📋 Tabelas criadas: {', '.join(new_tables)}")
        
        # Verificar estrutura da tabela appointments
        if 'appointments' in new_tables:
            columns = inspector.get_columns('appointments')
            print(f"📊 Colunas da tabela 'appointments':")
            for col in columns:
                print(f"   - {col['name']}: {col['type']}")
        
    except Exception as e:
        print(f"❌ Erro ao criar tabelas: {e}")
        raise


def test_database():
    """Testa se o banco está funcionando corretamente"""
    try:
        engine = create_engine(settings.database_url)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        
        print("🧪 Testando banco de dados...")
        
        with SessionLocal() as db:
            # Testar inserção
            test_appointment = Appointment(
                patient_name="Teste Migração",
                patient_phone="5511999999999",
                patient_birth_date="01/01/1990",
                appointment_date=datetime.now().date(),
                appointment_time=datetime.now().time(),
                status=AppointmentStatus.AGENDADA,
                notes="Teste de migração"
            )
            
            db.add(test_appointment)
            db.commit()
            
            # Testar consulta
            appointments = db.query(Appointment).all()
            print(f"✅ Teste bem-sucedido! {len(appointments)} consulta(s) encontrada(s).")
            
            # Limpar teste
            db.delete(test_appointment)
            db.commit()
            print("🧹 Dados de teste removidos.")
            
    except Exception as e:
        print(f"❌ Erro no teste do banco: {e}")
        raise


def main():
    """Função principal da migração"""
    print("🚀 MIGRAÇÃO DO BANCO DE DADOS")
    print("=" * 50)
    print(f"📅 Data: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"🗄️  Banco: {settings.database_url}")
    print("=" * 50)
    
    try:
        # 1. Verificar dados existentes
        print("\n1️⃣ Verificando dados existentes...")
        existing_data = backup_existing_data()
        
        # 2. Confirmar migração
        if existing_data:
            response = input("\n⚠️  Dados existentes serão perdidos. Continuar? (s/N): ").strip().lower()
            if response not in ['s', 'sim', 'y', 'yes']:
                print("❌ Migração cancelada pelo usuário.")
                return
        
        # 3. Remover tabelas existentes
        print("\n2️⃣ Removendo tabelas existentes...")
        drop_existing_tables()
        
        # 4. Criar novas tabelas
        print("\n3️⃣ Criando novas tabelas...")
        create_new_tables()
        
        # 5. Testar banco
        print("\n4️⃣ Testando banco de dados...")
        test_database()
        
        print("\n" + "=" * 50)
        print("✅ MIGRAÇÃO CONCLUÍDA COM SUCESSO!")
        print("=" * 50)
        print("📋 Próximos passos:")
        print("   1. Testar o bot com: python test_bot.py")
        print("   2. Verificar dashboard em: http://localhost:8000/dashboard")
        print("   3. Fazer deploy se tudo estiver funcionando")
        
    except Exception as e:
        print(f"\n❌ ERRO NA MIGRAÇÃO: {e}")
        print("🔧 Verifique as configurações e tente novamente.")
        sys.exit(1)


if __name__ == "__main__":
    main()
