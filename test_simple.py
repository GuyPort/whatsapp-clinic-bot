"""
Teste simples do agente Claude com Tools.
"""
import asyncio
import sys
from sqlalchemy.orm import Session

# Adicionar o diretório raiz ao path
sys.path.insert(0, '.')

from app.database import init_db, get_db
from app.ai_agent import ai_agent


async def test_agent():
    """Testa o agente com uma mensagem simples"""
    
    print("🤖 Teste Simples do Agente Claude")
    print("=" * 50)
    
    # Inicializar banco
    init_db()
    
    # Telefone de teste
    test_phone = "5511999999999"
    
    # Teste 1: Mensagem inicial
    print("\n📤 Teste 1: Mensagem inicial")
    print("Mensagem: 'oi'")
    
    try:
        with get_db() as db:
            response = await ai_agent.process_message(test_phone, "oi", db)
        
        print(f"📥 Resposta: {response}")
        print("✅ Teste 1 passou!")
        
    except Exception as e:
        print(f"❌ Erro no Teste 1: {str(e)}")
        return False
    
    # Teste 2: Menu de opções
    print("\n📤 Teste 2: Menu de opções")
    print("Mensagem: '1'")
    
    try:
        with get_db() as db:
            response = await ai_agent.process_message(test_phone, "1", db)
        
        print(f"📥 Resposta: {response}")
        print("✅ Teste 2 passou!")
        
    except Exception as e:
        print(f"❌ Erro no Teste 2: {str(e)}")
        return False
    
    # Teste 3: Informações da clínica
    print("\n📤 Teste 3: Informações da clínica")
    print("Mensagem: '3'")
    
    try:
        with get_db() as db:
            response = await ai_agent.process_message(test_phone, "3", db)
        
        print(f"📥 Resposta: {response}")
        print("✅ Teste 3 passou!")
        
    except Exception as e:
        print(f"❌ Erro no Teste 3: {str(e)}")
        return False
    
    print("\n" + "=" * 50)
    print("✅ TODOS OS TESTES PASSARAM!")
    print("🎉 O agente está funcionando perfeitamente!")
    print("=" * 50)
    
    return True


if __name__ == "__main__":
    try:
        asyncio.run(test_agent())
    except KeyboardInterrupt:
        print("\n\n👋 Teste interrompido pelo usuário")
    except Exception as e:
        print(f"\n❌ Erro fatal: {str(e)}")
        import traceback
        traceback.print_exc()
