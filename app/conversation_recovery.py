"""Readiness, process composition and metadata-only recovery; import has no I/O."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from threading import Lock
from typing import Callable, Mapping

from sqlalchemy import select, text

from app.conversation_state import (
    ConversationConfig, ConversationCoordinator, DependencyName, DependencyStatus,
    ReadinessReport, RecoveryReport,
)
from app.conversation_tasks import ConversationRuntime, _require_ready, _SimulatorCapture
from app.models import ConversationContext, PausedContact


class DependencyNotReady(RuntimeError):
    def __init__(self):
        super().__init__("conversation dependencies unavailable")


class DependencyReadiness:
    """Allowlisted synchronous probes; their injected clients must enforce timeouts."""
    def __init__(self, probes: Mapping[DependencyName, Callable[[], bool]]):
        self._probes = {name: probes.get(name) for name in DependencyName}

    def check(self) -> ReadinessReport:
        states = []
        for name in DependencyName:
            try:
                ready = self._probes[name]() is True
            except Exception:
                ready = False
            states.append(DependencyStatus(name, ready))
        return ReadinessReport(tuple(states))

    def require_ready(self) -> None:
        if not self.check().ready:
            raise DependencyNotReady()

    @classmethod
    def for_dependencies(cls, *, secret, sql_probe, store, broker):
        def has_secret():
            value = secret()
            return isinstance(value, str) and bool(value.strip())
        return cls({DependencyName.SECRET: has_secret,
            DependencyName.SQL: sql_probe, DependencyName.REDIS: store.client.ping,
            DependencyName.EPOCH: lambda: store.readiness().ready,
            DependencyName.BROKER: broker.probe})


def public_readiness(runtime):
    """Revalidate the complete report before publishing only public enum values."""
    states = {name.value: "not_ready" for name in DependencyName}
    try:
        report = runtime.readiness_status()
        rows = tuple(report.dependencies)
        if (len(rows) == len(DependencyName)
                and {row.name for row in rows} == set(DependencyName)
                and all(isinstance(row.name, DependencyName) and type(row.ready) is bool for row in rows)):
            states = {row.name.value: "ready" if row.ready else "not_ready" for row in rows}
    except Exception:
        pass
    ready = all(value == "ready" for value in states.values())
    return {"status": "ready" if ready else "not_ready", "dependencies": states}, 200 if ready else 503


class RecoveryService:
    """One bounded pass; continuations live in a shared epoch-fenced checkpoint.

    SCAN may repeat entries. Leases, fresh state and canonical CAS remain the
    authority. Recovery never executes the model or any outbound transport.
    """
    def __init__(self, runtime, *, page_size=None, max_pages=None):
        self.runtime = runtime
        config = runtime.store.config
        self.page_size = config.recovery_page_size if page_size is None else page_size
        self.max_pages = config.recovery_max_pages if max_pages is None else max_pages
        if (type(self.page_size) is not int or not 1 <= self.page_size <= 1000
                or type(self.max_pages) is not int or not 1 <= self.max_pages <= 100):
            raise ValueError("invalid recovery limits")
        self._lock = Lock()

    def run_once(self, now: datetime) -> RecoveryReport:
        try:
            _require_ready(self.runtime)
        except Exception:
            return RecoveryReport()
        if not self._lock.acquire(blocking=False):
            return RecoveryReport()
        counts = {name: 0 for name in RecoveryReport.__dataclass_fields__}
        runtime = self.runtime
        try:
            if not isinstance(now, datetime) or now.tzinfo is None:
                return RecoveryReport(failed=1)
            for _ in range(self.max_pages):
                try:
                    _require_ready(runtime)
                    expected, position = runtime.store.recovery_checkpoint()
                    turn = position[0]
                    cursors = list(position[1:])
                    if turn == 0:
                        page = runtime.store.recoverable_mutations(now, self.page_size, cursor=cursors[turn])
                    else:
                        page = runtime.store.recoverable_batches(self.page_size, cursor=cursors[turn], now=now)
                    cursors[turn] = page.next_cursor
                    counts["scanned"] += page.scanned
                    counts["failed"] += page.failed
                except Exception:
                    counts["failed"] += 1
                    continue
                for item in (*page.mutations, *page.commands):
                    try:
                        _require_ready(runtime)
                    except Exception:
                        return RecoveryReport(**counts)
                    phone = item[0] if isinstance(item, tuple) else item.phone
                    try:
                        with runtime.store.contact_lease(phone) as lease:
                            _require_ready(runtime)
                            with runtime.session_factory() as db:
                                # Fresh SQL after lease: no autoflush/DML and no inference
                                # of a commit from the current content of these rows.
                                with db.no_autoflush:
                                    db.execute(select(ConversationContext.phone).where(ConversationContext.phone == phone)).first()
                                    db.execute(select(PausedContact.phone).where(PausedContact.phone == phone)).first()
                                _require_ready(runtime)
                                lease.assert_owned()
                                _require_ready(runtime)
                                if isinstance(item, tuple):
                                    outcome = runtime.store.recover_mutation(phone, item[1], runtime.clock.now(), lease,
                                        require_ready=lambda: _require_ready(runtime))
                                else:
                                    broker = (_SimulatorCapture() if phone == ConversationCoordinator.TEST_PHONE
                                              else runtime.processing_broker)
                                    outcome = runtime.store.recover_batch(item, broker, runtime.clock.now(), lease,
                                        require_ready=lambda: _require_ready(runtime))
                        counts[outcome if outcome in counts and outcome != "scanned" else "failed"] += 1
                    except Exception:
                        counts["failed"] += 1
                try:
                    _require_ready(runtime)
                    runtime.store.save_recovery_checkpoint(expected, (1 - turn, *cursors),
                        require_ready=lambda: _require_ready(runtime))
                except Exception:
                    counts["failed"] += 1
            return RecoveryReport(**counts)
        finally:
            self._lock.release()


class UTCClock:
    def now(self):
        return datetime.now(timezone.utc)


def compose_runtime(*, settings, client, session_factory, sql_probe, agent,
                    processing_broker, outbound_broker, transport, clock=None, config=None):
    """The only runtime composition contract, usable unchanged by all adapters."""
    from app.conversation_redis import RedisConversationStore
    clock = clock or UTCClock()
    store = RedisConversationStore(client, config or ConversationConfig.from_settings(settings), clock)
    readiness = DependencyReadiness.for_dependencies(secret=lambda: settings.webhook_secret,
        sql_probe=sql_probe, store=store, broker=processing_broker)
    coordinator = ConversationCoordinator(store, clock, require_ready=lambda: _require_ready(runtime))
    runtime = ConversationRuntime(coordinator, store, session_factory,
        agent, processing_broker, outbound_broker, transport, clock, readiness.check)
    return runtime


def bounded_sql_dependencies(database_url, *, engine_factory=None):
    """Construct a lazy SQL pool; both connect and statement waits are bounded."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine_factory = engine_factory or create_engine
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if database_url.startswith("sqlite:"):
        options = {"connect_args": {"check_same_thread": False, "timeout": 2}, "echo": False}
    elif database_url.startswith(("postgresql:", "postgresql+psycopg2:")):
        options = {"connect_args": {"connect_timeout": 2,
            "options": "-c statement_timeout=2000 -c lock_timeout=2000 -c timezone=America/Sao_Paulo"},
            "pool_timeout": 2, "pool_size": 2, "max_overflow": 0, "echo": False}
    else:
        raise ValueError("unsupported SQL configuration")
    engine = engine_factory(database_url, **options)
    def probe():
        with engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar() == 1
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False), probe


def bounded_redis_client(url, *, client_factory=None):
    """Reject URL policy overrides and verify effective bounds without connecting."""
    from urllib.parse import urlsplit, parse_qsl
    import re
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    try:
        parts = urlsplit(url)
        # An explicit allowlist also closes case/encoding/duplicate-key variants
        # independently of redis-py's URL/keyword precedence in any version.
        if parts.scheme not in ("redis", "rediss") or not parts.hostname or parts.fragment:
            raise ValueError
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        if query and (len(query) != 1 or query[0][0] != "db" or not re.fullmatch(r"[0-9]+", query[0][1])):
            raise ValueError
        client = (client_factory or redis.Redis.from_url)(url,
            socket_connect_timeout=2, socket_timeout=2, retry_on_timeout=False,
            retry_on_error=[], retry=Retry(NoBackoff(), 0), decode_responses=True)
        effective = client.connection_pool.connection_kwargs
        if (any(type(effective.get(name)) not in (int, float) or not 0 < effective[name] <= 2
                for name in ("socket_connect_timeout", "socket_timeout"))
                or effective.get("retry_on_timeout") is not False
                or effective.get("retry_on_error") != []
                or not isinstance(effective.get("retry"), Retry)
                or getattr(effective["retry"], "_retries", None) != 0):
            raise ValueError
        return client
    except Exception:
        raise ValueError("invalid Redis configuration") from None


def build_runtime(*, settings, processing_task, outbound_task, celery):
    """Lazy client construction only; no SQL initialization or dependency writes."""
    import os
    from app.ai_agent import ai_agent
    from app.whatsapp_service import whatsapp_service
    from app.celery_app import CeleryProcessingBroker, CeleryOutboundBroker, probe_broker
    config = ConversationConfig.from_settings(settings)
    config = replace(config,
        recovery_page_size=int(os.environ.get("BATCH_RECOVERY_PAGE_SIZE", "100")),
        recovery_max_pages=int(os.environ.get("BATCH_RECOVERY_MAX_PAGES", "2")))
    if not (1 <= config.recovery_page_size <= 1000 and 1 <= config.recovery_max_pages <= 100):
        raise ValueError("invalid recovery limits")
    client = bounded_redis_client(settings.redis_url)
    sessions, sql_probe = bounded_sql_dependencies(settings.database_url)
    return compose_runtime(settings=settings, config=config, client=client,
        session_factory=sessions, sql_probe=sql_probe, agent=ai_agent,
        processing_broker=CeleryProcessingBroker(processing_task, probe=lambda: probe_broker(celery)),
        outbound_broker=CeleryOutboundBroker(outbound_task), transport=whatsapp_service)
