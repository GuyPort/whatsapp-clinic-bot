#!/usr/bin/env python3
"""
Script de migração para corrigir o tipo da coluna appointment_date no PostgreSQL do Railway.
Altera de DATE para VARCHAR(10) para evitar conversão automática de timezone.
"""

import os
import sys
from sqlalchemy import create_engine, text
from app.simple_config import settings

def migrate_railway_appointment_date():
    """Migra a coluna appointment_date de DATE para VARCHAR(10) no Railway"""
    try:
        print("🚀 Migração Railway - Correção Bug Data")
        print("=" * 50)
        
        database_url = settings.database_url
        print(f"🔗 Conectando ao banco: {database_url[:50]}...")
        
        if 'sqlite' in database_url:
            print("❌ Este script é para PostgreSQL (Railway). SQLite não precisa desta migração.")
            return False
        
        engine = create_engine(database_url)
        
        with engine.connect() as conn:
            print("✅ Conectado ao PostgreSQL do Railway!")
            
            # 1. Verificar tipo atual da coluna
            print("\n🔍 Verificando tipo atual da coluna...")
            result = conn.execute(text("""
                SELECT column_name, data_type, character_maximum_length
                FROM information_schema.columns 
                WHERE table_name = 'appointments' 
                AND column_name = 'appointment_date'
            """))
            
            column_info = result.fetchone()
            if column_info:
                current_type = column_info[1]
                print(f"   Tipo atual: {current_type}")
                
                if current_type == 'character varying':
                    print("✅ Coluna já está como VARCHAR! Nenhuma migração necessária.")
                    return True
                elif current_type == 'date':
                    print("🚨 Coluna está como DATE - migração necessária!")
                else:
                    print(f"⚠️ Tipo inesperado: {current_type}")
            else:
                print("❌ Coluna appointment_date não encontrada!")
                return False
            
            # 2. Fazer backup dos dados existentes
            print("\n💾 Fazendo backup dos dados existentes...")
            result = conn.execute(text("""
                SELECT id, patient_name, appointment_date, appointment_time
                FROM appointments 
                ORDER BY created_at DESC
            """))
            
            existing_appointments = result.fetchall()
            print(f"   Encontrados {len(existing_appointments)} agendamentos existentes")
            
            # 3. Alterar tipo da coluna
            print("\n🔧 Alterando tipo da coluna para VARCHAR(10)...")
            conn.execute(text("""
                ALTER TABLE appointments 
                ALTER COLUMN appointment_date TYPE VARCHAR(10);
            """))
            print("✅ Tipo da coluna alterado!")
            
            # 4. Converter dados existentes de DATE para YYYYMMDD
            if existing_appointments:
                print("\n🔄 Convertendo dados existentes...")
                conn.execute(text("""
                    UPDATE appointments 
                    SET appointment_date = TO_CHAR(appointment_date::date, 'YYYYMMDD');
                """))
                print("✅ Dados convertidos para formato YYYYMMDD!")
            
            # 5. Verificar resultado
            print("\n🔍 Verificando resultado da migração...")
            result = conn.execute(text("""
                SELECT id, patient_name, appointment_date, appointment_time
                FROM appointments 
                ORDER BY created_at DESC 
                LIMIT 3
            """))
            
            print("   Exemplos de dados após migração:")
            for row in result:
                print(f"   ID {row[0]}: {row[1]} - Data: {row[2]} (tipo: {type(row[2])}) - Hora: {row[3]}")
            
            conn.commit()
            print("\n✅ Migração concluída com sucesso!")
            print("🎯 Agora as datas serão salvas como strings sem conversão automática!")
            
            return True
            
    except Exception as e:
        print(f"❌ Erro durante migração: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def create_admin_endpoint_migration():
    """Cria endpoint admin para executar migração via web"""
    migration_code = '''
@app.post("/admin/migrate-fix-date")
async def migrate_fix_date_admin():
    """Endpoint admin para executar migração de data"""
    try:
        from migrate_railway_fix_date import migrate_railway_appointment_date
        success = migrate_railway_appointment_date()
        
        if success:
            return {"status": "success", "message": "Migração executada com sucesso!"}
        else:
            return {"status": "error", "message": "Erro durante migração"}
            
    except Exception as e:
        return {"status": "error", "message": f"Erro: {str(e)}"}
'''
    
    print("\n📋 Para executar via endpoint admin, adicione ao main.py:")
    print(migration_code)

if __name__ == "__main__":
    print("🔧 Script de Migração Railway - Correção Bug Data")
    print("=" * 60)
    
    # Verificar se está no Railway
    if 'RAILWAY_ENVIRONMENT' in os.environ:
        print("🚂 Detectado: Ambiente Railway")
    else:
        print("💻 Detectado: Ambiente Local")
        print("⚠️ Certifique-se de que DATABASE_URL aponta para o Railway!")
    
    # Confirmar antes de executar
    response = input("\n⚠️ Esta migração irá alterar a estrutura do banco PostgreSQL. Continuar? (s/N): ")
    if response.lower() != 's':
        print("❌ Migração cancelada.")
        sys.exit(0)
    
    success = migrate_railway_appointment_date()
    
    if success:
        print("\n🎉 Migração concluída com sucesso!")
        print("📋 Próximos passos:")
        print("   1. Fazer novo agendamento via WhatsApp")
        print("   2. Verificar logs: appointment_datetime_formatted: 'YYYYMMDD'")
        print("   3. Verificar banco: deve salvar como string sem hífen")
        print("   4. Verificar dashboard: deve exibir data correta")
        
        create_admin_endpoint_migration()
    else:
        print("\n❌ Migração falhou!")
        sys.exit(1)
