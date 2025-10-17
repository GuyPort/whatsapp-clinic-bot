#!/usr/bin/env python3
"""
Script para criar a tabela conversation_contexts no PostgreSQL.
Execute este script após adicionar o model ConversationContext.
"""

import sys
import os
from datetime import datetime

# Adicionar o diretório raiz ao path para importar os módulos
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database import engine
from app.models import Base, ConversationContext

def create_conversation_context_table():
    """Cria a tabela conversation_contexts no banco de dados"""
    try:
        print("🔄 Criando tabela conversation_contexts...")
        
        # Criar apenas a tabela ConversationContext
        Base.metadata.create_all(bind=engine, tables=[ConversationContext.__table__])
        
        print("✅ Tabela conversation_contexts criada com sucesso!")
        print("📊 Estrutura da tabela:")
        print("   - phone (PK): String(20) - Número do WhatsApp")
        print("   - messages: JSON - Histórico de mensagens")
        print("   - current_flow: String(50) - Fluxo atual")
        print("   - flow_data: JSON - Dados coletados")
        print("   - status: String(20) - Status da conversa")
        print("   - paused_until: DateTime - Quando pausa expira")
        print("   - last_activity: DateTime - Última atividade")
        print("   - created_at: DateTime - Data de criação")
        
    except Exception as e:
        print(f"❌ Erro ao criar tabela: {str(e)}")
        return False
    
    return True

if __name__ == "__main__":
    print("🚀 Migration: ConversationContext")
    print("=" * 50)
    
    success = create_conversation_context_table()
    
    if success:
        print("\n✅ Migration concluída com sucesso!")
        print("🎯 O bot agora pode manter contexto de conversas!")
    else:
        print("\n❌ Migration falhou!")
        sys.exit(1)
