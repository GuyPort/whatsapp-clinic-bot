import json
import math
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import pytest

from tests.fakes import ManualClock
from app.conversation_state import (
    AgentIntent,
    AgentResult,
    BrokerPort,
    ClosureTransitionRef,
    ConfigurationIssue,
    ConfigurationInvalid,
    ContactLease,
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


def test_manual_pause_accepts_exact_maximum_and_rejects_later_deadline():
    """Catches an off-by-one error at the 365-day safety boundary."""
    now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)

    assert manual_pause_deadline(now, 24 * 365) == datetime(
        2027, 9, 12, 12, tzinfo=timezone.utc
    )
    with pytest.raises(InvalidManualPauseDuration) as raised:
        manual_pause_deadline(now, (24 * 365) + 0.01)

    assert raised.value.reason_code is FailureReason.ABOVE_MAXIMUM


def test_manual_pause_translates_timedelta_overflow_to_domain_error():
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
