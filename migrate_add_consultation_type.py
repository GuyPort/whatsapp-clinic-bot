#!/usr/bin/env python3
"""
Script de migração para adicionar coluna consultation_type na tabela appointments.
Executa no Railway via endpoint admin.
"""

import os
import sys
from sqlalchemy import text

def migrate_add_consultation_type():
    """Adiciona coluna consultation_type na tabela appointments"""
    try:
        # Importar configurações do banco
        from app.database import get_db
        
        with get_db() as db:
            # Verificar se a coluna já existe
            result = db.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'appointments' 
                AND column_name = 'consultation_type'
            """))
            
            if result.fetchone():
                print("✅ Coluna 'consultation_type' já existe na tabela 'appointments'")
                return {"success": True, "message": "Coluna já existe"}
            
            # Adicionar coluna
            db.execute(text("""
                ALTER TABLE appointments 
                ADD COLUMN consultation_type VARCHAR(50)
            """))
            
            # Atualizar registros existentes com valor padrão
            db.execute(text("""
                UPDATE appointments 
                SET consultation_type = 'clinica_geral' 
                WHERE consultation_type IS NULL
            """))
            
            db.commit()
            print("✅ Coluna 'consultation_type' adicionada com sucesso!")
            print("✅ Registros existentes atualizados com valor padrão 'clinica_geral'")
            
            return {"success": True, "message": "Migração executada com sucesso"}
            
    except Exception as e:
        print(f"❌ Erro na migração: {str(e)}")
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    result = migrate_add_consultation_type()
    print(f"Resultado: {result}")
