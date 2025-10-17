"""
Configurações simples sem Pydantic para evitar problemas de cache.
VERSÃO: 2025-10-15 15:07:00 - REBUILD DEFINITIVO
"""
import os

# Configurações carregadas diretamente das variáveis de ambiente
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
EVOLUTION_API_URL = "https://wasenderapi.com"
EVOLUTION_API_KEY = os.getenv("WASENDER_API_KEY")
EVOLUTION_INSTANCE_NAME = os.getenv("WASENDER_PROJECT_NAME", "clinica-bot")

# Configuração de banco de dados
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./data/appointments.db"  # Fallback para dev local
)

# Railway PostgreSQL usa postgres:// mas SQLAlchemy precisa de postgresql://
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ENVIRONMENT = "production"
LOG_LEVEL = "INFO"
TIMEZONE = "America/Sao_Paulo"

# Classe simples para compatibilidade
class Settings:
    anthropic_api_key = ANTHROPIC_API_KEY
    evolution_api_url = EVOLUTION_API_URL
    evolution_api_key = EVOLUTION_API_KEY
    evolution_instance_name = EVOLUTION_INSTANCE_NAME
    # Google Calendar removido
    database_url = DATABASE_URL
    environment = ENVIRONMENT
    log_level = LOG_LEVEL
    timezone = TIMEZONE

# Instância global das configurações
settings = Settings()
