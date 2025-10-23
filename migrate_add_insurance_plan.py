#!/usr/bin/env python3
"""
Script de migração para adicionar coluna insurance_plan na tabela appointments.
Executa no Railway via endpoint admin.
"""

import os
import sys
from sqlalchemy import text

def migrate_add_insurance_plan():
    """Adiciona coluna insurance_plan na tabela appointments"""
    try:
        # Importar configurações do banco
        from app.database import get_db
        
        with get_db() as db:
            # Verificar se a coluna já existe
            result = db.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'appointments' 
                AND column_name = 'insurance_plan'
            """))
            
            if result.fetchone():
                print("✅ Coluna 'insurance_plan' já existe na tabela 'appointments'")
                return {"success": True, "message": "Coluna já existe"}
            
            # Adicionar coluna
            db.execute(text("""
                ALTER TABLE appointments 
                ADD COLUMN insurance_plan VARCHAR(50)
            """))
            
            # Atualizar registros existentes com valor padrão
            db.execute(text("""
                UPDATE appointments 
                SET insurance_plan = 'particular' 
                WHERE insurance_plan IS NULL
            """))
            
            db.commit()
            print("✅ Coluna 'insurance_plan' adicionada com sucesso!")
            print("✅ Registros existentes atualizados com valor padrão 'particular'")
            
            return {"success": True, "message": "Migração executada com sucesso"}
            
    except Exception as e:
        print(f"❌ Erro na migração: {str(e)}")
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    result = migrate_add_insurance_plan()
    print(f"Resultado: {result}")
