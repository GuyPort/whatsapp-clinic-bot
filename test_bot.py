"""
Script para testar o bot localmente sem WhatsApp.
Simula conversas para testar a lógica do agente.
"""
import asyncio
import sys
from sqlalchemy.orm import Session

# Adicionar o diretório raiz ao path
sys.path.insert(0, '.')

from app.database import init_db, get_db
from app.ai_agent import ai_agent


async def test_conversation():
    """Testa uma conversa completa de agendamento"""
    
    print("🤖 Bot de Teste - Simulador de Conversa")
    print("=" * 50)
    print("Digite 'sair' para encerrar\n")
    
    # Inicializar banco
    init_db()
    
    # Telefone de teste
    test_phone = "5511999999999"
    
    print("Bot: Olá! Bem-vindo à clínica. Como posso ajudar?\n")
    
    while True:
        # Ler mensagem do usuário
        user_message = input("Você: ").strip()
        
        if not user_message:
            continue
        
        if user_message.lower() in ['sair', 'exit', 'quit']:
            print("\n👋 Encerrando teste...")
            break
        
        # Processar mensagem
        try:
            with get_db() as db:
                response = await ai_agent.process_message(
                    test_phone,
                    user_message,
                    db
                )
            
            print(f"\nBot: {response}\n")
            
        except Exception as e:
            print(f"\n❌ Erro: {str(e)}\n")
            import traceback
            traceback.print_exc()


async def test_quick_questions():
    """Testa perguntas rápidas"""
    
    print("🧪 Teste Rápido - Perguntas Simples")
    print("=" * 50)
    
    init_db()
    
    test_phone = "5511888888888"
    
    questions = [
        "Qual o valor da consulta?",
        "Qual o horário de atendimento?",
        "Qual o endereço?",
        "Quais convênios são aceitos?",
    ]
    
    for question in questions:
        print(f"\n📤 Pergunta: {question}")
        
        try:
            with get_db() as db:
                response = await ai_agent.process_message(
                    test_phone,
                    question,
                    db
                )
            
            print(f"📥 Resposta: {response}")
            
        except Exception as e:
            print(f"❌ Erro: {str(e)}")


def main():
    """Menu principal"""
    print("\n🧪 Modo de Teste do Bot")
    print("=" * 50)
    print("1. Conversa interativa (simular agendamento completo)")
    print("2. Teste rápido (perguntas simples)")
    print("3. Sair")
    
    choice = input("\nEscolha uma opção: ").strip()
    
    if choice == "1":
        asyncio.run(test_conversation())
    elif choice == "2":
        asyncio.run(test_quick_questions())
    else:
        print("👋 Até logo!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Teste interrompido pelo usuário")
    except Exception as e:
        print(f"\n❌ Erro fatal: {str(e)}")
        import traceback
        traceback.print_exc()

