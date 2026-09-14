"""Recoverable synchronous task bodies; no Celery registration or live clients.

The broker and provider boundaries are not transactions with Redis/SQL. A lost
acknowledgement or process death may duplicate an external call. No exactly-once
delivery guarantee is made here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
from enum import Enum
import asyncio
import inspect
import logging
from typing import Callable, Protocol, TYPE_CHECKING
from uuid import uuid4

from celery.exceptions import Retry as CeleryRetry
from sqlalchemy.exc import SQLAlchemyError

from app.conversation_state import (
    AgentUnavailable, BrokerPort, BrokerUnavailable, ClaimOutcome,
    ContactLeaseLost, ContactLockUnavailable, ConversationCoordinator,
    ConversationDomainError, ConversationGenerationUnavailable,
    ConversationMutationAborted, ConversationMutationAmbiguous,
    ConversationMutationPending, ConversationStateUnavailable, ConversationStore,
    EnqueueResult, FailureReason, OutboundEnvelope, ProcessingCommand,
    ReadinessReport, ReadinessUnavailable,
    OutboundKind, TransportUnavailable, fixed_reply_result,
    IngressDisposition, SenderIdentity,
)
from app.utils import AuditEvent, ConversationAuditLogger, new_audit_correlation_id


logger = logging.getLogger(__name__)
conversation_audit = ConversationAuditLogger(logger)


def _emit_audit(event: AuditEvent, **fields: object) -> None:
    conversation_audit.emit(event, correlation_id=new_audit_correlation_id(), **fields)


def _retry_outcome(error: BaseException) -> str:
    """Map internal failures to fixed operator classes without rendering errors."""
    if isinstance(error, (ReadinessUnavailable, BrokerUnavailable, AgentUnavailable)):
        return "dependency_unavailable"
    if isinstance(error, ConversationStateUnavailable):
        return "persistence_failed"
    if isinstance(error, TransportUnavailable):
        return "transport_failed"
    return "coordination_failed"

if TYPE_CHECKING:
    from app.ai_agent import ClaudeToolAgent
    from app.whatsapp_service import WhatsAppService
    from sqlalchemy.orm import Session


class ProcessingOutcome(str, Enum):
    PROCESSED = "PROCESSED"
    TERMINAL = "TERMINAL"
    DUPLICATE = "DUPLICATE"


class SendOutcome(str, Enum):
    SENT = "SENT"
    DISCARDED = "DISCARDED"


class OutboundBrokerPort(Protocol):
    def enqueue_outbound(self, outbound: OutboundEnvelope) -> EnqueueResult: ...


class Clock(Protocol):
    def now(self): ...


@dataclass(repr=False)
class ConversationRuntime:
    coordinator: ConversationCoordinator
    store: ConversationStore
    session_factory: Callable[[], Session]
    agent: ClaudeToolAgent
    processing_broker: BrokerPort
    outbound_broker: OutboundBrokerPort
    transport: WhatsAppService
    clock: Clock
    readiness_status: Callable[[], ReadinessReport]


RETRYABLE_ERRORS = (ContactLockUnavailable, ContactLeaseLost,
    ConversationGenerationUnavailable, ConversationStateUnavailable,
    ConversationMutationPending, ConversationMutationAmbiguous,
    ReadinessUnavailable, BrokerUnavailable, AgentUnavailable, TransportUnavailable)


class RetryRequested(ConversationDomainError):
    """Sanitized reason; the explicit command is used only for task redelivery."""
    def __init__(self, reason_code, command=None):
        super().__init__(reason_code)
        self.command = command


def _require_ready(runtime):
    try:
        if runtime is None or not runtime.readiness_status().ready:
            raise ReadinessUnavailable(FailureReason.READINESS_UNAVAILABLE)
    except CeleryRetry:
        raise
    except Exception:
        raise ReadinessUnavailable(FailureReason.READINESS_UNAVAILABLE) from None


def _enqueue(outbound, runtime, lease):
    lease.assert_owned()
    _require_ready(runtime)
    try:
        result = runtime.outbound_broker.enqueue_outbound(outbound)
    except CeleryRetry:
        raise
    except Exception:
        raise BrokerUnavailable(FailureReason.BROKER_UNAVAILABLE) from None
    if result is not EnqueueResult.CONFIRMED:
        raise BrokerUnavailable(FailureReason.BROKER_UNAVAILABLE)


@contextmanager
def _session(runtime):
    _require_ready(runtime)
    try:
        session = runtime.session_factory()
    except CeleryRetry:
        raise
    except Exception:
        raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE) from None
    try:
        with session as db:
            _require_ready(runtime)
            yield db
    except SQLAlchemyError:
        raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE) from None


def process_batch(command: ProcessingCommand, runtime: ConversationRuntime) -> ProcessingOutcome:
    """Claim, stage, commit, enqueue and complete under one renewable lease."""
    command = ProcessingCommand.from_payload(command.to_payload())
    _emit_audit(AuditEvent.PROCESSING, outcome="started", attempt_state="claiming")
    try:
        _require_ready(runtime)
        with runtime.store.contact_lease(command.phone) as lease:
            _require_ready(runtime)
            try:
                claim = runtime.store.claim_or_resume_batch(command, runtime.clock.now(), lease)
            except RETRYABLE_ERRORS:
                # A lost acknowledgement may hide a successful atomic claim.
                # Recover only metadata using the same lease; never drain again.
                try:
                    dispatch = runtime.store.dispatch(command, lease)
                    if dispatch.processing_id:
                        command = replace(command, processing_id=dispatch.processing_id,
                            operation_id=dispatch.operation_id, staging_id=command.batch_id)
                except RETRYABLE_ERRORS:
                    pass  # Coordination may still be unavailable; batch ID survives.
                raise
            if claim.outcome is ClaimOutcome.TERMINAL:
                _emit_audit(AuditEvent.PROCESSING, outcome="terminal", attempt_state="terminal")
                return ProcessingOutcome.TERMINAL
            if claim.outcome is ClaimOutcome.DUPLICATE:
                _emit_audit(AuditEvent.PROCESSING, outcome="duplicate", attempt_state="duplicate")
                return ProcessingOutcome.DUPLICATE
            attempt = claim.attempt
            command = replace(command, processing_id=attempt.processing_id,
                operation_id=attempt.operation_id, staging_id=command.batch_id)
            _require_ready(runtime)
            result = claim.result
            fixed = tuple(e for e in claim.envelopes if e.kind != "text")
            texts = tuple(e for e in claim.envelopes if e.kind == "text")
            with _session(runtime) as db:
                if result is None:
                    snapshot = runtime.coordinator.processing_snapshot(db, command.phone, runtime.clock.now(), lease)
                    if texts:
                        lease.assert_owned()
                        _require_ready(runtime)
                        result = runtime.agent.prepare_result("\n".join(e.content for e in texts), command.phone, snapshot)
                        if fixed:
                            result = replace(result, text=fixed_reply_result(fixed).text + "\n\n" + result.text)
                    else:
                        result = fixed_reply_result(fixed)
                    # Preserve the acknowledged result before stopping later effects.
                    runtime.store.stage_agent_result(command, attempt, result, runtime.clock.now(), lease)
                _require_ready(runtime)
                if texts:
                    outbound = runtime.coordinator.apply_agent_result(db, command.phone, result,
                        attempt.processing_id, attempt.operation_id, runtime.clock.now(), lease)
                else:
                    # A staged fixed result has no context delta or SQL transition.
                    runtime.coordinator.processing_snapshot(db, command.phone, runtime.clock.now(), lease)
                    _require_ready(runtime)
                    runtime.store.prepare_fixed_response(command, attempt, runtime.clock.now(), lease)
                    outbound = OutboundEnvelope(command.phone, result.text, OutboundKind.NORMAL,
                        command.generation, attempt.processing_id, attempt.operation_id)
                _require_ready(runtime)  # SQL commit may have outlived readiness.
                reservation = runtime.store.reserve_outbound_enqueue(command, attempt, runtime.clock.now(), lease)
                _enqueue(outbound, runtime, lease)
                # A local acknowledgement must survive readiness closing in enqueue.
                runtime.store.record_outbound_attempt(command, attempt, runtime.clock.now(), lease, reservation=reservation)
                runtime.store.complete_batch(command, attempt, runtime.clock.now(), lease, reservation=reservation)
            _emit_audit(AuditEvent.PROCESSING, outcome="processed", attempt_state="completed")
            return ProcessingOutcome.PROCESSED
    except ConversationMutationAborted:
        _emit_audit(AuditEvent.PROCESSING, outcome="terminal", attempt_state="aborted")
        return ProcessingOutcome.TERMINAL
    except RETRYABLE_ERRORS as exc:
        _emit_audit(AuditEvent.PROCESSING, outcome=_retry_outcome(exc), attempt_state="retryable")
        raise RetryRequested(exc.reason_code, command) from None


class _SimulatorCapture:
    """Acknowledgement means captured in this request, never provider delivery."""
    def __init__(self):
        self.commands: list[ProcessingCommand] = []
        self.outbound: list[OutboundEnvelope] = []

    def enqueue_processing(self, command: ProcessingCommand) -> EnqueueResult:
        command = ProcessingCommand.from_payload(command.to_payload())
        if command.phone != ConversationCoordinator.TEST_PHONE:
            raise BrokerUnavailable(FailureReason.BROKER_UNAVAILABLE)
        self.commands.append(command)
        return EnqueueResult.CONFIRMED

    def enqueue_outbound(self, outbound: OutboundEnvelope) -> EnqueueResult:
        outbound = OutboundEnvelope.from_payload(outbound.to_payload())
        if outbound.phone != ConversationCoordinator.TEST_PHONE:
            raise BrokerUnavailable(FailureReason.BROKER_UNAVAILABLE)
        self.outbound.append(outbound)
        return EnqueueResult.CONFIRMED


def simulate_message(message: str, runtime: ConversationRuntime) -> str:
    """Run synthetic patient ingress and the canonical task with local captures."""
    _require_ready(runtime)
    capture = _SimulatorCapture()
    local = ConversationRuntime(runtime.coordinator, runtime.store, runtime.session_factory,
        runtime.agent, capture, capture, None, runtime.clock, runtime.readiness_status)
    phone = ConversationCoordinator.TEST_PHONE
    with local.store.contact_lease(phone) as lease, _session(local) as db:
        _require_ready(local)
        receipt = local.coordinator.accept_ingress(db,
            SenderIdentity(phone, False, str(uuid4()), "pn"), "text", message,
            local.clock.now(), lease, capture)
    if receipt.disposition is IngressDisposition.DROPPED:
        _emit_audit(AuditEvent.PROCESSING, outcome="paused", cycle="paused")
        return "[Bot pausado para este número - aguardando atendimento humano]"
    if receipt.disposition is not IngressDisposition.BUFFERED or not capture.commands:
        raise BrokerUnavailable(FailureReason.BROKER_UNAVAILABLE)
    for command in capture.commands:
        if process_batch(command, local) is not ProcessingOutcome.PROCESSED:
            raise BrokerUnavailable(FailureReason.BROKER_UNAVAILABLE)
    response = "\n\n".join(outbound.text for outbound in capture.outbound)
    _emit_audit(AuditEvent.PROCESSING, outcome="simulated", attempt_state="captured")
    return response


def send_outbound(outbound: OutboundEnvelope, runtime: ConversationRuntime) -> SendOutcome:
    """Authorize using fresh SQL and keep the lease across the provider call."""
    outbound = OutboundEnvelope.from_payload(outbound.to_payload())
    _emit_audit(AuditEvent.OUTBOUND, outcome="started", attempt_state="authorizing")
    try:
        _require_ready(runtime)
        with _session(runtime) as db, runtime.store.contact_lease(outbound.phone) as lease:
            _require_ready(runtime)
            if not runtime.coordinator.may_send(db, outbound, runtime.clock.now(), lease):
                _emit_audit(AuditEvent.OUTBOUND, outcome="discarded", attempt_state="terminal")
                return SendOutcome.DISCARDED
            lease.assert_owned()  # Fence check before final readiness/transport boundary.
            _require_ready(runtime)
            try:
                result = runtime.transport.send_message(outbound.phone, outbound.text)
                if inspect.isawaitable(result):
                    result = asyncio.run(result)
            except CeleryRetry:
                raise
            except Exception:
                raise TransportUnavailable(FailureReason.TRANSPORT_UNAVAILABLE) from None
            if result is not True:
                raise TransportUnavailable(FailureReason.TRANSPORT_UNAVAILABLE)
            _emit_audit(AuditEvent.OUTBOUND, outcome="sent", attempt_state="completed")
            return SendOutcome.SENT
    except RETRYABLE_ERRORS as exc:
        _emit_audit(AuditEvent.OUTBOUND, outcome=_retry_outcome(exc), attempt_state="retryable")
        raise RetryRequested(exc.reason_code) from None
