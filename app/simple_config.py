"""
Configurações simples sem Pydantic para evitar problemas de cache.
"""
import os

# Configurações carregadas diretamente das variáveis de ambiente
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
EVOLUTION_API_URL = os.getenv("WASENDER_URL", "").replace("ttps://", "https://").rstrip("/api/send-message/").rstrip("/")
EVOLUTION_API_KEY = os.getenv("WASENDER_API_KEY")
EVOLUTION_INSTANCE_NAME = os.getenv("WASENDER_PROJECT_NAME", "clinica-bot")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_FILE = "google-credentials.json"
DATABASE_URL = "sqlite:///./data/appointments.db"
ENVIRONMENT = "production"
LOG_LEVEL = "INFO"
TIMEZONE = "America/Sao_Paulo"

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
