import json
import math
from uuid import UUID
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import pytest
import importlib
import importlib.util

from tests.fakes import ManualClock
from app.conversation_state import (
    AgentIntent,
    AgentResult,
    BrokerPort,
    ClosureTransitionRef,
    ConfigurationIssue,
    ConfigurationInvalid,
    ContactLease,
    ContactAnchor,
    ConversationGenerationUnavailable,
    ConversationConfig,
    ConversationCycle,
    ConversationSnapshot,
    ConversationState,
    ConversationStateUnavailable,
    ConversationStore,
    DependencyName,
    DependencyStatus,
    DispatchPhase,
    FailureReason,
    IngressDisposition,
    InvalidCanonicalContact,
    InvalidManualPauseDuration,
    MutationPhase,
    OutboundEnvelope,
    OutboundKind,
    PauseTransitionRef,
    ProcessingPhase,
    ReadinessReport,
    manual_pause_deadline,
    validate_manual_pause_hours,
)
from app.simple_config import Settings
from app.utils import normalize_phone


def recovery_api():
    assert importlib.util.find_spec("app.conversation_recovery") is not None, "readiness/recovery missing"
    return importlib.import_module("app.conversation_recovery")


def test_ready_coordinator_keeps_legacy_constructor_and_explicit_gate(transition_env):
    from app.conversation_state import ConversationCoordinator, ReadinessUnavailable
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    def closed():
        raise ReadinessUnavailable(FailureReason.READINESS_UNAVAILABLE)
    gated = ConversationCoordinator(store, clock, require_ready=closed)
    with store.contact_lease(PHONE) as lease:
        assert coordinator.resolve_ingress(db, PHONE, clock.now(), lease).cycle is ConversationCycle.OPEN
        with pytest.raises(ReadinessUnavailable):
            gated.pause_manual(db, PHONE, 1, "secretary_dashboard_pause", clock.now(), lease, "synthetic-operation")
        assert db.get(PausedContact, PHONE) is None
        assert store.read_details(lease) == ()


@pytest.mark.parametrize("failed", [(), ("secret",), ("sql",), ("redis",), ("epoch",),
                                  ("broker",), ("sql", "epoch", "broker")])
def test_ready_dependency_matrix_is_allowlisted_and_fail_closed(failed):
    api = recovery_api()
    probes = {name: (lambda name=name: name.value not in failed) for name in DependencyName}
    probes["private-host"] = lambda: pytest.fail("unknown dependency was probed")
    readiness = api.DependencyReadiness(probes)
    report = readiness.check()
    assert tuple(row.name for row in report.dependencies) == tuple(DependencyName)
    assert {row.name.value for row in report.dependencies if not row.ready} == set(failed)
    assert report.ready is (not failed)
    if failed:
        with pytest.raises(api.DependencyNotReady, match="conversation dependencies unavailable"):
            readiness.require_ready()
    else:
        readiness.require_ready()


@pytest.mark.parametrize("value", [None, 1, "ready", object(), RuntimeError("private-token")])
def test_ready_probe_missing_exception_and_nonboolean_never_open(value, caplog):
    api = recovery_api()
    def probe():
        if isinstance(value, Exception):
            raise value
        return value
    report = api.DependencyReadiness({DependencyName.SQL: probe}).check()
    assert report.ready is False
    assert all(row.ready is False for row in report.dependencies)
    assert "private-token" not in caplog.text + repr(report)


@pytest.mark.parametrize("fault", [None, "epoch_absent", "epoch_mismatch", "run_id_mismatch",
                                  "noeviction_invalid", "persistence_invalid"])
@pytest.mark.parametrize("_case", [None], ids=["state-41"])
def test_ready_real_attestation_separates_redis_availability_from_epoch(_case, fault):
    from tests.fakes import InMemoryConversationStore
    api = recovery_api()
    store = InMemoryConversationStore(ConversationConfig.from_settings(Settings(_valid_environment())))
    if fault:
        store.inject_fault(fault)
    readiness = api.DependencyReadiness.for_dependencies(secret=lambda: "synthetic",
        sql_probe=lambda: True, store=store, broker=type("Broker", (), {"probe": lambda self: True})())
    report = readiness.check()
    states = {row.name.value: row.ready for row in report.dependencies}
    assert states == {"secret": True, "sql": True, "redis": True, "epoch": fault is None, "broker": True}
    assert store.global_epoch_writes == 0


@pytest.mark.parametrize("fault", ["cas", "acl", "epoch", "run_id", "persistence", "invalid"])
def test_recovery_checkpoint_cas_attestation_and_argument_preflight(fault):
    from app.conversation_redis import RECOVERY_CHECKPOINT_KEY, GLOBAL_EPOCH_KEY
    from tests.fakes import InMemoryConversationStore
    from app.conversation_state import ConversationDomainError
    store = InMemoryConversationStore(ConversationConfig.from_settings(Settings(_valid_environment())))
    expected, position = store.recovery_checkpoint()
    assert position == (0, None, None)
    if fault == "cas":
        store.save_recovery_checkpoint(expected, (1, None, None))
    elif fault == "acl":
        store.client.denied_commands.add(("SET", RECOVERY_CHECKPOINT_KEY))
    elif fault == "epoch":
        store.before_atomic = lambda: store.client.values.update({GLOBAL_EPOCH_KEY: "unavailable"})
    elif fault == "run_id":
        store.before_atomic = lambda: store.client.server.update(run_id="unavailable")
    elif fault == "persistence":
        store.before_atomic = lambda: store.client.persistence.update(aof_last_write_status="err")
    before = store.client.get(RECOVERY_CHECKPOINT_KEY)
    with pytest.raises(ConversationDomainError):
        store.save_recovery_checkpoint(expected, (1, "private-token", None) if fault == "invalid" else (1, None, None))
    assert store.client.get(RECOVERY_CHECKPOINT_KEY) == before


@pytest.mark.parametrize("cursor", ["private-token", "[0,0]", "m1.WzAsLTFd", "m1.W3RydWUsMF0", "m1." + "A" * 200])
def test_recovery_mutation_invalid_cursor_is_sanitized_and_has_no_writes(cursor):
    from tests.fakes import InMemoryConversationStore
    store = InMemoryConversationStore(ConversationConfig.from_settings(Settings(_valid_environment())))
    before = store.snapshot()
    with pytest.raises(ConversationStateUnavailable) as raised:
        store.recoverable_mutations(store.clock.now(), cursor=cursor)
    assert cursor not in str(raised.value)
    assert store.snapshot() == before


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(51) 99999-0000", "5551999990000"),
        ("5551999990000@s.whatsapp.net", "5551999990000"),
        ("5551999990000@c.us", "5551999990000"),
    ],
)
def test_normalize_phone_accepts_only_canonical_identity(raw, expected):
    """Catches loss of Brazilian formatting/JID normalization."""
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "123", "0551999990000", "1" * 16, "5551999990000@g.us"])
def test_normalize_phone_rejects_invalid_identity(raw):
    """Catches malformed or non-individual identities entering state keys."""
    assert normalize_phone(raw) == ""


@pytest.mark.parametrize("value", [0, -1, "x", math.inf, math.nan, 1e308])
def test_manual_pause_rejects_invalid_values(value):
    """Catches invalid durations becoming unbounded or already-expired pauses."""
    with pytest.raises((ValueError, OverflowError)):
        validate_manual_pause_hours(value)


def test_manual_pause_rejects_booleans_despite_integer_subclassing():
    """Catches True silently becoming a one-hour manual pause."""
    with pytest.raises(InvalidManualPauseDuration) as raised:
        validate_manual_pause_hours(True)

    assert raised.value.reason_code is FailureReason.INVALID_TYPE


def test_manual_pause_hours_accepts_exact_maximum_and_rejects_above_it():
    """Catches an unbounded finite duration escaping the validator."""
    assert validate_manual_pause_hours(8760) == 8760.0

    with pytest.raises(InvalidManualPauseDuration) as raised:
        validate_manual_pause_hours(8760.01)

    assert raised.value.reason_code is FailureReason.ABOVE_MAXIMUM


def test_manual_pause_rejects_huge_integer_with_enumerated_reason():
    """Catches integer-to-float overflow escaping the domain exception contract."""
    with pytest.raises(InvalidManualPauseDuration) as raised:
        validate_manual_pause_hours(10 ** 400)

    assert raised.value.reason_code is FailureReason.ABOVE_MAXIMUM


def test_manual_pause_accepts_exact_maximum_and_rejects_later_deadline():
    """Catches an off-by-one error at the 365-day safety boundary."""
    now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)

    assert manual_pause_deadline(now, 24 * 365) == datetime(
        2027, 9, 12, 12, tzinfo=timezone.utc
    )
    with pytest.raises(InvalidManualPauseDuration) as raised:
        manual_pause_deadline(now, (24 * 365) + 0.01)

    assert raised.value.reason_code is FailureReason.ABOVE_MAXIMUM


@pytest.mark.parametrize("_case", [None], ids=["state-33"])
def test_manual_pause_translates_timedelta_overflow_to_domain_error(_case, ):
    """Catches huge finite input escaping as a platform OverflowError."""
    with pytest.raises(InvalidManualPauseDuration) as raised:
        manual_pause_deadline(datetime.max.replace(tzinfo=timezone.utc), 1)

    assert raised.value.reason_code is FailureReason.OVERFLOW


def _valid_environment() -> dict[str, str]:
    return {
        "ANTHROPIC_API_KEY": "synthetic-anthropic",
        "WASENDER_API_KEY": "synthetic-wasender",
        "WASENDER_PROJECT_NAME": "synthetic-project",
        "WASENDER_WEBHOOK_SECRET": "synthetic-webhook-secret",
        "DATABASE_URL": "sqlite://",
        "REDIS_URL": "redis://synthetic.invalid/0",
        "ADMIN_PASSWORD": "synthetic-admin",
        "CONVERSATION_COORDINATION_EPOCH": "00000000-0000-4000-8000-000000000001",
        "CONVERSATION_REDIS_EXPECTED_RUN_ID": "synthetic-run-id",
        "CONVERSATION_REDIS_ATTEST_NOEVICTION": "true",
        "CONVERSATION_REDIS_ATTEST_PERSISTENCE": "true",
        "CONTACT_LEASE_TTL_SECONDS": "60",
        "CONTACT_LEASE_HEARTBEAT_SECONDS": "15",
        "CLAIM_TTL_SECONDS": "45",
        "DISPATCH_RETRY_SECONDS": "900",
        "PROCESSING_RETRY_SECONDS": "600",
        "ENQUEUE_VISIBILITY_SECONDS": "60",
        "ENQUEUE_BACKOFF_SECONDS": "30",
        "REPLAY_WINDOW_SECONDS": "604800",
        "TTL_MARGIN_SECONDS": "300",
        "BATCH_RECOVERY_INTERVAL_SECONDS": "20",
    }


def test_settings_are_isolated_instances_backed_by_injected_environment():
    """Catches class attributes leaking one environment into another instance."""
    first_env = _valid_environment()
    second_env = _valid_environment()
    second_env["DATABASE_URL"] = "postgres://db.example/clinic"
    second_env["ADMIN_PASSWORD"] = "different-synthetic-admin"

    first = Settings(first_env)
    second = Settings(second_env)

    assert first.database_url == "sqlite://"
    assert first.admin_password == "synthetic-admin"
    assert second.database_url == "postgresql://db.example/clinic"
    assert second.admin_password == "different-synthetic-admin"


def test_valid_conversation_configuration_has_typed_values_and_no_issues():
    """Catches valid deployment settings being rejected or left as raw strings."""
    config = ConversationConfig.from_settings(Settings(_valid_environment()))

    assert config.coordination_epoch.hex == "00000000000040008000000000000001"
    assert config.contact_lease_ttl_seconds == 60
    assert config.contact_lease_heartbeat_seconds == 15
    assert config.claim_ttl_seconds == 45
    assert config.dispatch_retry_seconds == 900
    assert config.processing_retry_seconds == 600
    assert config.enqueue_visibility_seconds == 60
    assert config.enqueue_backoff_seconds == 30
    assert config.replay_window_seconds == 604800
    assert config.ttl_margin_seconds == 300
    assert config.batch_recovery_interval_seconds == 20
    assert config.redis_expected_run_id == "synthetic-run-id"
    assert config.issues == ()


@pytest.mark.parametrize(
    ("name", "attribute", "expected_issue"),
    [
        ("CONTACT_LEASE_TTL_SECONDS", "contact_lease_ttl_seconds", ConfigurationIssue.INVALID_CONTACT_LEASE_TTL),
        (
            "CONTACT_LEASE_HEARTBEAT_SECONDS",
            "contact_lease_heartbeat_seconds",
            ConfigurationIssue.INVALID_CONTACT_LEASE_HEARTBEAT,
        ),
        ("CLAIM_TTL_SECONDS", "claim_ttl_seconds", ConfigurationIssue.INVALID_CLAIM_TTL),
        ("DISPATCH_RETRY_SECONDS", "dispatch_retry_seconds", ConfigurationIssue.INVALID_DISPATCH_RETRY),
        ("PROCESSING_RETRY_SECONDS", "processing_retry_seconds", ConfigurationIssue.INVALID_PROCESSING_RETRY),
        (
            "ENQUEUE_VISIBILITY_SECONDS",
            "enqueue_visibility_seconds",
            ConfigurationIssue.INVALID_ENQUEUE_VISIBILITY,
        ),
        ("ENQUEUE_BACKOFF_SECONDS", "enqueue_backoff_seconds", ConfigurationIssue.INVALID_ENQUEUE_BACKOFF),
        ("REPLAY_WINDOW_SECONDS", "replay_window_seconds", ConfigurationIssue.INVALID_REPLAY_WINDOW),
        ("TTL_MARGIN_SECONDS", "ttl_margin_seconds", ConfigurationIssue.INVALID_TTL_MARGIN),
        (
            "BATCH_RECOVERY_INTERVAL_SECONDS",
            "batch_recovery_interval_seconds",
            ConfigurationIssue.INVALID_BATCH_RECOVERY_INTERVAL,
        ),
    ],
)
def test_malformed_duration_becomes_enumerated_configuration_issue(
    name, attribute, expected_issue
):
    """Catches numeric parsing errors bypassing fail-closed configuration reporting."""
    environment = _valid_environment()
    malformed = "synthetic-malformed-duration"
    environment[name] = malformed

    settings = Settings(environment)
    config = ConversationConfig.from_settings(settings)

    assert getattr(config, attribute) is None
    assert expected_issue in config.issues
    assert malformed not in repr(settings.__dict__)
    assert malformed not in repr(config)


def test_large_integer_lease_relationship_is_checked_without_float_conversion():
    """Catches overflow while validating heartbeat against a huge lease TTL."""
    environment = _valid_environment()
    huge_duration = "1" + ("0" * 400)
    environment["CONTACT_LEASE_TTL_SECONDS"] = huge_duration
    environment["CONTACT_LEASE_HEARTBEAT_SECONDS"] = huge_duration

    config = ConversationConfig.from_settings(Settings(environment))

    assert ConfigurationIssue.HEARTBEAT_EXCEEDS_LEASE_LIMIT in config.issues


@pytest.mark.parametrize(
    ("updates", "expected_issue"),
    [
        ({"CONVERSATION_COORDINATION_EPOCH": "not-a-uuid"}, ConfigurationIssue.INVALID_EPOCH),
        ({"CONVERSATION_REDIS_EXPECTED_RUN_ID": ""}, ConfigurationIssue.MISSING_REDIS_RUN_ID),
        ({"CONVERSATION_REDIS_ATTEST_NOEVICTION": "false"}, ConfigurationIssue.NOEVICTION_NOT_ATTESTED),
        ({"CONVERSATION_REDIS_ATTEST_PERSISTENCE": "false"}, ConfigurationIssue.PERSISTENCE_NOT_ATTESTED),
        ({"CONTACT_LEASE_TTL_SECONDS": "0"}, ConfigurationIssue.INVALID_CONTACT_LEASE_TTL),
        ({"CONTACT_LEASE_HEARTBEAT_SECONDS": "21"}, ConfigurationIssue.HEARTBEAT_EXCEEDS_LEASE_LIMIT),
        ({"CLAIM_TTL_SECONDS": "600"}, ConfigurationIssue.CLAIM_NOT_BELOW_PROCESSING_HORIZON),
        ({"ENQUEUE_VISIBILITY_SECONDS": "900"}, ConfigurationIssue.VISIBILITY_NOT_BELOW_DISPATCH_HORIZON),
        ({"ENQUEUE_BACKOFF_SECONDS": "900"}, ConfigurationIssue.BACKOFF_NOT_BELOW_DISPATCH_HORIZON),
    ],
)
def test_conversation_configuration_reports_fail_closed_issues(updates, expected_issue):
    """Catches unsafe coordination settings being accepted as ready."""
    environment = _valid_environment()
    environment.update(updates)

    config = ConversationConfig.from_settings(Settings(environment))

    assert expected_issue in config.issues


def test_invalid_configuration_does_not_retain_raw_epoch():
    """Catches invalid raw coordination values leaking through the typed contract."""
    environment = _valid_environment()
    environment["CONVERSATION_COORDINATION_EPOCH"] = "sensitive-malformed-value"

    config = ConversationConfig.from_settings(Settings(environment))

    assert config.coordination_epoch is None
    assert "sensitive-malformed-value" not in repr(config)


def test_readiness_requires_every_dependency():
    """Catches partially healthy deployments accepting conversation traffic."""
    report = ReadinessReport(
        (
            DependencyStatus(DependencyName.SECRET, True),
            DependencyStatus(DependencyName.SQL, True),
            DependencyStatus(DependencyName.REDIS, False),
            DependencyStatus(DependencyName.EPOCH, True),
            DependencyStatus(DependencyName.BROKER, True),
        )
    )

    assert report.ready is False


def test_domain_enums_serialize_to_the_cross_task_wire_values():
    """Catches a state/phase wire value drifting between adapters."""
    values = {
        "state": [item.value for item in ConversationState],
        "cycle": [item.value for item in ConversationCycle],
        "outbound": [item.value for item in OutboundKind],
        "ingress": [item.value for item in IngressDisposition],
        "mutation": [item.value for item in MutationPhase],
        "processing": [item.value for item in ProcessingPhase],
        "dispatch": [item.value for item in DispatchPhase],
        "intent": [item.value for item in AgentIntent],
    }

    assert values == {
        "state": ["BOT_ACTIVE", "SECRETARY_ATTENDANCE"],
        "cycle": ["OPEN", "PAUSED", "CLOSED", "MUTATING", "QUARANTINED"],
        "outbound": ["NORMAL", "TRANSFER_CONFIRMATION", "CLOSURE_CONFIRMATION"],
        "ingress": ["BUFFERED", "PROCESSED", "DROPPED", "APPLIED", "IGNORED", "FAILED", "DUPLICATE"],
        "mutation": ["PREPARED", "COMMITTING", "COMMITTED", "ABORTED", "QUARANTINED"],
        "processing": ["CLAIMED", "RESULT_READY", "APPLYING", "DONE", "QUARANTINED"],
        "dispatch": ["PENDING", "SCHEDULED", "STAGED", "PROCESSED", "EXHAUSTED"],
        "intent": ["SAVE_CONTEXT", "PAUSE_FOR_SECRETARY", "CLOSE_CONTEXT"],
    }


def test_outbound_envelope_serializes_nested_refs_to_json_primitives():
    """Catches enum/datetime objects leaking into the broker payload."""
    envelope = OutboundEnvelope(
        phone="5551999990000",
        text="synthetic response",
        kind=OutboundKind.TRANSFER_CONFIRMATION,
        generation="generation-1",
        processing_id="processing-1",
        operation_id="operation-1",
        pause_ref=PauseTransitionRef(
            generation="generation-1",
            paused_until=datetime(2026, 9, 13, 12, tzinfo=timezone.utc),
            reason="human_request",
        ),
    )

    payload = envelope.to_dict()

    assert payload == {
        "phone": "5551999990000",
        "text": "synthetic response",
        "kind": "TRANSFER_CONFIRMATION",
        "generation": "generation-1",
        "processing_id": "processing-1",
        "operation_id": "operation-1",
        "pause_ref": {
            "generation": "generation-1",
            "paused_until": "2026-09-13T12:00:00+00:00",
            "reason": "human_request",
        },
        "closure_ref": None,
    }
    assert json.loads(json.dumps(payload)) == payload


def test_snapshot_and_agent_result_are_frozen_domain_values():
    """Catches cross-task results becoming mutable after construction."""
    snapshot = ConversationSnapshot(
        phone="5551999990000",
        messages=[],
        current_flow=None,
        flow_data={},
        status="active",
        last_activity=None,
    )
    result = AgentResult("ok", [], None, {}, AgentIntent.SAVE_CONTEXT)
    closure = ClosureTransitionRef("generation-1", "operation-1")

    with pytest.raises(AttributeError):
        snapshot.phone = "5551888880000"
    with pytest.raises(AttributeError):
        result.text = "changed"
    with pytest.raises(AttributeError):
        closure.operation_id = "changed"


def test_domain_exceptions_expose_only_an_enumerated_reason_code():
    """Catches free-form or sensitive failure detail escaping the domain boundary."""
    error = ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE)

    assert error.reason_code is FailureReason.STATE_UNAVAILABLE
    assert vars(error) == {"reason_code": FailureReason.STATE_UNAVAILABLE}
    with pytest.raises(ValueError):
        ConfigurationInvalid("arbitrary-sensitive-detail")


def test_minimal_protocols_accept_real_structural_implementations():
    """Catches removal of the readiness/lease/broker seams required by later adapters."""
    class Store:
        def readiness(self):
            return ReadinessReport((DependencyStatus(DependencyName.REDIS, True),))

        def contact_lease(self, phone):
            return nullcontext(
                ContactLease(phone, "owner-1", datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc))
            )

    class Broker:
        def probe(self):
            return True

    store: ConversationStore = Store()
    broker: BrokerPort = Broker()

    assert store.readiness().ready is True
    with store.contact_lease("5551999990000") as lease:
        assert lease.phone == "5551999990000"
    assert broker.probe() is True


def test_manual_clock_rejects_naive_initial_and_set_values():
    """Catches timezone ambiguity entering deterministic concurrency tests."""
    with pytest.raises(ValueError):
        ManualClock(datetime(2026, 9, 12, 12))

    clock = ManualClock(datetime(2026, 9, 12, 12, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        clock.set(datetime(2026, 9, 12, 13))


def test_manual_clock_advances_monotonically_and_returns_new_time():
    """Catches deterministic tests accidentally moving time backwards."""
    start = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    clock = ManualClock(start)

    assert clock.now() == start
    assert clock.advance(timedelta(seconds=15)) == datetime(
        2026, 9, 12, 12, 0, 15, tzinfo=timezone.utc
    )
    assert clock.now() == datetime(2026, 9, 12, 12, 0, 15, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        clock.advance(timedelta(microseconds=-1))


@pytest.mark.parametrize("revision,generation,history", [
    (-1, UUID(int=1), (UUID(int=1),)),
    (True, UUID(int=1), (UUID(int=1),)),
    (0, 1, (UUID(int=1),)),
    (0, UUID(int=1), ()),
    (0, UUID(int=1), (UUID(int=2),)),
    (0, UUID(int=1), (UUID(int=1), UUID(int=1))),
])
def test_anchor_domain_rejects_invalid_revision_or_generation_lineage(revision, generation, history):
    """Catches invalid anchor fences entering adapters through typed domain construction."""
    with pytest.raises(ConversationGenerationUnavailable) as raised:
        ContactAnchor(revision, generation, (), "unused", history)
    assert raised.value.reason_code is FailureReason.GENERATION_UNAVAILABLE


@pytest.fixture
def transition_env(session_factory):
    from app import conversation_state
    from tests.fakes import BarrierSession
    from tests.test_conversation_concurrency import make_store
    store = make_store()
    with session_factory() as session:
        db = BarrierSession(session)
        coordinator_type = getattr(conversation_state, "ConversationCoordinator", None)
        coordinator = coordinator_type(store, store.clock) if coordinator_type else None
        yield coordinator, db, store, store.clock


PHONE = "5551999990000"
OTHER = "5551888880000"
TEST_PHONE = "5500000000000"


@pytest.mark.parametrize("_case", [None], ids=["state-10-never-reused-generation"])
def test_pause_mutations_and_rejected_negative_extension_cannot_restore_generation(transition_env, _case):
    coordinator, db, store, clock = transition_env
    generations = []
    with store.contact_lease(PHONE) as lease:
        generations.append(coordinator.resolve_ingress(db, PHONE, clock.now(), lease).generation)
        generations.append(coordinator.pause_manual(db, PHONE, 2, "secretary_dashboard_pause", clock.now(), lease, "manual").generation)
        generations.append(coordinator.extend_pause(db, PHONE, 1, clock.now(), lease, "extend").generation)
        before = store.contact_snapshot(PHONE)
        with pytest.raises(InvalidManualPauseDuration):
            coordinator.extend_pause(db, PHONE, -1, clock.now(), lease, "negative")
        assert store.contact_snapshot(PHONE) == before
        coordinator.unpause(db, PHONE, clock.now(), lease, "resume")
        generations.append(str(store.read_anchor(lease).last_generation))
        generations.append(coordinator.pause_manual(db, PHONE, 2, "secretary_dashboard_pause", clock.now(), lease, "again").generation)
    assert len(set(generations)) == len(generations)
    assert all(UUID(value).version == 4 for value in generations)


@pytest.mark.parametrize("hours", [0, -1, "invalid", float("inf"), float("nan")],
                         ids=["state-11-zero", "state-11-negative", "state-11-text", "state-11-infinity", "state-11-nan"])
def test_invalid_manual_duration_has_no_preparation_or_sql_effect(transition_env, hours):
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        before = store.snapshot()
        with pytest.raises(InvalidManualPauseDuration):
            coordinator.pause_manual(db, PHONE, hours, "secretary_dashboard_pause", clock.now(), lease, "invalid")
        assert store.snapshot() == before
        assert db.events == []


@pytest.mark.parametrize("phone", [
    pytest.param("", id="state-09-empty"), pytest.param("123", id="state-09-short"),
    pytest.param("1111111111111111", id="state-09-long"),
])
def test_invalid_contact_never_initializes_coordination_or_sql(transition_env, phone):
    coordinator, db, store, clock = transition_env
    before = store.snapshot()
    with pytest.raises(InvalidCanonicalContact):
        with store.contact_lease(phone) as lease:
            coordinator.resolve_ingress(db, phone, clock.now(), lease)
    assert store.snapshot() == before
    assert db.events == []


@pytest.mark.parametrize("change", ["missing", "renewed", "removed", "old_generation"],
                         ids=lambda value: "state-13-" + value)
def test_state_transfer_reference_rejects_every_stale_binding(transition_env, change):
    from dataclasses import replace
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        reference = coordinator.pause_for_secretary(db, PHONE, "user_requested_human_assistance",
                                                   clock.now(), lease, "transfer")
        outbound = OutboundEnvelope(PHONE, "synthetic", OutboundKind.TRANSFER_CONFIRMATION,
                                    reference.generation, "processing", "transfer", pause_ref=reference)
        if change == "missing":
            outbound = replace(outbound, pause_ref=None)
        elif change == "old_generation":
            outbound = replace(outbound, generation="00000000-0000-4000-8000-000000000099")
        elif change == "renewed":
            clock.advance(timedelta(seconds=1))
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "renewal")
        else:
            coordinator.unpause(db, PHONE, clock.now(), lease, "unpause")
        before = db.events.count("commit_entered")
        assert coordinator.may_send(db, outbound, clock.now(), lease) is False
        assert db.events.count("commit_entered") == before


@pytest.mark.parametrize("intent", [AgentIntent.SAVE_CONTEXT, AgentIntent.PAUSE_FOR_SECRETARY],
                         ids=["state-15-save", "state-15-transfer"])
def test_nested_coordinator_operations_use_one_top_level_lease(transition_env, intent):
    coordinator, db, store, clock = transition_env
    before = store.client.operation_calls.get("acquire", 0)
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        result = AgentResult("synthetic", [], None, {}, intent)
        outbound = coordinator.apply_agent_result(db, PHONE, result, "processing", "operation", clock.now(), lease)
        assert coordinator.may_send(db, outbound, clock.now(), lease)
        assert store.client.operation_calls["acquire"] == before + 1
    assert store.client.operation_calls["acquire"] == before + 1


@pytest.mark.parametrize("fail_prepare", [False, True], ids=["state-19-prepared", "state-19-failed-cas"])
def test_preparation_persists_complete_barrier_before_first_sql_effect(transition_env, fail_prepare):
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        opened = coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        db.events.clear()
        before = store.contact_snapshot(PHONE)
        if fail_prepare:
            store.fail_next_atomic("prepare_mutation")
            with pytest.raises(ConversationStateUnavailable):
                coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "prepare-proof")
            assert store.contact_snapshot(PHONE) == before
            assert not any(event in db.events for event in ("execute", "add_returned", "flush_entered", "commit_entered"))
        else:
            def first_dml():
                attempt = store.inspect_mutation(PHONE, "prepare-proof", lease)
                assert attempt.phase is MutationPhase.PREPARED
                assert attempt.operation_id == "prepare-proof"
                assert attempt.prior_cycle is ConversationCycle.OPEN
                assert attempt.target_cycle is ConversationCycle.PAUSED
                assert str(attempt.generation) != opened.generation
                assert UUID(opened.generation) in store.read_anchor(lease).generation_history
                assert attempt.target_fingerprint
                assert store.read_anchor(lease).cycle is ConversationCycle.MUTATING
            db.hooks["execute"] = first_dml
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "prepare-proof")
            assert "execute" in db.events


@pytest.mark.parametrize("fault", ["epoch", "owner", "expired_lease", "phase"],
                         ids=lambda value: "state-24-" + value)
def test_committing_cas_rechecks_every_authority_before_sql_commit(transition_env, fault, conversation_resources):
    from app.conversation_redis import contact_keys
    from app.conversation_state import ConversationDomainError
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        def change_authority():
            keys = contact_keys(PHONE)
            if fault == "epoch":
                store.delete_global_epoch()
            elif fault == "owner":
                store.client.values[keys.lease] = "different-owner"
            elif fault == "expired_lease":
                clock.advance(timedelta(seconds=61))
            else:
                key = next(key for key in store.client.values if key.startswith(keys.mutation_prefix))
                value = json.loads(store.client.values[key])
                assert value["body"]["operation_id"] == "cas-proof"
                value["body"]["phase"] = "ABORTED"
                store.client.values[key] = json.dumps(value)
        store.client.before_operation["enter_committing"] = change_authority
        with pytest.raises(ConversationDomainError) as caught:
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "cas-proof")
        assert caught.value.reason_code.value == {
            "epoch": "readiness_unavailable", "owner": "contact_lease_lost",
            "expired_lease": "contact_lease_lost", "phase": "conversation_generation_unavailable",
        }[fault]
        assert "flush" in db.events
        assert "commit_entered" not in db.events
        assert "rollback" in db.events
    if fault in ("epoch", "owner"):
        conversation_resources.expect_unreleased_lease(store, lease, reason="CAS fault prevents old-owner release",
            owner_token="different-owner" if fault == "owner" else None)


@pytest.mark.parametrize("checkpoint", ["commit_entered", "commit_returned"],
                         ids=["state-35-normal-barrier", "normal-commit-returned-baseline"])
def test_normal_context_commit_is_fenced_until_redis_finalization(transition_env, checkpoint):
    from app.models import ConversationContext
    from app.conversation_state import ConversationMutationPending
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        result = AgentResult("synthetic", [{"role": "user", "content": "new context"}], None, {}, AgentIntent.SAVE_CONTEXT)
        def observe_barrier():
            attempt = store.inspect_mutation(PHONE, "normal-save", lease)
            assert attempt.phase is MutationPhase.COMMITTING
            assert store.read_anchor(lease).cycle is ConversationCycle.MUTATING
            assert "flush" in db.events
            with pytest.raises(ConversationMutationPending):
                coordinator.apply_agent_result(db, PHONE, result, "competitor", "competing-save", clock.now(), lease)
        db.hooks[checkpoint] = observe_barrier
        coordinator.apply_agent_result(db, PHONE, result, "processing", "normal-save", clock.now(), lease)
        assert db.events.count("commit_entered") == 1
        assert db.get(ConversationContext, PHONE).messages == [{"role": "user", "content": "new context"}]
        assert store.inspect_mutation(PHONE, "normal-save", lease).phase is MutationPhase.COMMITTED


def seed_context(db, phone, now):
    from app.models import ConversationContext
    assert db.get_bind().url.database in (None, "", ":memory:")
    row = ConversationContext(phone=phone, messages=[{"role": "user", "content": "synthetic"}],
                              flow_data={"synthetic": True}, current_flow="duvidas",
                              status="active", last_activity=now.replace(tzinfo=None), created_at=now.replace(tzinfo=None))
    db.add(row)
    db.session.commit()
    return row


@pytest.mark.parametrize("_case", [None], ids=["state-01"])
def test_state_ingress_without_pause_is_open_and_does_not_commit(_case, transition_env):
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        resolved = coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        assert resolved.state is ConversationState.BOT_ACTIVE
        assert resolved.cycle is ConversationCycle.OPEN
        assert UUID(resolved.generation) == store.read_anchor(lease).last_generation
    assert "commit_entered" not in db.events


@pytest.mark.parametrize("delta,paused", [
    pytest.param(-1, True, id="state-02-before"),
    pytest.param(0, False, id="state-03-exact"),
    pytest.param(1, False, id="state-04-after"),
])
def test_pause_exact_expiry_boundary(transition_env, delta, paused):
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        ref = coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "op-1")
    clock.set(ref.paused_until + timedelta(microseconds=delta))
    with store.contact_lease(PHONE) as lease:
        result = coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        assert (result.state is ConversationState.SECRETARY_ATTENDANCE) is paused
        assert (result.generation == ref.generation) is paused
        assert (db.get(PausedContact, PHONE) is not None) is paused
        assert result.cycle is (ConversationCycle.PAUSED if paused else ConversationCycle.OPEN)


@pytest.mark.parametrize("_case", [None], ids=["state-05"])
def test_pause_transfer_atomically_deletes_context_and_preserves_other_contact(_case, transition_env):
    from app.models import ConversationContext, PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        other = seed_context(db, OTHER, clock.now())
        ref = coordinator.pause_for_secretary(db, PHONE, "user_requested_human_assistance", clock.now(), lease, "op-transfer")
        assert db.get(ConversationContext, PHONE) is None
        assert db.get(ConversationContext, OTHER).messages == other.messages
        assert db.get(PausedContact, PHONE).paused_until == datetime(2026, 9, 13, 12)
        assert ref.paused_until == datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
        assert ref.reason == "user_requested_human_assistance"
        assert store.read_anchor(lease).cycle is ConversationCycle.PAUSED
    assert db.events.count("commit_entered") == 1


@pytest.mark.parametrize("_case", [None], ids=["state-06"])
def test_pause_beatriz_renewal_rotates_generation_but_retry_does_not(_case, transition_env):
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        first = coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "op-1")
    clock.advance(timedelta(hours=1))
    with store.contact_lease(PHONE) as lease:
        second = coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "op-2")
        clock.advance(timedelta(seconds=1))
        retry = coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "op-2")
        assert second == retry
        assert second.generation != first.generation
        assert second.paused_until == datetime(2026, 9, 13, 13, tzinfo=timezone.utc)
        assert len(store.read_anchor(lease).generation_history) == 3
    assert db.events.count("commit_entered") == 2


@pytest.mark.parametrize("_case", [None], ids=["state-33"])
def test_pause_manual_and_extension_enforce_resulting_365_day_limit(_case, transition_env):
    from app.models import ConversationContext, PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        ref = coordinator.pause_manual(db, PHONE, 8759, "secretary_dashboard_pause", clock.now(), lease, "manual-1")
        assert db.get(ConversationContext, PHONE) is not None
        extended = coordinator.extend_pause(db, PHONE, 1, clock.now(), lease, "extend-1")
        assert extended.paused_until == datetime(2027, 9, 12, 12, tzinfo=timezone.utc)
        assert extended.generation != ref.generation
        baseline = store.contact_snapshot(PHONE)
        for action in (
            lambda: coordinator.pause_manual(db, PHONE, 8761, "secretary_dashboard_pause", clock.now(), lease, "invalid-1"),
            lambda: coordinator.extend_pause(db, PHONE, 1, clock.now(), lease, "invalid-2"),
        ):
            with pytest.raises(InvalidManualPauseDuration):
                action()
        assert baseline == store.contact_snapshot(PHONE)
        coordinator.unpause(db, PHONE, clock.now(), lease, "unpause-1")
        assert db.get(PausedContact, PHONE) is None
        assert db.get(ConversationContext, PHONE) is not None
        assert store.read_anchor(lease).cycle is ConversationCycle.OPEN


@pytest.mark.parametrize("_case", [None], ids=["state-31"])
def test_closed_new_ingress_rotates_generation_and_invalidates_closure_send(_case, transition_env):
    from app.models import ConversationContext
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        ref = coordinator.close_context(db, PHONE, clock.now(), lease, "close-1")
        assert db.get(ConversationContext, PHONE) is None
        assert store.read_anchor(lease).cycle is ConversationCycle.CLOSED
        envelope = OutboundEnvelope(PHONE, "synthetic", OutboundKind.CLOSURE_CONFIRMATION,
                                    ref.generation, None, "close-1", closure_ref=ref)
        assert coordinator.may_send(db, envelope, clock.now(), lease)
        reopened = coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        assert reopened.cycle is ConversationCycle.OPEN
        assert reopened.generation != ref.generation
        assert not coordinator.may_send(db, envelope, clock.now(), lease)


@pytest.mark.parametrize("_case", [None], ids=["state-12"])
def test_send_exact_pause_reference_and_current_open_generation_only(_case, transition_env):
    from dataclasses import replace
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        opened = coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        normal = OutboundEnvelope(PHONE, "synthetic", OutboundKind.NORMAL, opened.generation, "p-1", "op-1")
        assert coordinator.may_send(db, normal, clock.now(), lease)
        ref = coordinator.pause_for_secretary(db, PHONE, "user_requested_human_assistance", clock.now(), lease, "pause-1")
        transfer = OutboundEnvelope(PHONE, "synthetic", OutboundKind.TRANSFER_CONFIRMATION,
                                    ref.generation, "p-1", "pause-1", pause_ref=ref)
        assert coordinator.may_send(db, transfer, clock.now(), lease)
        assert not coordinator.may_send(db, normal, clock.now(), lease)
        for bad_ref in (replace(ref, reason="wrong"), replace(ref, paused_until=ref.paused_until + timedelta(microseconds=1))):
            assert not coordinator.may_send(db, replace(transfer, pause_ref=bad_ref), clock.now(), lease)
        coordinator.unpause(db, PHONE, clock.now(), lease, "unpause-1")
        assert not coordinator.may_send(db, transfer, clock.now(), lease)


@pytest.mark.parametrize("kind", list(OutboundKind))
@pytest.mark.parametrize("_case", [None], ids=["state-14"])
def test_send_missing_coordination_never_initializes_contact(_case, transition_env, kind):
    """Sender authorization must not recreate state after coordination loss."""
    from app.models import ConversationContext, PausedContact
    coordinator, db, store, clock = transition_env
    outbound = OutboundEnvelope(PHONE, "synthetic", kind,
                                "00000000-0000-4000-8000-000000000002", "p-1", "op-1")
    with store.contact_lease(PHONE) as lease:
        before = store.snapshot()
        with pytest.raises(ConversationGenerationUnavailable) as raised:
            coordinator.may_send(db, outbound, clock.now(), lease)
        assert raised.value.reason_code is FailureReason.GENERATION_UNAVAILABLE
        assert store.snapshot() == before
        assert db.get(ConversationContext, PHONE) is None
        assert db.get(PausedContact, PHONE) is None
        assert "execute" not in db.events
        assert "flush_entered" not in db.events
        assert "commit_entered" not in db.events


@pytest.mark.parametrize("intent,kind,cycle", [
    (AgentIntent.SAVE_CONTEXT, OutboundKind.NORMAL, ConversationCycle.OPEN),
    (AgentIntent.PAUSE_FOR_SECRETARY, OutboundKind.TRANSFER_CONFIRMATION, ConversationCycle.PAUSED),
    (AgentIntent.CLOSE_CONTEXT, OutboundKind.CLOSURE_CONFIRMATION, ConversationCycle.CLOSED),
])
@pytest.mark.parametrize("_case", [None], ids=["state-17"])
def test_mutation_agent_result_is_persisted_once_with_typed_send(_case, transition_env, intent, kind, cycle):
    from app.models import ConversationContext
    coordinator, db, store, clock = transition_env
    result = AgentResult("synthetic", [{"role": "assistant", "content": "synthetic"}], "duvidas", {"done": True}, intent)
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        outbound = coordinator.apply_agent_result(db, PHONE, result, "p-1", "op-1", clock.now(), lease)
        retry = coordinator.apply_agent_result(db, PHONE, result, "p-1", "op-1", clock.now(), lease)
        assert retry == outbound
        assert outbound.kind is kind
        assert store.read_anchor(lease).cycle is cycle
        assert coordinator.may_send(db, outbound, clock.now(), lease)
        row = db.get(ConversationContext, PHONE)
        if intent is AgentIntent.SAVE_CONTEXT:
            assert (row.messages, row.current_flow, row.flow_data, row.status) == (result.messages, "duvidas", {"done": True}, "active")
            assert row.last_activity == datetime(2026, 9, 12, 12)
        else:
            assert row is None
    assert db.events.count("commit_entered") == 1


@pytest.mark.parametrize("age,paused,deleted", [(59, False, False), (60, False, False), (61, False, True), (61, True, True)])
def test_state_inactivity_conditional_delete_preserves_pause_and_other_contact(transition_env, age, paused, deleted):
    from app.models import ConversationContext
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now() - timedelta(minutes=age))
        seed_context(db, OTHER, clock.now() - timedelta(minutes=90))
        if paused:
            coordinator.pause_manual(db, PHONE, 24, "secretary_dashboard_pause", clock.now(), lease, "pause-1")
        changed = coordinator.close_inactive_context(db, PHONE, clock.now() - timedelta(hours=1), clock.now(), lease, "clean-1")
        assert changed is deleted
        assert (db.get(ConversationContext, PHONE) is None) is deleted
        assert db.get(ConversationContext, OTHER) is not None
        assert store.read_anchor(lease).cycle is (ConversationCycle.PAUSED if paused else ConversationCycle.CLOSED if deleted else ConversationCycle.OPEN)


def test_mutation_reset_is_exact_test_contact_only(transition_env):
    from app.models import Appointment, ConversationContext, PausedContact
    coordinator, db, store, clock = transition_env
    for phone in (TEST_PHONE, OTHER):
        with store.contact_lease(phone) as lease:
            coordinator.resolve_ingress(db, phone, clock.now(), lease)
            seed_context(db, phone, clock.now())
            coordinator.pause_manual(db, phone, 24, "secretary_dashboard_pause", clock.now(), lease, "pause-1")
        db.add(Appointment(patient_name="synthetic", patient_phone=phone, patient_birth_date="01/01/2000",
                           appointment_date="20260914", appointment_time="12:00",
                           created_at=clock.now().replace(tzinfo=None), updated_at=clock.now().replace(tzinfo=None)))
        db.session.commit()
    with store.contact_lease(OTHER) as lease:
        with pytest.raises(InvalidCanonicalContact):
            coordinator.reset_test_state(db, OTHER, clock.now(), lease, "reset-bad")
    with store.contact_lease(TEST_PHONE) as lease:
        coordinator.reset_test_state(db, TEST_PHONE, clock.now(), lease, "reset-1")
        assert db.get(ConversationContext, TEST_PHONE) is None
        assert db.get(PausedContact, TEST_PHONE) is None
        assert db.query(Appointment).filter_by(patient_phone=TEST_PHONE).count() == 0
        assert store.read_anchor(lease).cycle is ConversationCycle.OPEN
    assert db.get(ConversationContext, OTHER) is not None
    assert db.get(PausedContact, OTHER) is not None
    assert db.query(Appointment).filter_by(patient_phone=OTHER).count() == 1


@pytest.mark.parametrize("_case", [None], ids=["state-40"])
def test_state_existing_sql_without_anchor_fails_closed(_case, transition_env):
    coordinator, db, store, clock = transition_env
    seed_context(db, PHONE, clock.now())
    with store.contact_lease(PHONE) as lease:
        with pytest.raises(ConversationGenerationUnavailable):
            coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
    assert "commit_entered" not in db.events


def test_state_sql_read_failure_has_only_enumerated_error(transition_env, monkeypatch):
    coordinator, db, store, clock = transition_env
    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic-private-database-error")
    monkeypatch.setattr(db.session, "get", fail)
    with store.contact_lease(PHONE) as lease:
        with pytest.raises(ConversationStateUnavailable) as caught:
            coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        assert caught.value.reason_code is FailureReason.STATE_UNAVAILABLE
        assert "synthetic-private" not in str(caught.value)


def test_mutation_runner_committed_retry_never_reapplies_dml(transition_env):
    from app.conversation_state import MutationTarget
    from app.models import ConversationContext
    from sqlalchemy import delete
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        target = MutationTarget("CLOSE_CONTEXT", "synthetic-request-hash", coordinator._db_hash(db, PHONE), ConversationCycle.CLOSED)
        def dml(session, _attempt):
            session.execute(delete(ConversationContext).where(ConversationContext.phone == PHONE))
        first = coordinator._run_mutation(db, PHONE, "CLOSE_CONTEXT", target, lease, "close-1", clock.now(), dml)
        second = coordinator._run_mutation(db, PHONE, "CLOSE_CONTEXT", target, lease, "close-1", clock.now(), dml)
        assert first == second
        assert db.get(ConversationContext, PHONE) is None
        assert db.events.count("execute") == 1
        assert db.events.count("commit_entered") == 1


@pytest.mark.parametrize("intent", list(AgentIntent))
def test_mutation_new_agent_result_is_blocked_during_administrative_pause(transition_env, intent):
    from app.conversation_state import ConversationMutationPending
    from app.models import ConversationContext
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        coordinator.pause_manual(db, PHONE, 24, "secretary_dashboard_pause", clock.now(), lease, "pause-1")
        before = store.contact_snapshot(PHONE)
        with pytest.raises(ConversationMutationPending):
            coordinator.apply_agent_result(db, PHONE, AgentResult("synthetic", [], None, {}, intent), "p-1", "agent-1", clock.now(), lease)
        assert store.contact_snapshot(PHONE) == before
        assert db.get(ConversationContext, PHONE) is not None
        assert db.events.count("commit_entered") == 1


def test_closed_direct_close_rejects_valid_pause_without_changing_sql_or_cycle(transition_env):
    from app.conversation_state import ConversationMutationPending
    from app.models import PausedContact, ConversationContext
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        coordinator.pause_manual(db, PHONE, 24, "secretary_dashboard_pause", clock.now(), lease, "pause-1")
        before = store.contact_snapshot(PHONE)
        with pytest.raises(ConversationMutationPending):
            coordinator.close_context(db, PHONE, clock.now(), lease, "close-1")
        assert store.contact_snapshot(PHONE) == before
        assert db.get(PausedContact, PHONE) is not None
        assert db.get(ConversationContext, PHONE) is not None
        assert db.events.count("commit_entered") == 1


@pytest.mark.parametrize("reason,allowed", [("secretary_dashboard_pause", False),
                                           ("secretary_manual_pause", False),
                                           ("user_requested_human_assistance", True)])
def test_send_transfer_requires_patient_requested_reason(transition_env, reason, allowed):
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        ref = coordinator.pause_for_secretary(db, PHONE, reason, clock.now(), lease, "pause-1")
        outbound = OutboundEnvelope(PHONE, "synthetic", OutboundKind.TRANSFER_CONFIRMATION,
                                    ref.generation, "p-1", "pause-1", pause_ref=ref)
        assert coordinator.may_send(db, outbound, clock.now(), lease) is allowed


@pytest.mark.parametrize("method", ["secretary", "manual"])
def test_pause_freeform_reason_is_rejected_before_sql_or_redis_metadata(transition_env, method):
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        before = store.contact_snapshot(PHONE)
        with pytest.raises(ConversationStateUnavailable) as caught:
            if method == "secretary":
                coordinator.pause_for_secretary(db, PHONE, "synthetic-private-note", clock.now(), lease, "pause-1")
            else:
                coordinator.pause_manual(db, PHONE, 24, "synthetic-private-note", clock.now(), lease, "pause-1")
        assert caught.value.reason_code is FailureReason.INVALID_VALUE
        assert "synthetic-private-note" not in str(caught.value)
        assert store.contact_snapshot(PHONE) == before
        assert db.get(PausedContact, PHONE) is None
        assert "commit_entered" not in db.events


def test_pause_extend_maps_legacy_freeform_reason_to_safe_administrative_code(transition_env):
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.pause_manual(db, PHONE, 24, "secretary_dashboard_pause", clock.now(), lease, "pause-1")
        db.get(PausedContact, PHONE).reason = "synthetic-private-note"
        db.session.commit()
        ref = coordinator.extend_pause(db, PHONE, 1, clock.now(), lease, "extend-1")
        assert ref.reason == "secretary_dashboard_pause"
        assert db.get(PausedContact, PHONE).reason == "secretary_dashboard_pause"
        assert "synthetic-private-note" not in json.dumps(store.contact_snapshot(PHONE))


# Task 4: real store algorithms with synthetic Redis/broker boundaries.
def batch_api():
    from app import conversation_state as domain
    assert hasattr(domain, "ProcessingCommand"), "Task 4 processing command is missing"
    return domain


def append_batch(store, lease, *, message_id="synthetic-id", content="synthetic text"):
    domain = batch_api()
    anchor = store.read_anchor(lease)
    envelope = domain.InboundEnvelope("text", content, store.clock.now(), str(anchor.last_generation), message_id)
    return store.finalize_ingress_once(lease.phone, envelope, message_id,
                                      str(anchor.last_generation), lease)


def batch_command(store, lease, receipt):
    domain = batch_api()
    return domain.ProcessingCommand(lease.phone, receipt.batch_id, str(store.config.coordination_epoch),
                                    str(store.read_anchor(lease).last_generation))


def batch_details(store, lease, kind):
    return [item for item in store.read_details(lease) if item.entry.kind == kind]


@pytest.mark.parametrize("_case", [None], ids=["state-32"])
def test_dedup_buffer_accepts_once_and_preserves_raw_id_only_in_envelope(_case, ):
    from tests.test_conversation_concurrency import make_store
    import hashlib
    domain, store = batch_api(), make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        first = append_batch(store, lease)
        second = append_batch(store, lease, content="changed replay content")
        assert first.disposition is domain.IngressDisposition.BUFFERED
        assert second.disposition is domain.IngressDisposition.DUPLICATE
        assert second.batch_id == first.batch_id
        assert len(batch_details(store, lease, "batch")) == 1
        buffer = batch_details(store, lease, "buffer")[0]
        assert [e["content"] for e in buffer.body["envelopes"]] == ["synthetic text"]
        assert buffer.body["envelopes"][0]["message_id"] == "synthetic-id"
        digest = batch_details(store, lease, "dedupe")[0]
        assert digest.entry.id == hashlib.sha256(b"synthetic-id").hexdigest()
        assert "synthetic-id" not in json.dumps(dict(digest.body))
        assert digest.entry.expected_until >= store.clock.now() + timedelta(days=7)
        assert len(store.recoverable_batches().commands) == 1


def test_buffer_without_id_accepts_twice_without_replay_guarantee():
    from tests.test_conversation_concurrency import make_store
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        first = append_batch(store, lease, message_id=None)
        second = append_batch(store, lease, message_id=None)
        assert first.batch_id == second.batch_id
        assert len(batch_details(store, lease, "buffer")[0].body["envelopes"]) == 2
        assert batch_details(store, lease, "dedupe") == []


@pytest.mark.parametrize("action", ["DROPPED", "IGNORED"])
@pytest.mark.parametrize("_case", [None], ids=["flow-55"])
def test_dedup_dropped_and_ignored_retain_no_message_content(_case, transition_env, action):
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        ref = coordinator.pause_manual(db, PHONE, 24 * 30, "secretary_dashboard_pause", clock.now(), lease, "pause-long")
        receipt = store.finalize_ingress_once(PHONE, None, "synthetic-drop", ref.generation, lease,
                    disposition=domain.IngressDisposition(action), paused_until=ref.paused_until)
        assert receipt.disposition.value == action
        detail = batch_details(store, lease, "dedupe")[0]
        if action == "DROPPED":
            assert detail.entry.expected_until >= ref.paused_until + timedelta(days=7, seconds=300)
        assert batch_details(store, lease, "buffer") == []
        assert store.recoverable_batches().commands == ()
    clock.advance(timedelta(days=31))
    with store.contact_lease(PHONE) as lease:
        if action == "DROPPED":
            replay = store.finalize_ingress_once(PHONE, None, "synthetic-drop", ref.generation, lease,
                        disposition=domain.IngressDisposition.DROPPED, paused_until=ref.paused_until)
            assert replay.disposition is domain.IngressDisposition.DUPLICATE


def test_dedup_secretary_command_applied_only_with_committed_finalization(transition_env):
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        resolution = coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        pending = store.finalize_ingress_once(PHONE, None, "synthetic-command", resolution.generation, lease,
                                             disposition=domain.IngressDisposition.APPLIED)
        assert pending.disposition is None
        replay = store.finalize_ingress_once(PHONE, None, "synthetic-command", resolution.generation, lease,
                                            disposition=domain.IngressDisposition.APPLIED)
        assert replay.operation_id == pending.operation_id
        assert batch_details(store, lease, "dedupe")[0].body["disposition"] is None
        store.fail_next_atomic("finalize_committed")
        with pytest.raises(domain.ConversationMutationAmbiguous):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, pending.operation_id)
        assert batch_details(store, lease, "dedupe")[0].body["disposition"] is None
        store.finalize_committed(PHONE, pending.operation_id, lease, clock.now())
        assert batch_details(store, lease, "dedupe")[0].body["disposition"] == "APPLIED"
        replay = store.finalize_ingress_once(PHONE, None, "synthetic-command", resolution.generation, lease,
                                            disposition=domain.IngressDisposition.APPLIED)
        assert replay.disposition is domain.IngressDisposition.DUPLICATE


@pytest.mark.parametrize("outcome,delay,phase", [("CONFIRMED", 60, "SCHEDULED"),
    ("DEFINITIVE_FAILURE", 30, "PENDING"), ("AMBIGUOUS", 60, "PENDING")])
@pytest.mark.parametrize("offset,called", [(-1, False), (0, True), (1, True)])
@pytest.mark.parametrize("_case", [None], ids=["state-48"])
def test_enqueue_reserves_before_broker_and_obeys_inclusive_next_time(_case, outcome, delay, phase, offset, called):
    from tests.test_conversation_concurrency import make_store
    from tests.fakes import ScriptedBroker
    domain, store = batch_api(), make_store()
    broker = ScriptedBroker()
    broker.next_result = domain.EnqueueResult(outcome)
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        started = store.clock.now()
        def at_broker(_command):
            reserved = store.dispatch(command, lease)
            assert reserved.enqueue_attempt_id is not None
            assert reserved.next_enqueue_at == started + timedelta(seconds=60)
        broker.on_enqueue = at_broker
        if outcome == "CONFIRMED":
            assert store.ensure_consumer(broker, command, started, lease) is domain.EnsureConsumerResult.SCHEDULED
        else:
            with pytest.raises(domain.BrokerUnavailable):
                store.ensure_consumer(broker, command, started, lease)
        reserved = store.dispatch(command, lease)
        assert reserved.phase.value == phase
        assert reserved.next_enqueue_at == started + timedelta(seconds=delay)
        assert reserved.scheduled_at == (started if outcome == "CONFIRMED" else None)
    store.clock.set(started + timedelta(seconds=delay, microseconds=offset))
    broker.on_enqueue = None
    broker.next_result = domain.EnqueueResult.CONFIRMED
    with store.contact_lease(PHONE) as lease:
        result = store.ensure_consumer(broker, command, store.clock.now(), lease)
        assert len(broker.calls) == (2 if called else 1)
        assert result is (domain.EnsureConsumerResult.SCHEDULED if called else domain.EnsureConsumerResult.NOT_DUE)
        assert (store.dispatch(command, lease).enqueue_attempt_id != reserved.enqueue_attempt_id) is called


@pytest.mark.parametrize("delta,terminal", [(-1, False), (0, True), (1, True)])
@pytest.mark.parametrize("_case", [None], ids=["state-42"])
def test_batch_dispatch_deadline_exhausts_without_content_or_recreation(_case, delta, terminal, conversation_resources):
    from tests.test_conversation_concurrency import make_store
    domain, store = batch_api(), make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        deadline = store.dispatch(command, lease).dispatch_deadline
    store.clock.set(deadline + timedelta(microseconds=delta))
    with store.contact_lease(PHONE) as lease:
        claim = store.claim_or_resume_batch(command, store.clock.now(), lease)
        assert claim.outcome.value == ("TERMINAL" if terminal else "CLAIMED")
        assert store.dispatch(command, lease).phase.value == ("EXHAUSTED" if terminal else "STAGED")
        if terminal:
            assert claim.envelopes == ()
            assert batch_details(store, lease, "staging") == []
            assert batch_details(store, lease, "buffer") == []
            assert batch_details(store, lease, "dedupe")[0].body["disposition"] == "FAILED"
            assert append_batch(store, lease).disposition is domain.IngressDisposition.DUPLICATE
            assert store.claim_or_resume_batch(command, store.clock.now(), lease).outcome.value == "TERMINAL"
            assert store.recoverable_batches().commands == ()
    if not terminal:
        conversation_resources.expect_crashed_claim(store, command, claim.attempt, reason="worker stops after the last valid dispatch claim")


@pytest.mark.parametrize("_case", [None], ids=["state-46"])
def test_staged_ignores_old_dispatch_deadline_and_never_drains_new_buffer(_case, conversation_resources):
    from tests.test_conversation_concurrency import make_store
    domain, store = batch_api(), make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        deadline = store.dispatch(command, lease).dispatch_deadline
    store.clock.set(deadline - timedelta(seconds=1))
    with store.contact_lease(PHONE) as lease:
        first = store.claim_or_resume_batch(command, store.clock.now(), lease)
        new = append_batch(store, lease, message_id="new-id", content="new input")
        assert new.batch_id != command.batch_id
    store.clock.set(first.attempt.claim_deadline)
    with store.contact_lease(PHONE) as lease:
        resumed = store.claim_or_resume_batch(command, store.clock.now(), lease)
        assert resumed.outcome.value == "CLAIMED"
        assert resumed.attempt.processing_id == first.attempt.processing_id
        assert resumed.attempt.operation_id == first.attempt.operation_id
        assert resumed.attempt.processing_deadline == first.attempt.processing_deadline
        assert resumed.attempt.claim_token != first.attempt.claim_token
        assert [e.content for e in resumed.envelopes] == ["synthetic text"]
        assert batch_details(store, lease, "buffer")[0].body["envelopes"][0]["content"] == "new input"
        assert store.finalize_ingress_once(PHONE, None, "synthetic-id", command.generation, lease).disposition is domain.IngressDisposition.DUPLICATE
    conversation_resources.expect_crashed_claim(store, command, resumed.attempt, reason="replacement worker stops before publishing RESULT_READY")


@pytest.mark.parametrize("_case", [None], ids=["state-34"])
def test_result_ready_complete_batch_lifecycle_reuses_result_and_purges_content(_case, transition_env):
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, clock.now(), lease)
        result = domain.AgentResult("synthetic response", [{"role": "user", "content": "synthetic text"}], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(command, claim.attempt, result, clock.now(), lease)
    with store.contact_lease(PHONE) as lease:
        resumed = store.claim_or_resume_batch(command, clock.now(), lease)
        assert resumed.outcome.value == "RESULT_READY"
        assert resumed.result == result
        outbound = coordinator.apply_agent_result(db, PHONE, resumed.result, resumed.attempt.processing_id,
                    resumed.attempt.operation_id, clock.now(), lease)
        assert outbound.text == "synthetic response"
        reservation = store.reserve_outbound_enqueue(command, resumed.attempt, clock.now(), lease)
        store.record_outbound_attempt(command, resumed.attempt, clock.now(), lease, reservation=reservation)
        store.complete_batch(command, resumed.attempt, clock.now(), lease, reservation=reservation)
        store.complete_batch(command, resumed.attempt, clock.now(), lease, reservation=reservation)
        assert store.dispatch(command, lease).phase is domain.DispatchPhase.PROCESSED
        assert batch_details(store, lease, "processing")[0].body["phase"] == "DONE"
        assert batch_details(store, lease, "dedupe")[0].body["disposition"] == "PROCESSED"
        assert batch_details(store, lease, "staging") == []
        assert "synthetic response" not in str(store.contact_snapshot(PHONE))
        assert "synthetic text" not in str(store.contact_snapshot(PHONE))
        assert store.recoverable_batches().commands == ()


@pytest.mark.parametrize("offset,accepted", [(-1, True), (0, False), (1, False)])
@pytest.mark.parametrize("_case", [None], ids=["state-47"])
def test_result_ready_processing_deadline_is_inclusive(_case, offset, accepted):
    from tests.test_conversation_concurrency import make_store
    domain, store = batch_api(), make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, store.clock.now(), lease)
        # Advance the clock at the atomic boundary; the Python check happened earlier.
        target = claim.attempt.processing_deadline + timedelta(microseconds=offset)
        store.client.before_operation["stage_agent_result"] = lambda: store.clock.set(target)
        # Keep the lease valid over the synthetic long call; only the processing deadline moves.
        from app.conversation_redis import contact_keys
        store.client.expiry[contact_keys(PHONE).lease] = (target + timedelta(seconds=60)).timestamp()
        result = domain.AgentResult("late result", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        if accepted:
            store.stage_agent_result(command, claim.attempt, result, store.clock.now(), lease)
        else:
            with pytest.raises(domain.ConversationMutationPending):
                store.stage_agent_result(command, claim.attempt, result, store.clock.now(), lease)
            assert store.dispatch(command, lease).phase is domain.DispatchPhase.EXHAUSTED
            assert "late result" not in str(store.contact_snapshot(PHONE))


def test_batch_command_rejects_invalid_or_extra_payload_without_leaking_input():
    domain = batch_api()
    with pytest.raises(domain.ConversationStateUnavailable) as caught:
        domain.ProcessingCommand.from_payload({"sensitive": "synthetic secret"})
    assert caught.value.reason_code.value == "invalid_task_command"
    assert "synthetic secret" not in str(caught.value)
    command = domain.ProcessingCommand(PHONE, "00000000-0000-4000-8000-000000000003",
        "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002")
    assert domain.ProcessingCommand.from_payload(json.loads(json.dumps(command.to_payload()))) == command


@pytest.mark.parametrize("_case", [None], ids=["state-48"])
def test_enqueue_initial_time_and_dispatch_deadline_use_received_at(_case, ):
    from tests.test_conversation_concurrency import make_store
    domain, store = batch_api(), make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        received = store.clock.now()
        envelope = domain.InboundEnvelope("text", "synthetic", received, str(anchor.last_generation), "delayed-ingress")
        store.clock.advance(timedelta(seconds=2))
        receipt = store.finalize_ingress_once(PHONE, envelope, envelope.message_id, envelope.generation, lease)
        dispatch = store.dispatch(batch_command(store, lease, receipt), lease)
        assert dispatch.next_enqueue_at == received
        assert dispatch.dispatch_deadline == received + timedelta(seconds=900)
