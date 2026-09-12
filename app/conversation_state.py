"""Pure contracts for fail-closed conversation coordination."""

from __future__ import annotations

import math
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol, Sequence
from uuid import UUID


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


@dataclass(frozen=True)
class ContactLease:
    phone: str
    owner_token: str
    lease_deadline: datetime


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
        if lease_ttl is not None and heartbeat is not None and heartbeat > lease_ttl / 3:
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
    hours = float(value)
    if not math.isfinite(hours) or hours <= 0:
        raise InvalidManualPauseDuration("invalid_value")
    if hours > MAX_MANUAL_PAUSE.total_seconds() / 3600:
        raise InvalidManualPauseDuration("above_maximum")
    return hours


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
