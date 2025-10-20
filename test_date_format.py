#!/usr/bin/env python3
"""
Script de teste para verificar se o PostgreSQL está aceitando o formato DD-MM-AAAA
após o restart com PGDATESTYLE=SQL,DMY.
"""

import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# Adicionar o diretório app ao path para importar configurações
sys.path.append('app')
from simple_config import settings

def test_date_format():
    """Testa se o PostgreSQL aceita formato DD-MM-AAAA"""
    
    print("🔍 Testando formato de data no PostgreSQL...")
    print("=" * 50)
    
    # Usar configuração do projeto
    database_url = settings.database_url
    if not database_url:
        print("❌ DATABASE_URL não configurada!")
        return False
    
    print(f"📡 Conectando ao banco: {database_url[:30]}...")
    
    # Detectar tipo de banco
    if "sqlite" in database_url.lower():
        print("⚠️ Detectado SQLite local - não é possível testar PostgreSQL")
        print("💡 Este teste deve ser executado no ambiente Railway (PostgreSQL)")
        return False
    
    try:
        # Criar engine com configuração de timezone (apenas para PostgreSQL)
        engine = create_engine(
            database_url,
            connect_args={
                "options": "-c timezone=America/Sao_Paulo"
            }
        )
        
        with engine.connect() as conn:
            print("✅ Conexão estabelecida!")
            
            # Teste 1: Verificar configuração atual do datestyle
            print("\n🔧 Verificando configuração PGDATESTYLE...")
            result = conn.execute(text("SHOW datestyle"))
            datestyle = result.fetchone()[0]
            print(f"   PGDATESTYLE atual: {datestyle}")
            
            # Teste 2: Tentar query com formato DD-MM-AAAA
            print("\n📅 Testando query com formato '22-10-2025'...")
            try:
                result = conn.execute(text("""
                    SELECT COUNT(*) as total 
                    FROM appointments 
                    WHERE appointment_date = '22-10-2025'
                """))
                count = result.fetchone()[0]
                print(f"   ✅ Query executada com sucesso! Encontrados: {count} registros")
                return True
                
            except SQLAlchemyError as e:
                print(f"   ❌ Erro na query: {str(e)}")
                
                # Teste 3: Tentar formato YYYYMMDD como fallback
                print("\n📅 Testando formato alternativo '20251022'...")
                try:
                    result = conn.execute(text("""
                        SELECT COUNT(*) as total 
                        FROM appointments 
                        WHERE appointment_date = '20251022'
                    """))
                    count = result.fetchone()[0]
                    print(f"   ✅ Formato YYYYMMDD funciona! Encontrados: {count} registros")
                    print("   💡 Recomendação: Usar formato YYYYMMDD")
                    return False
                    
                except SQLAlchemyError as e2:
                    print(f"   ❌ Formato YYYYMMDD também falhou: {str(e2)}")
                    return False
            
    except Exception as e:
        print(f"❌ Erro de conexão: {str(e)}")
        return False

def main():
    """Função principal"""
    print("🚀 Teste de Formato de Data - PostgreSQL")
    print("=" * 50)
    
    success = test_date_format()
    
    print("\n" + "=" * 50)
    if success:
        print("✅ RESULTADO: Formato DD-MM-AAAA está funcionando!")
        print("💡 O problema pode estar no código Python.")
    else:
        print("❌ RESULTADO: Formato DD-MM-AAAA ainda não funciona.")
        print("💡 Recomendação: Implementar formato YYYYMMDD definitivamente.")
    
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())