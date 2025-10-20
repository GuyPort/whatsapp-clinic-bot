#!/usr/bin/env python3
"""
Script para executar a migração no Railway via endpoint.
Este script aguarda o deploy terminar e executa a migração.
"""

import requests
import time
import sys

def find_railway_url():
    """Tenta descobrir a URL do Railway"""
    # URLs comuns do Railway
    possible_urls = [
        "https://whatsapp-clinic-bot-production.up.railway.app",
        "https://whatsapp-clinic-bot.up.railway.app", 
        "https://clinic-bot.up.railway.app",
        "https://bot-clinica.up.railway.app"
    ]
    
    print("🔍 Tentando descobrir URL do Railway...")
    
    for url in possible_urls:
        try:
            print(f"   Testando: {url}")
            response = requests.get(f"{url}/health", timeout=10)
            if response.status_code == 200:
                print(f"✅ URL encontrada: {url}")
                return url
        except:
            continue
    
    print("❌ Não foi possível descobrir a URL automaticamente.")
    print("💡 Por favor, forneça a URL manualmente:")
    return input("URL do Railway (ex: https://seu-app.up.railway.app): ").strip()

def wait_for_deploy(url):
    """Aguarda o deploy terminar"""
    print(f"⏳ Aguardando deploy terminar em {url}...")
    
    max_attempts = 30
    for attempt in range(max_attempts):
        try:
            response = requests.get(f"{url}/health", timeout=10)
            if response.status_code == 200:
                print("✅ Deploy concluído!")
                return True
        except:
            pass
        
        print(f"   Tentativa {attempt + 1}/{max_attempts}...")
        time.sleep(10)
    
    print("⚠️ Deploy ainda não terminou, mas vou tentar executar a migração mesmo assim.")
    return False

def execute_migration(url):
    """Executa a migração via endpoint"""
    print(f"🚀 Executando migração em {url}...")
    
    try:
        response = requests.post(f"{url}/admin/migrate-fix-date", timeout=60)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("status") == "success":
                print("✅ Migração executada com sucesso!")
                print(f"📋 {result.get('message')}")
                return True
            else:
                print(f"❌ Erro na migração: {result.get('message')}")
                return False
        else:
            print(f"❌ Erro HTTP {response.status_code}: {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Erro ao executar migração: {str(e)}")
        return False

def main():
    print("🔧 Executor de Migração Railway - Correção Bug Data")
    print("=" * 60)
    
    # Descobrir URL
    url = find_railway_url()
    if not url:
        print("❌ URL não fornecida. Cancelando.")
        sys.exit(1)
    
    # Aguardar deploy
    wait_for_deploy(url)
    
    # Executar migração
    success = execute_migration(url)
    
    if success:
        print("\n🎉 Migração concluída com sucesso!")
        print("📋 Próximos passos:")
        print("   1. Fazer novo agendamento via WhatsApp")
        print("   2. Verificar logs: appointment_datetime_formatted: 'YYYYMMDD'")
        print("   3. Verificar banco: deve salvar como string sem hífen")
        print("   4. Verificar dashboard: deve exibir data correta")
    else:
        print("\n❌ Migração falhou!")
        print("💡 Tente executar manualmente:")
        print(f"   curl -X POST {url}/admin/migrate-fix-date")
        sys.exit(1)

if __name__ == "__main__":
    main()
