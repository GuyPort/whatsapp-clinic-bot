"""
Configuração e gerenciamento do banco de dados SQLite.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from typing import Generator
import os

from app.simple_config import settings
from app.models import Base


# Garantir que o diretório data existe
os.makedirs("data", exist_ok=True)

# Criar engine do SQLAlchemy com suporte a PostgreSQL e SQLite
# Configuração dinâmica baseada no tipo de banco
if settings.database_url.startswith("sqlite"):
    # Configuração para SQLite (desenvolvimento local)
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},  # Necessário para SQLite
        echo=settings.log_level == "DEBUG"
    )
else:
    # Configuração para PostgreSQL (produção Railway)
    # ADICIONAR: Configuração de timezone para PostgreSQL
    engine = create_engine(
        settings.database_url,
        echo=settings.log_level == "DEBUG",
        pool_pre_ping=True,  # Verifica conexão antes de usar
        pool_size=10,  # Pool de conexões
        max_overflow=20,  # Máximo de conexões extras
        connect_args={
            "options": "-c timezone=America/Sao_Paulo"  # Forçar timezone do Brasil
        }
    )

# Criar SessionLocal
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Inicializa o banco de dados criando todas as tabelas"""
    Base.metadata.create_all(bind=engine)
    print("✅ Banco de dados inicializado com sucesso!")


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """
    Context manager para obter uma sessão do banco de dados.
    
    Uso:
        with get_db() as db:
            db.query(Patient).all()
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


