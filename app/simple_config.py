"""
Configurações simples sem Pydantic para evitar problemas de cache.
"""
import os
from dotenv import load_dotenv

# Carregar variaveis do arquivo .env apenas no runtime normal.
if os.getenv("APP_SKIP_DOTENV") != "1":
    load_dotenv()

# Configurações carregadas diretamente das variáveis de ambiente
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
EVOLUTION_API_URL = "https://wasenderapi.com"
EVOLUTION_API_KEY = os.getenv("WASENDER_API_KEY", "").strip() or None
EVOLUTION_INSTANCE_NAME = os.getenv("WASENDER_PROJECT_NAME", "clinica-bot")

# Configuração de banco de dados
# Importante: projeto espera PostgreSQL em produção. Defina DATABASE_URL.
# O fallback abaixo só deve ser usado em experimentos locais sem concorrência.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./data/appointments.db"
)

# Railway PostgreSQL usa postgres:// mas SQLAlchemy precisa de postgresql://
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Configuração Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Autenticação Admin
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")  # ALTERAR EM PRODUÇÃO!

ENVIRONMENT = "production"
LOG_LEVEL = "INFO"
TIMEZONE = "America/Sao_Paulo"

def _optional_positive_int(environ, name):
    raw = environ.get(name)
    if raw is None:
        return None
    value = int(raw)
    return value if value > 0 else None


# Classe simples para compatibilidade
class Settings:
    def __init__(self, environ=None):
        env = os.environ if environ is None else environ

        self.anthropic_api_key = env.get("ANTHROPIC_API_KEY")
        self.evolution_api_url = EVOLUTION_API_URL
        self.evolution_api_key = env.get("WASENDER_API_KEY", "").strip() or None
        self.evolution_instance_name = env.get("WASENDER_PROJECT_NAME", "clinica-bot")

        database_url = env.get("DATABASE_URL", "sqlite:///./data/appointments.db")
        if database_url and database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        self.database_url = database_url
        self.redis_url = env.get("REDIS_URL", "redis://localhost:6379/0")
        self.admin_password = env.get("ADMIN_PASSWORD", "admin123")
        self.environment = ENVIRONMENT
        self.log_level = LOG_LEVEL
        self.timezone = TIMEZONE

        self.webhook_secret = env.get("WASENDER_WEBHOOK_SECRET", "").strip() or None
        self.coordination_epoch = env.get("CONVERSATION_COORDINATION_EPOCH", "").strip() or None
        self.redis_expected_run_id = env.get("CONVERSATION_REDIS_EXPECTED_RUN_ID", "").strip() or None
        self.redis_attest_noeviction = (
            env.get("CONVERSATION_REDIS_ATTEST_NOEVICTION", "").lower() == "true"
        )
        self.redis_attest_persistence = (
            env.get("CONVERSATION_REDIS_ATTEST_PERSISTENCE", "").lower() == "true"
        )
        self.contact_lease_ttl_seconds = _optional_positive_int(env, "CONTACT_LEASE_TTL_SECONDS")
        self.contact_lease_heartbeat_seconds = _optional_positive_int(
            env, "CONTACT_LEASE_HEARTBEAT_SECONDS"
        )
        self.claim_ttl_seconds = _optional_positive_int(env, "CLAIM_TTL_SECONDS")
        self.dispatch_retry_seconds = _optional_positive_int(env, "DISPATCH_RETRY_SECONDS")
        self.processing_retry_seconds = _optional_positive_int(env, "PROCESSING_RETRY_SECONDS")
        self.enqueue_visibility_seconds = _optional_positive_int(env, "ENQUEUE_VISIBILITY_SECONDS")
        self.enqueue_backoff_seconds = _optional_positive_int(env, "ENQUEUE_BACKOFF_SECONDS")
        self.replay_window_seconds = _optional_positive_int(env, "REPLAY_WINDOW_SECONDS")
        self.ttl_margin_seconds = _optional_positive_int(env, "TTL_MARGIN_SECONDS")
        self.batch_recovery_interval_seconds = _optional_positive_int(
            env, "BATCH_RECOVERY_INTERVAL_SECONDS"
        )

# Instância global das configurações
settings = Settings()
