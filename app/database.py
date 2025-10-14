"""
Configuração e gerenciamento do banco de dados SQLite.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from typing import Generator
import os

from app.config import settings
from app.models import Base


# Garantir que o diretório data existe
os.makedirs("data", exist_ok=True)

# Criar engine do SQLAlchemy
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},  # Necessário para SQLite
    echo=settings.log_level == "DEBUG"  # Log de SQL queries em debug
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


def get_db_session() -> Generator[Session, None, None]:
    """
    Dependency para FastAPI obter uma sessão do banco.
    
    Uso em rotas FastAPI:
        @app.get("/")
        def route(db: Session = Depends(get_db_session)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

