"""
Script para rodar o bot localmente.
"""
import uvicorn
import sys
import os
import json

def check_environment():
    """Verifica se o ambiente está configurado corretamente para Railway."""
    errors = []
    
    # No Railway, as variáveis de ambiente são definidas diretamente, não via .env
    # Então, removemos a verificação do arquivo .env aqui.
    
    # Google Calendar removido - usando apenas banco de dados
    
    # Verificar data/clinic_info.json
    if not os.path.exists('data/clinic_info.json'):
        errors.append("❌ Arquivo data/clinic_info.json não encontrado!")
    else:
        print("✅ Arquivo data/clinic_info.json encontrado.")

    # Verificar variáveis de ambiente essenciais
    required_env_vars = ["ANTHROPIC_API_KEY", "WASENDER_API_KEY", "WASENDER_URL", "WASENDER_PROJECT_NAME"]
    for var in required_env_vars:
        if not os.getenv(var):
            errors.append(f"❌ Variável de ambiente '{var}' não configurada.")
        else:
            print(f"✅ Variável de ambiente '{var}' configurada.")

    if errors:
        print("\nPor favor, configure o ambiente antes de rodar o bot.")
        print("Veja SETUP_GUIDE.md para instruções detalhadas.")
        for error in errors:
            print(error)
        sys.exit(1)
    else:
        print("\n✅ Ambiente configurado corretamente!")


if __name__ == "__main__":
    print("🤖 WhatsApp Clinic Bot")
    print("=" * 50)
    print("🚀 FORCE REBUILD - 2025-10-15 15:07:00 - REBUILD DEFINITIVO")
    print("=" * 50)
    
    print("🔍 Verificando ambiente...")
    check_environment()
    
    # Debug: Mostrar configurações
    print("🔧 CONFIGURAÇÕES DEBUG:")
    print(f"WASENDER_URL: {os.getenv('WASENDER_URL', 'NÃO DEFINIDO')}")
    print(f"WASENDER_API_KEY: {os.getenv('WASENDER_API_KEY', 'NÃO DEFINIDO')[:10]}...")
    print(f"WASENDER_PROJECT_NAME: {os.getenv('WASENDER_PROJECT_NAME', 'NÃO DEFINIDO')}")
    print("=" * 50)
    
    print("🚀 Iniciando servidor Uvicorn...")
    try:
        port = int(os.getenv("PORT", 8000))
        print(f"📡 Porta configurada: {port}")
        
        print("🔄 Iniciando FastAPI...")
        uvicorn.run(
            "app.main:app",
            host="0.0.0.0",
            port=port,
            reload=False,  # Desabilitado para produção
            log_level="info"
        )
    except Exception as e:
        print(f"❌ ERRO FATAL ao iniciar o servidor Uvicorn: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

