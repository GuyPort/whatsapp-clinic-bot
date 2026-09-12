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
def main_module(monkeypatch):
    """Import the actual routes with external construction/effects replaced."""
    import importlib.util
    import socket
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    import app
    from app import database
    from tests.fakes import ForbiddenAgentEffects

    effects = ForbiddenAgentEffects()
    original_connect = socket.socket.connect
    def guarded_connect(sock, address):
        # Windows implements asyncio's internal self-pipe with a socketpair.
        # Only the stdlib socketpair frame may create its loopback connection.
        caller = sys._getframe(1)
        if (caller.f_code.co_name == "_fallback_socketpair"
                and caller.f_globals.get("__name__") == "socket"
                and address[0] in ("127.0.0.1", "::1")):
            return original_connect(sock, address)
        return effects.boundary("network")()
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(database, "init_db", effects.boundary("init_db"))
    monkeypatch.setattr(database, "get_db", effects.boundary("implicit_session"))
    transport = SimpleNamespace(redis_client=None,
        send_message=effects.boundary("send"), add_message_to_buffer=effects.boundary("legacy_buffer"))

    def task_decorator(**options):
        def decorate(function):
            function.delay = effects.boundary("legacy_delay")
            function.apply_async = effects.boundary("legacy_enqueue")
            return function
        return decorate

    for name, module in {
        "app.ai_agent": SimpleNamespace(ai_agent=SimpleNamespace(
            _handle_secretary_pause=effects.boundary("legacy_pause"))),
        "app.whatsapp_service": SimpleNamespace(whatsapp_service=transport),
        "app.scheduler": SimpleNamespace(start_scheduler=effects.boundary("scheduler"),
                                          stop_scheduler=effects.boundary("scheduler")),
        "app.celery_app": SimpleNamespace(celery_app=SimpleNamespace(task=task_decorator)),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("app.main", Path(__file__).parents[1] / "app" / "main.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "app.main", module)
    monkeypatch.setattr(app, "main", module, raising=False)
    spec.loader.exec_module(module)
    module.forbidden_effects = effects
    yield module
    assert effects.calls == []


@pytest.fixture
def ingress_runtime(main_module, monkeypatch, session_factory):
    from app.conversation_state import ConversationConfig
    from app.simple_config import settings
    from tests.fakes import IngressRuntime
    runtime = IngressRuntime(session_factory, ConversationConfig.from_settings(settings))
    monkeypatch.setattr(main_module.app.state, "conversation_runtime", runtime, raising=False)
    return runtime


@pytest.fixture
def app_client(monkeypatch, main_module):
    main = main_module

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    monkeypatch.setattr(main.app.router, "lifespan_context", no_lifespan)
    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def admin_runtime(main_module, monkeypatch, session_factory):
    from app.conversation_state import ConversationConfig
    from tests.fakes import ProcessingRuntime
    runtime = ProcessingRuntime(session_factory, ConversationConfig.from_settings(main_module.settings))
    monkeypatch.setattr(main_module.app.state, "conversation_runtime", runtime, raising=False)
    monkeypatch.setattr(main_module.settings, "admin_password", "synthetic-admin-password")
    # Exercise old adapters against disposable SQL during RED, never a real DB.
    from contextlib import contextmanager
    @contextmanager
    def legacy_session():
        runtime.legacy_sessions += 1
        with session_factory() as db:
            assert db.bind.url.database in (None, "", ":memory:")
            yield db
    runtime.legacy_sessions = 0
    monkeypatch.setattr(main_module, "get_db", legacy_session)
    return runtime


@pytest.fixture
def admin_client(monkeypatch, main_module, admin_runtime):
    @asynccontextmanager
    async def no_lifespan(_app):
        yield
    monkeypatch.setattr(main_module.app.router, "lifespan_context", no_lifespan)
    with TestClient(main_module.app, raise_server_exceptions=False) as client:
        client.auth = ("synthetic-admin", "synthetic-admin-password")
        yield client


@pytest.fixture
def scheduler_module(main_module, admin_runtime):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "synthetic_scheduler", Path(__file__).parents[1] / "app" / "scheduler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.get_db = admin_runtime.session_factory
    return module
