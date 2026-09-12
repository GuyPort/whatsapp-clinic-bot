import os
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


os.environ["APP_SKIP_DOTENV"] = "1"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["REDIS_URL"] = "redis://synthetic.invalid/0"
os.environ["WASENDER_WEBHOOK_SECRET"] = "synthetic-webhook-secret"
os.environ["CONVERSATION_COORDINATION_EPOCH"] = "00000000-0000-4000-8000-000000000001"
os.environ["CONVERSATION_REDIS_EXPECTED_RUN_ID"] = "synthetic-run-id"
os.environ["CONVERSATION_REDIS_ATTEST_NOEVICTION"] = "true"
os.environ["CONVERSATION_REDIS_ATTEST_PERSISTENCE"] = "true"
os.environ["CONTACT_LEASE_TTL_SECONDS"] = "60"
os.environ["CONTACT_LEASE_HEARTBEAT_SECONDS"] = "15"
os.environ["CLAIM_TTL_SECONDS"] = "45"
os.environ["DISPATCH_RETRY_SECONDS"] = "900"
os.environ["PROCESSING_RETRY_SECONDS"] = "600"
os.environ["ENQUEUE_VISIBILITY_SECONDS"] = "60"
os.environ["ENQUEUE_BACKOFF_SECONDS"] = "30"
os.environ["REPLAY_WINDOW_SECONDS"] = "604800"
os.environ["TTL_MARGIN_SECONDS"] = "300"
os.environ["BATCH_RECOVERY_INTERVAL_SECONDS"] = "20"


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config.inicfg.setdefault("asyncio_default_fixture_loop_scope", "function")


@pytest.fixture
def session_factory():
    from app.database import Base

    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def app_client(monkeypatch):
    from app import main

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    monkeypatch.setattr(main.app.router, "lifespan_context", no_lifespan)
    with TestClient(main.app) as client:
        yield client
