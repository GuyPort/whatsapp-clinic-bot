import json
import math
from uuid import UUID
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


def seed_context(db, phone, now):
    from app.models import ConversationContext
    assert db.get_bind().url.database in (None, "", ":memory:")
    row = ConversationContext(phone=phone, messages=[{"role": "user", "content": "synthetic"}],
                              flow_data={"synthetic": True}, current_flow="duvidas",
                              status="active", last_activity=now.replace(tzinfo=None), created_at=now.replace(tzinfo=None))
    db.add(row)
    db.session.commit()
    return row


def test_state_ingress_without_pause_is_open_and_does_not_commit(transition_env):
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        resolved = coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        assert resolved.state is ConversationState.BOT_ACTIVE
        assert resolved.cycle is ConversationCycle.OPEN
        assert UUID(resolved.generation) == store.read_anchor(lease).last_generation
    assert "commit_entered" not in db.events


@pytest.mark.parametrize("delta,paused", [(-1, True), (0, False), (1, False)])
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


def test_pause_transfer_atomically_deletes_context_and_preserves_other_contact(transition_env):
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


def test_pause_beatriz_renewal_rotates_generation_but_retry_does_not(transition_env):
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


def test_pause_manual_and_extension_enforce_resulting_365_day_limit(transition_env):
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


def test_closed_new_ingress_rotates_generation_and_invalidates_closure_send(transition_env):
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


def test_send_exact_pause_reference_and_current_open_generation_only(transition_env):
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


@pytest.mark.parametrize("intent,kind,cycle", [
    (AgentIntent.SAVE_CONTEXT, OutboundKind.NORMAL, ConversationCycle.OPEN),
    (AgentIntent.PAUSE_FOR_SECRETARY, OutboundKind.TRANSFER_CONFIRMATION, ConversationCycle.PAUSED),
    (AgentIntent.CLOSE_CONTEXT, OutboundKind.CLOSURE_CONFIRMATION, ConversationCycle.CLOSED),
])
def test_mutation_agent_result_is_persisted_once_with_typed_send(transition_env, intent, kind, cycle):
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


def test_state_existing_sql_without_anchor_fails_closed(transition_env):
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
