"""
Configurações simples sem Pydantic para evitar problemas de cache.
VERSÃO: 2025-10-15 15:07:00 - REBUILD DEFINITIVO
"""
import os

# Configurações carregadas diretamente das variáveis de ambiente
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
WASENDER_URL_RAW = os.getenv("WASENDER_URL", "")

# CORREÇÃO DEFINITIVA DA URL - HARDCODED
EVOLUTION_API_URL = "https://wasenderapi.com"

EVOLUTION_API_KEY = os.getenv("WASENDER_API_KEY")
EVOLUTION_INSTANCE_NAME = os.getenv("WASENDER_PROJECT_NAME", "clinica-bot")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_FILE = "google-credentials.json"
DATABASE_URL = "sqlite:///./data/appointments.db"
ENVIRONMENT = "production"
LOG_LEVEL = "INFO"
TIMEZONE = "America/Sao_Paulo"

# Debug: Log das configurações
import logging
logger = logging.getLogger(__name__)
logger.info(f"=== CONFIGURAÇÕES DEBUG - REBUILD DEFINITIVO ===")
logger.info(f"WASENDER_URL original: {WASENDER_URL_RAW}")
logger.info(f"EVOLUTION_API_URL processada: {EVOLUTION_API_URL}")
logger.info(f"EVOLUTION_INSTANCE_NAME: {EVOLUTION_INSTANCE_NAME}")
logger.info(f"EVOLUTION_API_KEY: {EVOLUTION_API_KEY[:10] if EVOLUTION_API_KEY else 'None'}...")
logger.info(f"URL contém ttps://: {'ttps://' in WASENDER_URL_RAW}")
logger.info(f"URL após replace: {WASENDER_URL_RAW.replace('ttps://', 'https://')}")
logger.info(f"WASENDER_PROJECT_NAME env: {os.getenv('WASENDER_PROJECT_NAME', 'NÃO DEFINIDO')}")
logger.info(f"===============================================")

# Classe simples para compatibilidade
class Settings:
    anthropic_api_key = ANTHROPIC_API_KEY
    evolution_api_url = EVOLUTION_API_URL
    evolution_api_key = EVOLUTION_API_KEY
    evolution_instance_name = EVOLUTION_INSTANCE_NAME
    google_calendar_id = GOOGLE_CALENDAR_ID
    google_service_account_file = GOOGLE_SERVICE_ACCOUNT_FILE
    database_url = DATABASE_URL
    environment = ENVIRONMENT
    log_level = LOG_LEVEL
    timezone = TIMEZONE

# Instância global das configurações
settings = Settings()
