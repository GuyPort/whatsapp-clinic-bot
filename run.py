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
    
    # Verificar e criar google-credentials.json a partir da variável de ambiente
    if not os.path.exists('google-credentials.json'):
        google_creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if google_creds_json:
            try:
                # Tenta parsear para validar e depois escreve
                json.loads(google_creds_json) 
                with open('google-credentials.json', 'w') as f:
                    f.write(google_creds_json)
                print("✅ Arquivo google-credentials.json criado a partir da variável de ambiente.")
            except json.JSONDecodeError:
                errors.append("❌ Variável GOOGLE_CREDENTIALS inválida (não é um JSON válido).")
        else:
            print("⚠️  Variável GOOGLE_CREDENTIALS não encontrada.")
            print("   O bot funcionará, mas sem integração com Google Calendar.")
    else:
        print("✅ Arquivo google-credentials.json encontrado.")
    
    # Verificar data/clinic_info.json
    if not os.path.exists('data/clinic_info.json'):
        errors.append("❌ Arquivo data/clinic_info.json não encontrado!")
    else:
        print("✅ Arquivo data/clinic_info.json encontrado.")

    # Verificar variáveis de ambiente essenciais
    required_env_vars = ["ANTHROPIC_API_KEY", "WASENDER_API_KEY", "WASENDER_URL", "WASENDER_PROJECT_NAME", "GOOGLE_CALENDAR_ID"]
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
    
    print("🔍 Verificando ambiente...")
    check_environment()
    
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

