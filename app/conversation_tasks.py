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
from typing import Callable, Protocol, TYPE_CHECKING

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
)

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
    try:
        session = runtime.session_factory()
    except CeleryRetry:
        raise
    except Exception:
        raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE) from None
    try:
        with session as db:
            yield db
    except SQLAlchemyError:
        raise ConversationStateUnavailable(FailureReason.STATE_UNAVAILABLE) from None


def process_batch(command: ProcessingCommand, runtime: ConversationRuntime) -> ProcessingOutcome:
    """Claim, stage, commit, enqueue and complete under one renewable lease."""
    command = ProcessingCommand.from_payload(command.to_payload())
    try:
        _require_ready(runtime)
        with runtime.store.contact_lease(command.phone) as lease:
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
                return ProcessingOutcome.TERMINAL
            if claim.outcome is ClaimOutcome.DUPLICATE:
                return ProcessingOutcome.DUPLICATE
            attempt = claim.attempt
            command = replace(command, processing_id=attempt.processing_id,
                operation_id=attempt.operation_id, staging_id=command.batch_id)
            result = claim.result
            fixed = tuple(e for e in claim.envelopes if e.kind != "text")
            texts = tuple(e for e in claim.envelopes if e.kind == "text")
            with _session(runtime) as db:
                if result is None:
                    snapshot = runtime.coordinator.processing_snapshot(db, command.phone, runtime.clock.now(), lease)
                    if texts:
                        lease.assert_owned()
                        result = runtime.agent.prepare_result("\n".join(e.content for e in texts), command.phone, snapshot)
                        if fixed:
                            result = replace(result, text=fixed_reply_result(fixed).text + "\n\n" + result.text)
                    else:
                        result = fixed_reply_result(fixed)
                    runtime.store.stage_agent_result(command, attempt, result, runtime.clock.now(), lease)
                if texts:
                    outbound = runtime.coordinator.apply_agent_result(db, command.phone, result,
                        attempt.processing_id, attempt.operation_id, runtime.clock.now(), lease)
                else:
                    # A staged fixed result has no context delta or SQL transition.
                    runtime.coordinator.processing_snapshot(db, command.phone, runtime.clock.now(), lease)
                    runtime.store.prepare_fixed_response(command, attempt, runtime.clock.now(), lease)
                    outbound = OutboundEnvelope(command.phone, result.text, OutboundKind.NORMAL,
                        command.generation, attempt.processing_id, attempt.operation_id)
                reservation = runtime.store.reserve_outbound_enqueue(command, attempt, runtime.clock.now(), lease)
                _enqueue(outbound, runtime, lease)
                runtime.store.record_outbound_attempt(command, attempt, runtime.clock.now(), lease, reservation=reservation)
                runtime.store.complete_batch(command, attempt, runtime.clock.now(), lease, reservation=reservation)
            return ProcessingOutcome.PROCESSED
    except ConversationMutationAborted:
        return ProcessingOutcome.TERMINAL
    except RETRYABLE_ERRORS as exc:
        raise RetryRequested(exc.reason_code, command) from None


def send_outbound(outbound: OutboundEnvelope, runtime: ConversationRuntime) -> SendOutcome:
    """Authorize using fresh SQL and keep the lease across the provider call."""
    outbound = OutboundEnvelope.from_payload(outbound.to_payload())
    try:
        _require_ready(runtime)
        with _session(runtime) as db, runtime.store.contact_lease(outbound.phone) as lease:
            if not runtime.coordinator.may_send(db, outbound, runtime.clock.now(), lease):
                return SendOutcome.DISCARDED
            lease.assert_owned()  # Last operation before entering the transport.
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
            return SendOutcome.SENT
    except RETRYABLE_ERRORS as exc:
        raise RetryRequested(exc.reason_code) from None
