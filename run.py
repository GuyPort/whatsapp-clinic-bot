"""
Script para rodar o bot localmente.
"""
import uvicorn
import sys
import os

def check_environment():
    """Verifica se o ambiente está configurado corretamente"""
    errors = []
    
    # Verificar .env
    if not os.path.exists('.env'):
        errors.append("❌ Arquivo .env não encontrado! Crie baseado em env.example")
    
    # Verificar google-credentials.json
    if not os.path.exists('google-credentials.json'):
        print("⚠️  Arquivo google-credentials.json não encontrado")
        print("   O bot funcionará, mas sem integração com Google Calendar")
    
    # Verificar data/clinic_info.json
    if not os.path.exists('data/clinic_info.json'):
        errors.append("❌ Arquivo data/clinic_info.json não encontrado!")
    
    if errors:
        print("\n".join(errors))
        print("\nPor favor, configure o ambiente antes de rodar o bot.")
        print("Veja SETUP_GUIDE.md para instruções detalhadas.")
        return False
    
    return True


if __name__ == "__main__":
    print("🤖 WhatsApp Clinic Bot")
    print("=" * 50)
    
    if not check_environment():
        sys.exit(1)
    
    print("✅ Ambiente configurado!")
    print("🚀 Iniciando servidor...\n")
    
    # Rodar servidor - CORRIGIDO para funcionar no Railway
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=False,  # Desabilitado para produção
        log_level="info"
    )

