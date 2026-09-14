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
def session_factory(conversation_security_boundaries):
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
def main_module(monkeypatch, conversation_security_boundaries):
    """Import the actual routes with external construction/effects replaced."""
    import importlib.util
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    import app
    from app import database
    from tests.fakes import ForbiddenAgentEffects

    effects = ForbiddenAgentEffects()
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


@pytest.fixture
def scheduler_application_log_records(scheduler_module, caplog):
    """Observe application logs and the exact name of this injected scheduler."""
    def observed():
        return [record for record in caplog.records
                if record.name.startswith("app.") or record.name == scheduler_module.logger.name]
    return observed


@pytest.fixture
def conversation_security_boundaries(monkeypatch):
    """Fail closed if audit tests cross a forbidden external/write boundary."""
    import builtins
    import io
    import os
    import socket
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    import sqlalchemy
    from sqlalchemy.pool import StaticPool

    workspace = Path(__file__).parents[1].resolve()
    original_open = builtins.open
    original_socket_connect = socket.socket.connect
    original_create_engine = sqlalchemy.create_engine
    original_sqlite_connect = sqlite3.connect
    original_path_methods = {name: getattr(Path, name) for name in
                             ("mkdir", "rename", "replace", "unlink", "rmdir")}
    original_os_methods = {name: getattr(os, name) for name in
                           ("mkdir", "makedirs", "rename", "replace", "remove", "unlink", "rmdir")}

    def in_workspace(value):
        try:
            target = Path(value).resolve()
        except (TypeError, OSError):
            return False
        return target == workspace or workspace in target.parents

    def guarded_connect(sock, address):
        caller = sys._getframe(1)
        while caller is not None:
            if (caller.f_code.co_name == "_fallback_socketpair"
                    and caller.f_globals.get("__name__") == "socket"
                    and address[0] in ("127.0.0.1", "::1")):
                return original_socket_connect(sock, address)
            caller = caller.f_back
        raise AssertionError("network forbidden")

    def guarded_connect_ex(sock, address):
        raise AssertionError("network forbidden")

    def guarded_create_connection(*args, **kwargs):
        raise AssertionError("network forbidden")

    def safe_sqlite_database(database):
        return str(database) == ":memory:"

    def guarded_sqlite_connect(database, *args, **kwargs):
        if not safe_sqlite_database(database):
            raise AssertionError("persistent_sql forbidden")
        return original_sqlite_connect(database, *args, **kwargs)

    def guarded_create_engine(url, *args, **kwargs):
        parsed = sqlalchemy.engine.make_url(url)
        database = parsed.database
        query = dict(parsed.query)
        safe = (parsed.get_backend_name() == "sqlite"
                and database in (None, "", ":memory:")
                and not query)
        if not safe:
            raise AssertionError("persistent_sql forbidden")
        return original_create_engine(url, *args, **kwargs)

    def guarded_path_method(name):
        original = original_path_methods[name]
        def call(path, *args, **kwargs):
            if name == "mkdir" and kwargs.get("exist_ok") and Path(path).is_dir():
                return None
            targets = (path, args[0]) if name in ("rename", "replace") and args else (path,)
            if any(in_workspace(target) for target in targets):
                raise AssertionError(f"{name if name != 'unlink' else 'remove'} forbidden")
            return original(path, *args, **kwargs)
        return call

    def guarded_os_method(name):
        original = original_os_methods[name]
        def call(path, *args, **kwargs):
            if name == "makedirs" and kwargs.get("exist_ok") and Path(path).is_dir():
                return None
            targets = (path, args[0]) if name in ("rename", "replace") and args else (path,)
            if any(in_workspace(target) for target in targets):
                raise AssertionError(f"{name if name != 'unlink' else 'remove'} forbidden")
            return original(path, *args, **kwargs)
        return call

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            try:
                target = Path(file).resolve()
            except (TypeError, OSError):
                target = None
            if target is not None and (target == workspace or workspace in target.parents):
                pytest.fail("repository write forbidden in conversation security test")
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(sqlalchemy, "create_engine", guarded_create_engine)
    monkeypatch.setattr(sqlite3, "connect", guarded_sqlite_connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", guarded_sqlite_connect)
    for name in original_path_methods:
        monkeypatch.setattr(Path, name, guarded_path_method(name))
    for name in original_os_methods:
        monkeypatch.setattr(os, name, guarded_os_method(name))
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name,
            lambda *args, _name=name, **kwargs: pytest.fail(
                f"subprocess {_name} forbidden in conversation security test"))
    try:
        import dotenv
        monkeypatch.setattr(dotenv, "load_dotenv",
                            lambda *args, **kwargs: pytest.fail("dotenv forbidden in security test"))
    except ImportError:
        pass
    monkeypatch.setenv("APP_SKIP_DOTENV", "1")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")

    def network_probe():
        socket.create_connection(("192.0.2.1", 9), timeout=0.01)

    def persistent_sql_probe():
        sqlalchemy.create_engine(f"sqlite:///{workspace / 'forbidden-security.db'}")

    def persistent_sqlite_probe():
        sqlite3.connect(workspace / "forbidden-security.db")

    def memory_sql_probe():
        engine = sqlalchemy.create_engine("sqlite://", poolclass=StaticPool,
                                          connect_args={"check_same_thread": False})
        try:
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT 1").scalar() == 1
        finally:
            engine.dispose()

    return SimpleNamespace(
        network=network_probe,
        persistent_sql=persistent_sql_probe,
        persistent_sqlite=persistent_sqlite_probe,
        mkdir=lambda: (workspace / "forbidden-security-dir").mkdir(),
        rename=lambda: (workspace / "forbidden-source").rename(workspace / "forbidden-target"),
        remove=lambda: (workspace / "forbidden-source").unlink(),
        replace=lambda: (workspace / "forbidden-source").replace(workspace / "forbidden-target"),
        memory_sql=memory_sql_probe,
        readonly_file=lambda: (workspace / "tests" / "conftest.py").read_text(encoding="utf-8"),
    )
