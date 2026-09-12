"""Pure contracts for fail-closed conversation coordination."""

from __future__ import annotations

import math
import hashlib
import json
from copy import deepcopy
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence
from threading import Lock
from functools import wraps
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Appointment, ConversationContext, PausedContact


class ConversationState(str, Enum):
    BOT_ACTIVE = "BOT_ACTIVE"
    SECRETARY_ATTENDANCE = "SECRETARY_ATTENDANCE"


class ConversationCycle(str, Enum):
    OPEN = "OPEN"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"
    MUTATING = "MUTATING"
    QUARANTINED = "QUARANTINED"


class OutboundKind(str, Enum):
    NORMAL = "NORMAL"
    TRANSFER_CONFIRMATION = "TRANSFER_CONFIRMATION"
    CLOSURE_CONFIRMATION = "CLOSURE_CONFIRMATION"


class IngressDisposition(str, Enum):
    BUFFERED = "BUFFERED"
    PROCESSED = "PROCESSED"
    DROPPED = "DROPPED"
    APPLIED = "APPLIED"
    IGNORED = "IGNORED"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"


class MutationPhase(str, Enum):
    PREPARED = "PREPARED"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"
    QUARANTINED = "QUARANTINED"


class ProcessingPhase(str, Enum):
    CLAIMED = "CLAIMED"
    RESULT_READY = "RESULT_READY"
    APPLYING = "APPLYING"
    DONE = "DONE"
    QUARANTINED = "QUARANTINED"


class DispatchPhase(str, Enum):
    PENDING = "PENDING"
    SCHEDULED = "SCHEDULED"
    STAGED = "STAGED"
    PROCESSED = "PROCESSED"
    EXHAUSTED = "EXHAUSTED"


class AgentIntent(str, Enum):
    SAVE_CONTEXT = "SAVE_CONTEXT"
    PAUSE_FOR_SECRETARY = "PAUSE_FOR_SECRETARY"
    CLOSE_CONTEXT = "CLOSE_CONTEXT"


class DependencyName(str, Enum):
    SECRET = "secret"
    SQL = "sql"
    REDIS = "redis"
    EPOCH = "epoch"
    BROKER = "broker"


class FailureReason(str, Enum):
    STATE_UNAVAILABLE = "conversation_state_unavailable"
    CONTACT_LOCK_UNAVAILABLE = "contact_lock_unavailable"
    CONTACT_LEASE_LOST = "contact_lease_lost"
    GENERATION_UNAVAILABLE = "conversation_generation_unavailable"
    MUTATION_PENDING = "conversation_mutation_pending"
    MUTATION_AMBIGUOUS = "conversation_mutation_ambiguous"
    INVALID_CANONICAL_CONTACT = "invalid_canonical_contact"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    OVERFLOW = "overflow"
    ABOVE_MAXIMUM = "above_maximum"
    CONFIGURATION_INVALID = "configuration_invalid"
    READINESS_UNAVAILABLE = "readiness_unavailable"
    COMMIT_RESULT_UNKNOWN = "commit_result_unknown"
    REDIS_FINALIZE_AFTER_COMMIT_FAILED = "redis_finalize_after_commit_failed"
    CONDITION_CHANGED = "mutation_condition_changed"


class ConfigurationIssue(str, Enum):
    INVALID_EPOCH = "invalid_epoch"
    MISSING_REDIS_RUN_ID = "missing_redis_run_id"
    NOEVICTION_NOT_ATTESTED = "noeviction_not_attested"
    PERSISTENCE_NOT_ATTESTED = "persistence_not_attested"
    INVALID_CONTACT_LEASE_TTL = "invalid_contact_lease_ttl"
    INVALID_CONTACT_LEASE_HEARTBEAT = "invalid_contact_lease_heartbeat"
    INVALID_CLAIM_TTL = "invalid_claim_ttl"
    INVALID_DISPATCH_RETRY = "invalid_dispatch_retry"
    INVALID_PROCESSING_RETRY = "invalid_processing_retry"
    INVALID_ENQUEUE_VISIBILITY = "invalid_enqueue_visibility"
    INVALID_ENQUEUE_BACKOFF = "invalid_enqueue_backoff"
    INVALID_REPLAY_WINDOW = "invalid_replay_window"
    INVALID_TTL_MARGIN = "invalid_ttl_margin"
    INVALID_BATCH_RECOVERY_INTERVAL = "invalid_batch_recovery_interval"
    HEARTBEAT_EXCEEDS_LEASE_LIMIT = "heartbeat_exceeds_lease_limit"
    CLAIM_NOT_BELOW_PROCESSING_HORIZON = "claim_not_below_processing_horizon"
    VISIBILITY_NOT_BELOW_DISPATCH_HORIZON = "visibility_not_below_dispatch_horizon"
    BACKOFF_NOT_BELOW_DISPATCH_HORIZON = "backoff_not_below_dispatch_horizon"


class ConversationDomainError(Exception):
    def __init__(self, reason_code: FailureReason | str):
        try:
            reason = reason_code if isinstance(reason_code, FailureReason) else FailureReason(reason_code)
        except (TypeError, ValueError) as exc:
            raise ValueError("reason_code must be an enumerated FailureReason") from exc
        self.reason_code = reason
        super().__init__(reason)


class ConversationStateUnavailable(ConversationDomainError):
    pass


class ContactLockUnavailable(ConversationDomainError):
    pass


class ContactLeaseLost(ConversationDomainError):
    pass


class ConversationGenerationUnavailable(ConversationDomainError):
    pass


class ConversationMutationPending(ConversationDomainError):
    pass


class ConversationMutationAmbiguous(ConversationDomainError):
    pass


class InvalidCanonicalContact(ConversationDomainError):
    pass


class InvalidManualPauseDuration(ConversationDomainError, ValueError):
    pass


class ConfigurationInvalid(ConversationDomainError):
    pass


class ReadinessUnavailable(ConversationDomainError):
    pass


@dataclass(frozen=True)
class DependencyStatus:
    name: DependencyName
    ready: bool


@dataclass(frozen=True)
class ReadinessReport:
    dependencies: Sequence[DependencyStatus]

    @property
    def ready(self) -> bool:
        return all(item.ready for item in self.dependencies)

    @property
    def components(self) -> dict[str, str]:
        return {item.name.value: "ready" if item.ready else "unavailable"
                for item in self.dependencies}


@dataclass(frozen=True)
class ContactLease:
    phone: str
    owner_token: str
    lease_deadline: datetime
    ownership_guard: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def assert_owned(self) -> None:
        if self.ownership_guard is None:
            raise ContactLeaseLost(FailureReason.CONTACT_LEASE_LOST)
        self.ownership_guard()


@dataclass(frozen=True)
class ManifestEntry:
    kind: str
    id: str
    version: int
    expected_until: datetime
    index_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContactDetail:
    entry: ManifestEntry
    body: Mapping[str, Any]
    terminal: bool = False


@dataclass(frozen=True)
class IngressResolution:
    state: ConversationState
    cycle: ConversationCycle
    generation: str
    paused_until: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True)
class MutationTarget:
    kind: str
    request_fingerprint: str
    expected_hash: str
    cycle: ConversationCycle
    rotate_generation: bool = True
    paused_until: datetime | None = None
    reason: str | None = None
    context_hash: str | None = None

    @property
    def fingerprint(self) -> str:
        return _hash({"kind": self.kind, "expected_hash": self.expected_hash,
                      "paused_until": self.paused_until.isoformat() if self.paused_until else None,
                      "reason": self.reason, "context_hash": self.context_hash})


@dataclass(frozen=True)
class MutationAttempt:
    epoch: UUID
    operation_id: str
    kind: str
    phase: MutationPhase
    target_fingerprint: str
    request_fingerprint: str
    generation: UUID
    prior_cycle: ConversationCycle
    target_cycle: ConversationCycle
    processing_deadline: datetime
    expected_until: datetime
    owner_token_hash: str
    paused_until: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True)
class DefinitiveRollbackProof:
    """Local rollback receipt; only valid before SQL commit has been invoked."""
    operation_id: str
    owner_token_hash: str
    receipt_token: UUID


_rollback_receipts: dict[UUID, tuple[str, str]] = {}
_rollback_receipt_lock = Lock()


def _issue_rollback_proof(operation_id: str, lease: ContactLease) -> DefinitiveRollbackProof:
    proof = DefinitiveRollbackProof(operation_id, hashlib.sha256(lease.owner_token.encode()).hexdigest(), uuid4())
    with _rollback_receipt_lock:
        _rollback_receipts[proof.receipt_token] = (proof.operation_id, proof.owner_token_hash)
    return proof


def _consume_rollback_proof(proof: DefinitiveRollbackProof, operation_id: str, lease: ContactLease) -> bool:
    if not isinstance(proof, DefinitiveRollbackProof):
        return False
    expected = (operation_id, hashlib.sha256(lease.owner_token.encode()).hexdigest())
    with _rollback_receipt_lock:
        if (proof.operation_id, proof.owner_token_hash) != expected or _rollback_receipts.get(proof.receipt_token) != expected:
            return False
        del _rollback_receipts[proof.receipt_token]
    return True


@dataclass(frozen=True)
class ContactAnchor:
    contact_revision: int
    last_generation: UUID
    manifest: tuple[ManifestEntry, ...]
    manifest_fingerprint: str
    generation_history: tuple[UUID, ...]
    cycle: ConversationCycle = ConversationCycle.OPEN
    mutation_fence: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if (type(self.contact_revision) is not int or self.contact_revision < 0
                or not isinstance(self.last_generation, UUID)
                or not isinstance(self.generation_history, tuple)
                or not all(isinstance(value, UUID) for value in self.generation_history)
                or self.last_generation not in self.generation_history
                or len(set(self.generation_history)) != len(self.generation_history)):
            raise ConversationGenerationUnavailable(FailureReason.GENERATION_UNAVAILABLE)


@dataclass(frozen=True)
class PauseTransitionRef:
    generation: str
    paused_until: datetime
    reason: str


@dataclass(frozen=True)
class ClosureTransitionRef:
    generation: str
    operation_id: str


@dataclass(frozen=True)
class ConversationSnapshot:
    phone: str
    messages: list[dict]
    current_flow: str | None
    flow_data: dict
    status: str
    last_activity: datetime | None


@dataclass(frozen=True)
class AgentResult:
    text: str
    messages: list[dict]
    current_flow: str | None
    flow_data: dict
    intent: AgentIntent


@dataclass(frozen=True)
class OutboundEnvelope:
    phone: str
    text: str
    kind: OutboundKind
    generation: str
    processing_id: str | None
    operation_id: str | None
    pause_ref: PauseTransitionRef | None = None
    closure_ref: ClosureTransitionRef | None = None

    def to_dict(self) -> dict[str, Any]:
        pause_ref = None
        if self.pause_ref is not None:
            pause_ref = {
                "generation": self.pause_ref.generation,
                "paused_until": self.pause_ref.paused_until.isoformat(),
                "reason": self.pause_ref.reason,
            }
        closure_ref = None
        if self.closure_ref is not None:
            closure_ref = {
                "generation": self.closure_ref.generation,
                "operation_id": self.closure_ref.operation_id,
            }
        return {
            "phone": self.phone,
            "text": self.text,
            "kind": self.kind.value,
            "generation": self.generation,
            "processing_id": self.processing_id,
            "operation_id": self.operation_id,
            "pause_ref": pause_ref,
            "closure_ref": closure_ref,
        }


class ConversationStore(Protocol):
    def readiness(self) -> ReadinessReport: ...

    def contact_lease(self, phone: str) -> AbstractContextManager[ContactLease]: ...

    def initialize_contact(self, phone: str, lease: ContactLease | None = None,
                           *, db_state_present: bool | None = None) -> ContactAnchor: ...

    def assert_owned(self, lease: ContactLease) -> None: ...

    def renew_lease(self, lease: ContactLease) -> None: ...

    def release_lease(self, lease: ContactLease) -> None: ...

    def read_anchor(self, lease: ContactLease) -> ContactAnchor: ...

    def read_details(self, lease: ContactLease) -> tuple[ContactDetail, ...]: ...

    def compare_and_set(self, lease: ContactLease, expected: ContactAnchor,
                        details: tuple[ContactDetail, ...], *,
                        generation: UUID | None = None,
                        cycle: ConversationCycle | None = None) -> ContactAnchor: ...

    def cleanup(self, lease: ContactLease, expected: ContactAnchor) -> ContactAnchor: ...

    def inspect_mutation(self, phone: str, operation_id: str, lease: ContactLease,
                         *, operational: bool = False) -> MutationAttempt | None: ...

    def assert_mutation_available(self, lease: ContactLease, now: datetime) -> None: ...

    def prepare_mutation(self, phone: str, kind: str, target_fingerprint: str,
                         lease: ContactLease, operation_id: str, now: datetime,
                         *, target: MutationTarget) -> MutationAttempt: ...

    def enter_committing(self, phone: str, operation_id: str, lease: ContactLease,
                         now: datetime, processing_deadline: datetime) -> MutationAttempt: ...

    def preserve_or_abort_prepared(self, phone: str, operation_id: str,
                                  lease: ContactLease, now: datetime) -> None: ...

    def restore_prepared_after_rollback(self, phone: str, operation_id: str, lease: ContactLease,
                                        now: datetime, *, proof: DefinitiveRollbackProof) -> None: ...

    def quarantine_ambiguous_commit(self, phone: str, operation_id: str,
                                    lease: ContactLease, now: datetime) -> MutationAttempt: ...

    def finalize_committed(self, phone: str, operation_id: str, lease: ContactLease,
                           now: datetime) -> MutationAttempt: ...

    def resolve_quarantined_mutation(self, phone: str, operation_id: str, epoch: UUID,
                                     lease: ContactLease, now: datetime, *, quiescent: bool,
                                     outcome: MutationPhase) -> MutationAttempt: ...


class BrokerPort(Protocol):
    def probe(self) -> bool: ...


@dataclass(frozen=True)
class ConversationConfig:
    coordination_epoch: UUID | None
    redis_expected_run_id: str | None
    redis_attest_noeviction: bool
    redis_attest_persistence: bool
    contact_lease_ttl_seconds: int | None
    contact_lease_heartbeat_seconds: int | None
    claim_ttl_seconds: int | None
    dispatch_retry_seconds: int | None
    processing_retry_seconds: int | None
    enqueue_visibility_seconds: int | None
    enqueue_backoff_seconds: int | None
    replay_window_seconds: int | None
    ttl_margin_seconds: int | None
    batch_recovery_interval_seconds: int | None
    issues: tuple[ConfigurationIssue, ...]

    @classmethod
    def from_settings(cls, settings: Any) -> ConversationConfig:
        issues: list[ConfigurationIssue] = []
        try:
            epoch = UUID(settings.coordination_epoch) if settings.coordination_epoch else None
        except (AttributeError, TypeError, ValueError):
            epoch = None
        if epoch is None:
            issues.append(ConfigurationIssue.INVALID_EPOCH)

        run_id = settings.redis_expected_run_id or None
        if run_id is None:
            issues.append(ConfigurationIssue.MISSING_REDIS_RUN_ID)
        if not settings.redis_attest_noeviction:
            issues.append(ConfigurationIssue.NOEVICTION_NOT_ATTESTED)
        if not settings.redis_attest_persistence:
            issues.append(ConfigurationIssue.PERSISTENCE_NOT_ATTESTED)

        duration_fields = (
            ("contact_lease_ttl_seconds", ConfigurationIssue.INVALID_CONTACT_LEASE_TTL),
            ("contact_lease_heartbeat_seconds", ConfigurationIssue.INVALID_CONTACT_LEASE_HEARTBEAT),
            ("claim_ttl_seconds", ConfigurationIssue.INVALID_CLAIM_TTL),
            ("dispatch_retry_seconds", ConfigurationIssue.INVALID_DISPATCH_RETRY),
            ("processing_retry_seconds", ConfigurationIssue.INVALID_PROCESSING_RETRY),
            ("enqueue_visibility_seconds", ConfigurationIssue.INVALID_ENQUEUE_VISIBILITY),
            ("enqueue_backoff_seconds", ConfigurationIssue.INVALID_ENQUEUE_BACKOFF),
            ("replay_window_seconds", ConfigurationIssue.INVALID_REPLAY_WINDOW),
            ("ttl_margin_seconds", ConfigurationIssue.INVALID_TTL_MARGIN),
            ("batch_recovery_interval_seconds", ConfigurationIssue.INVALID_BATCH_RECOVERY_INTERVAL),
        )
        durations: dict[str, int | None] = {}
        for field_name, issue in duration_fields:
            value = getattr(settings, field_name, None)
            durations[field_name] = value if isinstance(value, int) and value > 0 else None
            if durations[field_name] is None:
                issues.append(issue)

        lease_ttl = durations["contact_lease_ttl_seconds"]
        heartbeat = durations["contact_lease_heartbeat_seconds"]
        if lease_ttl is not None and heartbeat is not None and heartbeat * 3 > lease_ttl:
            issues.append(ConfigurationIssue.HEARTBEAT_EXCEEDS_LEASE_LIMIT)

        claim_ttl = durations["claim_ttl_seconds"]
        processing_horizon = durations["processing_retry_seconds"]
        if claim_ttl is not None and processing_horizon is not None and claim_ttl >= processing_horizon:
            issues.append(ConfigurationIssue.CLAIM_NOT_BELOW_PROCESSING_HORIZON)

        dispatch_horizon = durations["dispatch_retry_seconds"]
        visibility = durations["enqueue_visibility_seconds"]
        if dispatch_horizon is not None and visibility is not None and visibility >= dispatch_horizon:
            issues.append(ConfigurationIssue.VISIBILITY_NOT_BELOW_DISPATCH_HORIZON)
        backoff = durations["enqueue_backoff_seconds"]
        if dispatch_horizon is not None and backoff is not None and backoff >= dispatch_horizon:
            issues.append(ConfigurationIssue.BACKOFF_NOT_BELOW_DISPATCH_HORIZON)

        return cls(
            coordination_epoch=epoch,
            redis_expected_run_id=run_id,
            redis_attest_noeviction=bool(settings.redis_attest_noeviction),
            redis_attest_persistence=bool(settings.redis_attest_persistence),
            issues=tuple(issues),
            **durations,
        )


DEFAULT_SECRETARY_PAUSE = timedelta(hours=24)
MAX_MANUAL_PAUSE = timedelta(days=365)


def validate_manual_pause_hours(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidManualPauseDuration("invalid_type")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidManualPauseDuration("invalid_value")
    if value <= 0:
        raise InvalidManualPauseDuration("invalid_value")
    if value > MAX_MANUAL_PAUSE.total_seconds() / 3600:
        raise InvalidManualPauseDuration("above_maximum")
    return float(value)


def manual_pause_deadline(now: datetime, hours: object) -> datetime:
    valid_hours = validate_manual_pause_hours(hours)
    try:
        deadline = now + timedelta(hours=valid_hours)
        maximum = now + MAX_MANUAL_PAUSE
    except OverflowError as exc:
        raise InvalidManualPauseDuration("overflow") from exc
    if deadline > maximum:
        raise InvalidManualPauseDuration("above_maximum")
    return deadline


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    # Existing SQL columns are timezone-naive UTC; public references are aware UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _sql_time(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def _reason_codes_only(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ConversationDomainError:
            raise
        except Exception:
            raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE) from None
    return guarded


class ConversationCoordinator:
    """Central SQL boundary. Every public operation consumes an existing lease."""

    TEST_PHONE = "5500000000000"

    def __init__(self, store: ConversationStore, clock):
        self.store, self.clock = store, clock

    def _ensure(self, db: Session, phone: str, lease: ContactLease) -> ContactAnchor:
        if lease.phone != phone:
            raise ContactLeaseLost(FailureReason.CONTACT_LEASE_LOST)
        lease.assert_owned()
        if db.new or db.dirty or db.deleted:
            raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
        with db.no_autoflush:
            present = (db.get(PausedContact, phone) is not None
                       or db.get(ConversationContext, phone) is not None)
        return self.store.initialize_contact(phone, lease, db_state_present=present)

    def _db_hash(self, db: Session, phone: str, *, appointments: bool = False) -> str:
        with db.no_autoflush:
            pause = db.get(PausedContact, phone, populate_existing=True)
            context = db.get(ConversationContext, phone, populate_existing=True)
            state = {
                "pause": None if pause is None else {
                    "until": _utc(pause.paused_until).isoformat(), "reason": pause.reason,
                    "at": _utc(pause.paused_at).isoformat()},
                "context": None if context is None else {
                    "messages": context.messages, "flow": context.current_flow,
                    "data": context.flow_data, "status": context.status,
                    "activity": _utc(context.last_activity).isoformat()},
            }
            if appointments:
                state["appointments"] = list(db.scalars(select(Appointment.id).where(
                    Appointment.patient_phone == phone).order_by(Appointment.id)))
        return _hash(state)

    def _request(self, db, phone, lease, operation_id, now, kind, parameters):
        self._ensure(db, phone, lease)
        self.store.assert_mutation_available(lease, now)
        request_hash = _hash({"kind": kind, "parameters": parameters})
        previous = self.store.inspect_mutation(phone, operation_id, lease)
        if previous is not None:
            if previous.request_fingerprint != request_hash:
                raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
            if previous.phase is not MutationPhase.COMMITTED:
                raise ConversationMutationPending(FailureReason.MUTATION_PENDING)
        return request_hash, previous

    def _run_mutation(self, db: Session, phone: str, kind: str, target: MutationTarget,
                      lease: ContactLease, operation_id: str, now: datetime,
                      apply_dml: Callable[[Session, MutationAttempt], Any]) -> MutationAttempt:
        attempt = self.store.prepare_mutation(phone, kind, target.fingerprint, lease,
                                               operation_id, now, target=target)
        if attempt.phase is MutationPhase.COMMITTED:
            return attempt
        try:
            apply_dml(db, attempt)
            db.flush()
            lease.assert_owned()
            self.store.enter_committing(phone, operation_id, lease, self.clock.now(),
                                        attempt.processing_deadline)
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                # No definitive rollback proof: preserve PREPARED and block contenders.
                raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE) from None
            proof = _issue_rollback_proof(operation_id, lease)
            try:
                self.store.restore_prepared_after_rollback(phone, operation_id, lease, self.clock.now(), proof=proof)
                self.store.preserve_or_abort_prepared(phone, operation_id, lease, self.clock.now())
            except ConversationDomainError:
                pass  # Owner loss must never remove another owner's fence.
            finally:
                with _rollback_receipt_lock:
                    _rollback_receipts.pop(proof.receipt_token, None)
            if isinstance(exc, ConversationDomainError):
                raise
            raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE) from None
        try:
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            try:
                self.store.quarantine_ambiguous_commit(phone, operation_id, lease, self.clock.now())
            except ConversationDomainError:
                pass  # COMMITTING remains durable until later quarantine processing.
            raise ConversationMutationAmbiguous(FailureReason.COMMIT_RESULT_UNKNOWN) from None
        try:
            return self.store.finalize_committed(phone, operation_id, lease, self.clock.now())
        except Exception:
            raise ConversationMutationAmbiguous(FailureReason.REDIS_FINALIZE_AFTER_COMMIT_FAILED) from None

    def _check_target(self, db, phone, target, *, appointments=False):
        if self._db_hash(db, phone, appointments=appointments) != target.expected_hash:
            raise ConversationStateUnavailable(FailureReason.CONDITION_CHANGED)

    @staticmethod
    def _pause_ref(attempt: MutationAttempt) -> PauseTransitionRef:
        if attempt.paused_until is None or attempt.reason is None:
            raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE)
        return PauseTransitionRef(str(attempt.generation), attempt.paused_until, attempt.reason)

    def _pause(self, db, phone, reason, now, lease, operation_id, *, kind, hours=None, agent_request=None):
        parameters = {"reason": reason, "hours": hours}
        if agent_request is not None:
            parameters["agent"] = agent_request
        request_hash, previous = self._request(db, phone, lease, operation_id, now, kind, parameters)
        if previous is not None:
            return self._pause_ref(previous)
        if agent_request is not None:
            self._assert_open(db, phone, lease)
        deadline = _utc(now) + DEFAULT_SECRETARY_PAUSE if hours is None else manual_pause_deadline(_utc(now), hours)
        if kind == "EXTEND_PAUSE":
            pause = db.get(PausedContact, phone)
            if pause is None:
                raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
            deadline = manual_pause_deadline(_utc(pause.paused_until), hours)
            if deadline > _utc(now) + MAX_MANUAL_PAUSE:
                raise InvalidManualPauseDuration(FailureReason.ABOVE_MAXIMUM)
            reason = pause.reason
        target = MutationTarget(kind, request_hash, self._db_hash(db, phone), ConversationCycle.PAUSED,
                                paused_until=deadline, reason=reason)
        def dml(session, _attempt):
            self._check_target(session, phone, target)
            if kind == "PAUSE_FOR_SECRETARY":
                session.execute(delete(ConversationContext).where(ConversationContext.phone == phone))
            row = session.get(PausedContact, phone)
            if row is None:
                row = PausedContact(phone=phone)
                session.add(row)
            row.paused_until, row.reason = _sql_time(deadline), reason
            if kind != "EXTEND_PAUSE":
                row.paused_at = _sql_time(now)
        return self._pause_ref(self._run_mutation(db, phone, kind, target, lease, operation_id, now, dml))

    @_reason_codes_only
    def pause_for_secretary(self, db: Session, phone: str, reason: str, now: datetime,
                            lease: ContactLease, operation_id: str) -> PauseTransitionRef:
        return self._pause(db, phone, reason, now, lease, operation_id, kind="PAUSE_FOR_SECRETARY")

    @_reason_codes_only
    def pause_manual(self, db: Session, phone: str, hours: object, reason: str, now: datetime,
                     lease: ContactLease, operation_id: str) -> PauseTransitionRef:
        valid_hours = validate_manual_pause_hours(hours)
        return self._pause(db, phone, reason, now, lease, operation_id, kind="PAUSE_MANUAL", hours=valid_hours)

    @_reason_codes_only
    def extend_pause(self, db: Session, phone: str, hours: object, now: datetime,
                     lease: ContactLease, operation_id: str) -> PauseTransitionRef:
        valid_hours = validate_manual_pause_hours(hours)
        return self._pause(db, phone, None, now, lease, operation_id, kind="EXTEND_PAUSE", hours=valid_hours)

    def _remove(self, db, phone, now, lease, operation_id, kind, *, cutoff=None, agent_request=None):
        parameters = {"cutoff": _utc(cutoff).isoformat() if cutoff else None}
        if agent_request is not None:
            parameters["agent"] = agent_request
        request_hash, previous = self._request(db, phone, lease, operation_id, now, kind,
                                               parameters)
        if previous is not None:
            return previous
        if agent_request is not None:
            self._assert_open(db, phone, lease)
        cycle = ConversationCycle.OPEN if kind in ("UNPAUSE", "EXPIRE_PAUSE", "RESET_TEST") else ConversationCycle.CLOSED
        if kind == "CLEAN_INACTIVE":
            row = db.get(ConversationContext, phone)
            if row is None or _utc(row.last_activity) >= _utc(cutoff):
                return None
            pause = db.get(PausedContact, phone)
            if pause is not None and _utc(pause.paused_until) > _utc(now):
                cycle = ConversationCycle.PAUSED
        target = MutationTarget(kind, request_hash, self._db_hash(db, phone, appointments=kind == "RESET_TEST"), cycle)
        def dml(session, _attempt):
            self._check_target(session, phone, target, appointments=kind == "RESET_TEST")
            if kind in ("CLOSE_CONTEXT", "CLEAN_INACTIVE", "RESET_TEST"):
                statement = delete(ConversationContext).where(ConversationContext.phone == phone)
                if cutoff is not None:
                    statement = statement.where(ConversationContext.last_activity < _sql_time(cutoff))
                result = session.execute(statement)
                if cutoff is not None and result.rowcount != 1:
                    raise ConversationStateUnavailable(FailureReason.CONDITION_CHANGED)
            if kind in ("UNPAUSE", "EXPIRE_PAUSE", "RESET_TEST"):
                statement = delete(PausedContact).where(PausedContact.phone == phone)
                if kind == "EXPIRE_PAUSE":
                    statement = statement.where(PausedContact.paused_until <= _sql_time(now))
                result = session.execute(statement)
                if kind == "EXPIRE_PAUSE" and result.rowcount != 1:
                    raise ConversationStateUnavailable(FailureReason.CONDITION_CHANGED)
            if kind == "RESET_TEST":
                session.execute(delete(Appointment).where(Appointment.patient_phone == phone))
        return self._run_mutation(db, phone, kind, target, lease, operation_id, now, dml)

    @_reason_codes_only
    def unpause(self, db: Session, phone: str, now: datetime, lease: ContactLease, operation_id: str) -> None:
        self._remove(db, phone, now, lease, operation_id, "UNPAUSE")

    @_reason_codes_only
    def close_context(self, db: Session, phone: str, now: datetime, lease: ContactLease,
                      operation_id: str) -> ClosureTransitionRef:
        attempt = self._remove(db, phone, now, lease, operation_id, "CLOSE_CONTEXT")
        return ClosureTransitionRef(str(attempt.generation), attempt.operation_id)

    @_reason_codes_only
    def close_inactive_context(self, db: Session, phone: str, cutoff: datetime, now: datetime,
                               lease: ContactLease, operation_id: str) -> bool:
        try:
            return self._remove(db, phone, now, lease, operation_id, "CLEAN_INACTIVE", cutoff=cutoff) is not None
        except ConversationStateUnavailable as exc:
            if exc.reason_code is FailureReason.CONDITION_CHANGED:
                return False
            raise

    @_reason_codes_only
    def reset_test_state(self, db: Session, phone: str, now: datetime,
                         lease: ContactLease, operation_id: str) -> None:
        if phone != self.TEST_PHONE:
            raise InvalidCanonicalContact(FailureReason.INVALID_CANONICAL_CONTACT)
        self._remove(db, phone, now, lease, operation_id, "RESET_TEST")

    @_reason_codes_only
    def resolve_ingress(self, db: Session, phone: str, now: datetime,
                         lease: ContactLease) -> IngressResolution:
        anchor = self._ensure(db, phone, lease)
        self.store.assert_mutation_available(lease, now)
        pause = db.get(PausedContact, phone)
        if pause is not None:
            if _utc(now) < _utc(pause.paused_until):
                if anchor.cycle is not ConversationCycle.PAUSED:
                    raise ConversationGenerationUnavailable(FailureReason.GENERATION_UNAVAILABLE)
                return IngressResolution(ConversationState.SECRETARY_ATTENDANCE, anchor.cycle,
                                         str(anchor.last_generation), _utc(pause.paused_until), pause.reason)
            operation = "expire-" + _hash({"generation": str(anchor.last_generation),
                                          "deadline": _utc(pause.paused_until).isoformat()})
            self._remove(db, phone, now, lease, operation, "EXPIRE_PAUSE")
            anchor = self.store.read_anchor(lease)
        elif anchor.cycle is ConversationCycle.PAUSED:
            raise ConversationGenerationUnavailable(FailureReason.GENERATION_UNAVAILABLE)
        if anchor.cycle is ConversationCycle.CLOSED:
            anchor = self.store.compare_and_set(lease, anchor, self.store.read_details(lease),
                                                generation=uuid4(), cycle=ConversationCycle.OPEN)
        return IngressResolution(ConversationState.BOT_ACTIVE, anchor.cycle, str(anchor.last_generation))

    @_reason_codes_only
    def apply_agent_result(self, db: Session, phone: str, result: AgentResult, processing_id: str,
                           operation_id: str, now: datetime, lease: ContactLease) -> OutboundEnvelope:
        agent_request = {"text": result.text, "messages": result.messages, "flow": result.current_flow,
                         "data": result.flow_data, "processing": processing_id}
        if result.intent is AgentIntent.PAUSE_FOR_SECRETARY:
            ref = self._pause(db, phone, "user_requested_human_assistance", now, lease, operation_id,
                              kind="PAUSE_FOR_SECRETARY", agent_request=agent_request)
            return OutboundEnvelope(phone, result.text, OutboundKind.TRANSFER_CONFIRMATION,
                                    ref.generation, processing_id, operation_id, pause_ref=ref)
        if result.intent is AgentIntent.CLOSE_CONTEXT:
            attempt = self._remove(db, phone, now, lease, operation_id, "CLOSE_CONTEXT", agent_request=agent_request)
            ref = ClosureTransitionRef(str(attempt.generation), attempt.operation_id)
            return OutboundEnvelope(phone, result.text, OutboundKind.CLOSURE_CONFIRMATION,
                                    ref.generation, processing_id, operation_id, closure_ref=ref)
        if result.intent is not AgentIntent.SAVE_CONTEXT:
            raise ConversationStateUnavailable(FailureReason.INVALID_VALUE)
        payload = {"messages": result.messages, "flow": result.current_flow, "data": result.flow_data}
        request_hash, previous = self._request(db, phone, lease, operation_id, now, "SAVE_CONTEXT",
                                               {"context": payload, "processing": processing_id, "text": result.text})
        if previous is None:
            anchor = self.store.read_anchor(lease)
            pause = db.get(PausedContact, phone)
            if anchor.cycle is not ConversationCycle.OPEN or pause is not None:
                raise ConversationMutationPending(FailureReason.MUTATION_PENDING)
            target = MutationTarget("SAVE_CONTEXT", request_hash, self._db_hash(db, phone), ConversationCycle.OPEN,
                                    rotate_generation=False, context_hash=_hash(payload))
            def dml(session, _attempt):
                self._check_target(session, phone, target)
                row = session.get(ConversationContext, phone)
                if row is None:
                    row = ConversationContext(phone=phone, created_at=_sql_time(now))
                    session.add(row)
                row.messages, row.current_flow, row.flow_data = deepcopy(result.messages), result.current_flow, deepcopy(result.flow_data)
                row.status, row.last_activity = "active", _sql_time(now)
            previous = self._run_mutation(db, phone, "SAVE_CONTEXT", target, lease, operation_id, now, dml)
        return OutboundEnvelope(phone, result.text, OutboundKind.NORMAL, str(previous.generation), processing_id, operation_id)

    def _assert_open(self, db, phone, lease):
        if (self.store.read_anchor(lease).cycle is not ConversationCycle.OPEN
                or db.get(PausedContact, phone) is not None):
            raise ConversationMutationPending(FailureReason.MUTATION_PENDING)

    @_reason_codes_only
    def may_send(self, db: Session, outbound: OutboundEnvelope, now: datetime, lease: ContactLease) -> bool:
        anchor = self._ensure(db, outbound.phone, lease)
        self.store.assert_mutation_available(lease, now)
        if outbound.generation != str(anchor.last_generation):
            return False
        pause = db.get(PausedContact, outbound.phone, populate_existing=True)
        if outbound.kind is OutboundKind.NORMAL:
            return anchor.cycle is ConversationCycle.OPEN and pause is None
        if outbound.kind is OutboundKind.TRANSFER_CONFIRMATION:
            ref = outbound.pause_ref
            return (anchor.cycle is ConversationCycle.PAUSED and pause is not None and ref is not None
                    and ref.generation == outbound.generation and _utc(now) < _utc(pause.paused_until)
                    and ref.paused_until == _utc(pause.paused_until) and ref.reason == pause.reason)
        if outbound.kind is OutboundKind.CLOSURE_CONFIRMATION:
            ref = outbound.closure_ref
            if (anchor.cycle is not ConversationCycle.CLOSED or pause is not None or ref is None
                    or ref.generation != outbound.generation or ref.operation_id != outbound.operation_id):
                return False
            attempt = self.store.inspect_mutation(outbound.phone, ref.operation_id, lease)
            return (attempt is not None and attempt.phase is MutationPhase.COMMITTED
                    and attempt.generation == anchor.last_generation and attempt.target_cycle is ConversationCycle.CLOSED)
        return False
