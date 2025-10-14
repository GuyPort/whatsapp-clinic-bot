"""
Configurações da aplicação carregadas de variáveis de ambiente.
"""
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    """Configurações da aplicação"""
    
    # Anthropic API (Claude)
    anthropic_api_key: str
    
    # Evolution API (WhatsApp) - usando Wasender
    evolution_api_url: str = Field(..., env="WASENDER_URL")
    evolution_api_key: str = Field(..., env="WASENDER_API_KEY")
    evolution_instance_name: str = Field(default="clinica-bot", env="WASENDER_PROJECT_NAME")
    
    # Google Calendar
    google_calendar_id: str
    google_service_account_file: str = "google-credentials.json"
    
    # Database
    database_url: str = "sqlite:///./data/appointments.db"
    
    # Configurações gerais
    environment: str = "production"
    log_level: str = "INFO"
    
    # Timezone
    timezone: str = "America/Sao_Paulo"
    
    class Config:
        env_file = ".env"
        case_sensitive = False


# Instância global das configurações
settings = Settings()
