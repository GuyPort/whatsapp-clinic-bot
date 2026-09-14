"""Synthetic flow contracts; no application lifespan or external services."""

import importlib
import json
import logging
import socket
import sys
import traceback
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import anthropic
import pytest
from anthropic.types import TextBlock, ToolUseBlock
from sqlalchemy.orm import Session

from app import conversation_state as domain
from app import utils
from app.conversation_redis import RedisConversationStore
from tests.fakes import ForbiddenAgentEffects, ManualClock, ScriptedClaude
from tests.fakes import WebhookRequest, webhook_payload


ADMIN_PHONE = "5551999990011"
SIMULATOR_PHONE = "5500000000000"


def recovery_api():
    assert importlib.util.find_spec("app.conversation_recovery") is not None, "recovery service missing"
    return importlib.import_module("app.conversation_recovery")


def close_ready(runtime):
    runtime.dependencies[domain.DependencyName.SECRET] = False


@pytest.mark.parametrize("_case", [None], ids=["flow-08-delayed-worker-after-pause"])
def test_paused_ingress_cannot_reappear_in_a_worker_delayed_past_expiry(main_module, admin_runtime, task_api, _case):
    rt = admin_runtime
    old = rt.buffer("older pending input")
    reference = rt.pause()
    assert webhook(main_module, text="discarded while paused", message_id="paused-drop").status_code == 200
    rt.clock.set(reference.paused_until + timedelta(seconds=1))
    assert task_api.process_batch(old, rt) is task_api.ProcessingOutcome.TERMINAL
    assert rt.agent.calls == rt.outbound_broker.calls == rt.transport.calls == []
    assert "discarded while paused" not in str(rt.store.snapshot())
    assert len(rt.processing_broker.calls) == 1


@pytest.mark.parametrize("_case", [None], ids=["flow-25-producer-retries-during-staging-owner"])
def test_concurrent_producer_retains_new_message_after_other_batch_is_staged(main_module, admin_runtime, task_api, _case):
    from threading import Event, Thread
    rt = admin_runtime
    command = rt.buffer("first batch")
    staged, finish = Event(), Event()
    outcomes, errors = [], []
    def model_boundary():
        staged.set()
        assert finish.wait(5), "producer did not reach the ownership boundary"
    rt.agent.on_prepare = model_boundary
    def consume():
        try:
            outcomes.append(task_api.process_batch(command, rt))
        except Exception as error:
            errors.append(error)
    worker = Thread(target=consume)
    worker.start()
    try:
        assert staged.wait(5)
        assert webhook(main_module, text="new message", message_id="concurrent-producer").status_code == 503
    finally:
        finish.set()
        worker.join(timeout=5)
    assert not worker.is_alive() and errors == []
    assert outcomes == [task_api.ProcessingOutcome.PROCESSED]
    assert webhook(main_module, text="new message", message_id="concurrent-producer").status_code == 200
    assert [entry["content"] for entry in rt.envelopes()] == ["new message"]
    assert [call[0] for call in rt.agent.calls] == ["first batch"]
    assert rt.processing_broker.calls[-1].batch_id != command.batch_id


@pytest.mark.parametrize("_case", [None], ids=["flow-42-long-pause-ttl-and-epoch"])
def test_long_pause_extension_retains_generation_and_epoch_loss_blocks_old_work(processing_runtime, task_api, _case):
    from dataclasses import replace
    from uuid import UUID
    from app.conversation_redis import contact_keys, EpochStore, GLOBAL_EPOCH_KEY
    rt = processing_runtime
    old_command = rt.buffer("old work")
    with rt.store.contact_lease(PHONE) as lease, rt.session_factory() as db:
        reference = rt.coordinator.pause_manual(db, PHONE, 30 * 24, "secretary_dashboard_pause", rt.clock.now(), lease, "long-pause")
    rt.clock.advance(timedelta(days=8))
    with rt.store.contact_lease(PHONE) as lease, rt.session_factory() as db:
        extended = rt.coordinator.extend_pause(db, PHONE, 48, rt.clock.now(), lease, "extend-long")
        assert extended.generation != reference.generation
        assert extended.paused_until == reference.paused_until + timedelta(days=2)
        key = contact_keys(PHONE).generation
        assert rt.store.client.values[key]
        expiry = rt.store.client.expiry.get(key)
        assert expiry is None or expiry >= (extended.paused_until + timedelta(days=7)).timestamp()
    old_epoch = rt.store.config.coordination_epoch
    rt.store.delete_global_epoch()
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(old_command, rt)
    rt.store.client.values[GLOBAL_EPOCH_KEY] = str(old_epoch)  # Isolated fake restoration only.
    new_epoch = UUID("00000000-0000-4000-8000-000000000088")
    assert EpochStore(rt.store.client, replace(rt.store.config, coordination_epoch=new_epoch)).rotate(old_epoch, new_epoch) == new_epoch
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(old_command, rt)
    assert rt.agent.calls == rt.outbound_broker.calls == rt.transport.calls == []


@pytest.mark.parametrize("_case", [None], ids=["flow-09-passive-expiry"])
def test_clock_and_background_passes_do_not_send_pause_expiration(admin_runtime, scheduler_module, _case):
    import asyncio
    rt = admin_runtime
    reference = rt.pause()
    rt.clock.set(reference.paused_until + timedelta(seconds=1))
    asyncio.run(scheduler_module.check_inactive_contexts(rt))
    recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert rt.processing_broker.calls == rt.outbound_broker.calls == rt.agent.calls == rt.transport.calls == []


@pytest.mark.parametrize("_case", [None], ids=["flow-11-existing-context-handoff"])
def test_transfer_discards_preexisting_context_and_never_resaves_agent_delta(processing_runtime, task_api, session_factory, _case):
    from app.models import ConversationContext, PausedContact
    rt = processing_runtime
    rt.seed_contact(PHONE)
    rt.agent.intent = domain.AgentIntent.PAUSE_FOR_SECRETARY
    assert task_api.process_batch(rt.buffer("ATENDIMENTO"), rt) is task_api.ProcessingOutcome.PROCESSED
    assert rt.agent.calls[0][2].messages
    with session_factory() as db:
        assert db.get(ConversationContext, PHONE) is None
        assert db.get(PausedContact, PHONE).paused_until == (rt.clock.now() + timedelta(hours=24)).replace(tzinfo=None)


@pytest.mark.parametrize("_case", [None], ids=["flow-16-local-confirmation-request"])
def test_transfer_submits_one_local_confirmation_without_claiming_provider_delivery(processing_runtime, task_api, _case):
    rt = processing_runtime
    rt.agent.intent = domain.AgentIntent.PAUSE_FOR_SECRETARY
    command = rt.buffer("ATENDIMENTO")
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert len(rt.outbound_broker.calls) == 1
    confirmation = rt.outbound_broker.calls[0]
    assert confirmation.kind is domain.OutboundKind.TRANSFER_CONFIRMATION
    assert confirmation.pause_ref.generation == confirmation.generation
    assert rt.transport.calls == []
    # Provider ambiguity is observable separately from the successful local enqueue.
    rt.transport.result = False
    with pytest.raises(task_api.RetryRequested):
        task_api.send_outbound(confirmation, rt)
    assert len(rt.transport.calls) == len(rt.outbound_broker.calls) == 1


@pytest.mark.parametrize("boundary", ["lock", "sql"], ids=["flow-27-before-staging", "flow-27-after-staging"])
def test_retry_keeps_original_buffer_or_staging_at_dependency_failure(processing_runtime, task_api, monkeypatch, boundary):
    rt = processing_runtime
    command = rt.buffer("retained input")
    original = rt.session_factory
    if boundary == "lock":
        rt.store.fail_next_atomic("acquire")
    else:
        def failed_session():
            raise domain.ConversationStateUnavailable(domain.FailureReason.STATE_UNAVAILABLE)
        monkeypatch.setattr(rt, "session_factory", failed_session)
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    assert [entry["content"] for entry in rt.envelopes()] == ["retained input"]
    details = rt.details()
    assert any(item.entry.kind == ("buffer" if boundary == "lock" else "staging") for item in details)
    assert rt.agent.calls == rt.outbound_broker.calls == rt.transport.calls == []
    monkeypatch.setattr(rt, "session_factory", original)
    if boundary == "sql":
        assert caught.value.command.staging_id == command.batch_id
        rt.clock.advance(timedelta(seconds=45))
    assert task_api.process_batch(caught.value.command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert len(rt.agent.calls) == 1


@pytest.mark.parametrize("_case", [None], ids=["flow-35-authenticated-duplicate-effects"])
def test_authenticated_duplicate_has_one_buffer_result_and_local_effect_set(main_module, admin_runtime, task_api, _case):
    rt = admin_runtime
    for _ in range(2):
        assert webhook(main_module, text="one synthetic input", message_id="same-provider-id").status_code == 200
    assert len(rt.envelopes()) == 1
    command = rt.processing_broker.calls[0]
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.TERMINAL
    assert len(rt.agent.calls) == len(rt.outbound_broker.calls) == 1
    assert sum(session.events.count("commit_entered") for session in rt.sessions) == 1
    assert task_api.send_outbound(rt.outbound_broker.calls[0], rt) is task_api.SendOutcome.SENT
    assert len(rt.transport.calls) == 1


@pytest.mark.parametrize("_case", [None], ids=["flow-44-paused-id-retained-after-expiry"])
def test_dropped_id_remains_terminal_after_long_pause_without_retaining_text(main_module, admin_runtime, _case):
    rt = admin_runtime
    with rt.store.contact_lease(PHONE) as lease, rt.session_factory() as db:
        reference = rt.coordinator.pause_manual(db, PHONE, 30 * 24, "secretary_dashboard_pause", rt.clock.now(), lease, "long-manual")
    assert webhook(main_module, message_id="long-dropped-id", text="must be discarded").status_code == 200
    rt.clock.set(reference.paused_until + timedelta(days=1))
    assert webhook(main_module, message_id="long-dropped-id", text="replay must be discarded").status_code == 200
    assert rt.processing_broker.calls == rt.agent.calls == rt.outbound_broker.calls == rt.transport.calls == []
    assert "must be discarded" not in str(rt.store.snapshot())


@pytest.mark.parametrize("failure", ["definitive", "ack-loss"], ids=["flow-47-no-provider-replay", "flow-48-broker-ack-before-record"])
def test_recovery_dispatches_persisted_batch_without_another_inbound(main_module, admin_runtime, task_api, failure):
    rt = admin_runtime
    if failure == "definitive":
        rt.processing_broker.next_result = domain.EnqueueResult.DEFINITIVE_FAILURE
    else:
        rt.store.fail_next_atomic("finish_enqueue")
    assert webhook(main_module, text="recoverable input").status_code == 503
    command = rt.processing_broker.calls[0]
    assert [entry["content"] for entry in rt.envelopes()] == ["recoverable input"]
    rt.processing_broker.next_result = domain.EnqueueResult.CONFIRMED
    rt.clock.advance(timedelta(seconds=60))
    assert recovery_api().RecoveryService(rt).run_once(rt.clock.now()).rescheduled == 1
    assert len(rt.processing_broker.calls) == 2
    assert all(item.batch_id == command.batch_id for item in rt.processing_broker.calls)
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.TERMINAL
    assert len(rt.agent.calls) == len(rt.outbound_broker.calls) == 1


@pytest.mark.parametrize("_case", [None], ids=["flow-49-delayed-past-old-buffer-window"])
def test_recovery_keeps_delayed_batch_content_inside_dispatch_horizon(processing_runtime, task_api, _case):
    rt = processing_runtime
    command = rt.buffer("delayed synthetic input")
    rt.clock.advance(timedelta(seconds=311))
    assert recovery_api().RecoveryService(rt).run_once(rt.clock.now()).rescheduled == 1
    assert [entry["content"] for entry in rt.envelopes()] == ["delayed synthetic input"]
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert rt.agent.calls[0][0] == "delayed synthetic input"


@pytest.mark.parametrize("_case", [None], ids=["flow-63-terminal-task-no-content-or-sql"])
def test_exhausted_task_returns_before_content_sql_model_or_transport(processing_runtime, task_api, monkeypatch, _case):
    from app.conversation_redis import contact_keys
    rt = processing_runtime
    command = rt.buffer()
    rt.clock.advance(timedelta(seconds=900))
    assert recovery_api().RecoveryService(rt).run_once(rt.clock.now()).exhausted == 1
    keys = contact_keys(command.phone)
    original = rt.store.client.get
    def metadata_only(key):
        assert not key.startswith((keys.buffer_prefix, keys.staging_prefix)), "terminal task read content"
        return original(key)
    monkeypatch.setattr(rt.store.client, "get", metadata_only)
    monkeypatch.setattr(rt, "session_factory", lambda: pytest.fail("terminal task opened SQL"))
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.TERMINAL
    assert rt.agent.calls == rt.outbound_broker.calls == rt.transport.calls == []


@pytest.mark.parametrize("_case", [None], ids=["flow-74-worker-initial-staging-atomicity"])
def test_worker_initial_staging_faults_never_leave_orphan_claim_or_partial_manifest(session_factory, task_api, _case):
    from app.simple_config import settings
    from tests.fakes import ProcessingRuntime
    config = domain.ConversationConfig.from_settings(settings)
    baseline = ProcessingRuntime(session_factory, config)
    command = baseline.buffer()
    with baseline.store.contact_lease(PHONE) as lease:
        baseline.store.claim_or_resume_batch(command, baseline.clock.now(), lease)
    count = baseline.store.client.write_counts["claim_or_resume_batch"]
    assert count >= 4
    for index in range(count):
        rt = ProcessingRuntime(session_factory, config)
        command = rt.buffer()
        before = rt.store.contact_snapshot(PHONE), deepcopy(rt.store.client.sets)
        rt.store.client.fail_write_at = ("claim_or_resume_batch", index)
        with pytest.raises(task_api.RetryRequested):
            task_api.process_batch(command, rt)
        assert (rt.store.contact_snapshot(PHONE), rt.store.client.sets) == before
        assert rt.agent.calls == rt.outbound_broker.calls == rt.transport.calls == []
        assert not any("commit_entered" in session.events for session in rt.sessions)
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.PROCESSED


@pytest.mark.parametrize("offset", [0, 1], ids=["flow-72-at-deadline", "flow-72-after-deadline"])
def test_late_agent_result_is_rejected_while_lease_remains_valid(processing_runtime, task_api, offset):
    from app.conversation_redis import contact_keys
    rt = processing_runtime
    command = rt.buffer()
    started = rt.clock.now()
    def delayed_result():
        rt.clock.set(started + timedelta(seconds=600, microseconds=offset))
        rt.store.client.expiry[contact_keys(command.phone).lease] = (rt.clock.now() + timedelta(seconds=60)).timestamp()
    rt.agent.on_prepare = delayed_result
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    assert len(rt.agent.calls) == 1
    assert not any("flush" in session.events or "commit_entered" in session.events for session in rt.sessions)
    assert rt.outbound_broker.calls == rt.transport.calls == []
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.TERMINAL
    assert not rt.envelopes()


@pytest.mark.parametrize("fault,offset", [
    pytest.param("lease", 0, id="flow-40-lost-after-flush"),
    pytest.param("deadline", 0, id="flow-73-at-deadline"),
    pytest.param("deadline", 1, id="flow-73-after-deadline"),
])
def test_processing_flush_cannot_cross_failed_commit_authority(processing_runtime, task_api, fault, offset):
    from app.conversation_redis import contact_keys
    rt = processing_runtime
    command = rt.buffer()
    started = rt.clock.now()
    def after_flush():
        key = contact_keys(command.phone).lease
        if fault == "lease":
            rt.store.client.values.pop(key)
        else:
            rt.clock.set(started + timedelta(seconds=600, microseconds=offset))
            rt.store.client.expiry[key] = (rt.clock.now() + timedelta(seconds=60)).timestamp()
    rt.persistent_session_hooks["flush"] = after_flush
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    assert rt.sessions[-1].events.count("flush") == 1
    assert "rollback" in rt.sessions[-1].events
    assert "commit_entered" not in rt.sessions[-1].events
    assert rt.outbound_broker.calls == rt.transport.calls == []
    if fault == "deadline":
        assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.TERMINAL


@pytest.mark.parametrize("intent,checkpoint", [
    pytest.param(domain.AgentIntent.PAUSE_FOR_SECRETARY, "commit_entered", id="flow-39-handoff-commit"),
    pytest.param(domain.AgentIntent.SAVE_CONTEXT, "commit_entered", id="flow-57-normal-commit"),
    pytest.param(domain.AgentIntent.SAVE_CONTEXT, "commit_returned", id="state-36-normal-finalization"),
])
def test_new_owner_cannot_repeat_sql_or_model_during_old_commit(processing_runtime, task_api, intent, checkpoint):
    rt = processing_runtime
    command = rt.buffer()
    rt.agent.intent = intent
    observed = []
    def blocked_commit():
        rt.clock.advance(timedelta(seconds=61))
        calls = len(rt.agent.calls), len(rt.outbound_broker.calls), rt.session_calls
        with pytest.raises(task_api.RetryRequested):
            task_api.process_batch(command, rt)
        assert (len(rt.agent.calls), len(rt.outbound_broker.calls), rt.session_calls) == calls
        observed.append("new owner refused while old commit is still entered")
    rt.session_hooks[checkpoint] = blocked_commit
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    assert observed == ["new owner refused while old commit is still entered"]
    assert sum(session.events.count("commit_returned") for session in rt.sessions) == 1
    assert len(rt.agent.calls) == 1
    assert rt.outbound_broker.calls == rt.transport.calls == []
    rt.clock.advance(timedelta(seconds=600))
    recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert rt.store.is_quarantined(command.phone)


@pytest.mark.parametrize("_case", [None], ids=["flow-59-old-model-response"])
def test_takeover_accepts_only_new_claim_result_after_old_model_returns(processing_runtime, task_api, _case):
    from threading import Event, Thread
    from app.conversation_redis import contact_keys
    rt = processing_runtime
    command = rt.buffer()
    entered, return_old = Event(), Event()
    old_errors = []
    def model_boundary():
        if len(rt.agent.calls) == 1:
            entered.set()
            assert return_old.wait(5), "new claimant did not finish"
    rt.agent.on_prepare = model_boundary
    def old_worker():
        try:
            task_api.process_batch(command, rt)
        except Exception as error:
            old_errors.append(error)
    worker = Thread(target=old_worker)
    worker.start()
    try:
        assert entered.wait(5), "old model did not start"
        rt.store.client.values.pop(contact_keys(command.phone).lease)
        rt.clock.advance(timedelta(seconds=45))
        assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.PROCESSED
    finally:
        return_old.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(old_errors) == 1 and isinstance(old_errors[0], task_api.RetryRequested)
    assert len(rt.agent.calls) == 2
    assert sum(session.events.count("commit_entered") for session in rt.sessions) == 1
    assert len(rt.outbound_broker.calls) == 1
    assert rt.transport.calls == []


@pytest.mark.parametrize("_case", [None], ids=["flow-64-two-recoverers"])
def test_two_recoverers_share_one_due_reservation_and_one_winning_batch(processing_runtime, task_api, monkeypatch, _case):
    from threading import Barrier, Thread
    rt = processing_runtime
    command = rt.buffer()
    rt.clock.advance(timedelta(seconds=60))
    expected, position = rt.store.recovery_checkpoint()
    rt.store.save_recovery_checkpoint(expected, (1, *position[1:]))
    discovered = Barrier(2)
    original = rt.store.recoverable_batches
    def same_page(*args, **kwargs):
        page = original(*args, **kwargs)
        discovered.wait(timeout=5)
        return page
    monkeypatch.setattr(rt.store, "recoverable_batches", same_page)
    reports, errors = [], []
    def recover():
        try:
            reports.append(recovery_api().RecoveryService(rt, max_pages=1).run_once(rt.clock.now()))
        except Exception as error:
            errors.append(error)
    workers = [Thread(target=recover) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=8)
    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert len(reports) == 2
    assert sum(report.rescheduled for report in reports) == 1
    assert len(rt.processing_broker.calls) == 2
    assert all(call.batch_id == command.batch_id for call in rt.processing_broker.calls)
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.TERMINAL
    assert len(rt.agent.calls) == len(rt.outbound_broker.calls) == 1
    assert rt.envelopes() == []


@pytest.mark.parametrize("boundary", ["json", "acquire", "finalize_ingress_once", "reserve_enqueue"])
def test_ready_race_ingress_stops_before_next_effect(main_module, ingress_runtime, monkeypatch, boundary):
    import asyncio
    rt = ingress_runtime
    request = WebhookRequest(main_module.app, webhook_payload())
    if boundary == "json":
        original = request.json
        async def body():
            result = await original()
            close_ready(rt)
            return result
        monkeypatch.setattr(request, "json", body)
    else:
        rt.store.client.after_operation[boundary] = lambda: close_ready(rt)
    response = asyncio.run(main_module.whatsapp_webhook(request))
    assert response.status_code == 503
    assert rt.processing_broker.calls == []
    if boundary == "json":
        assert rt.lease_calls == rt.session_calls == 0
    elif boundary == "acquire":
        assert rt.session_calls == 0


@pytest.mark.parametrize("boundary", ["acquire", "claim_or_resume_batch", "snapshot", "agent",
    "stage_agent_result", "prepare_mutation", "flush", "enter_committing", "commit_returned", "reserve_outbound_enqueue"])
def test_ready_race_processing_rechecks_before_agent_sql_and_outbound(processing_runtime, task_api, monkeypatch, boundary):
    rt = processing_runtime
    command = rt.buffer()
    if boundary == "snapshot":
        original = rt.coordinator.processing_snapshot
        def snapshot(*args, **kwargs):
            result = original(*args, **kwargs)
            close_ready(rt)
            return result
        monkeypatch.setattr(rt.coordinator, "processing_snapshot", snapshot)
    elif boundary == "agent":
        rt.agent.on_prepare = lambda: close_ready(rt)
    elif boundary in ("flush", "commit_returned"):
        rt.persistent_session_hooks[boundary] = lambda: close_ready(rt)
    else:
        rt.store.client.after_operation[boundary] = lambda: close_ready(rt)
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    assert rt.outbound_broker.calls == rt.transport.calls == []
    if boundary in ("acquire", "claim_or_resume_batch", "snapshot"):
        assert rt.agent.calls == []
    with rt._factory() as db:
        from app.models import ConversationContext
        assert db.bind.url.database in (None, "", ":memory:")
        row = db.get(ConversationContext, command.phone)
        assert (row is not None) is (boundary in ("commit_returned", "reserve_outbound_enqueue"))


def test_ready_race_outbound_ack_still_records_and_completes(processing_runtime, task_api):
    rt = processing_runtime
    command = rt.buffer()
    rt.outbound_broker.on_enqueue = lambda outbound: close_ready(rt)
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert len(rt.outbound_broker.calls) == 1
    processing = next(d for d in rt.details() if d.entry.kind == "processing")
    assert processing.body["phase"] == "DONE"
    assert processing.body["outbound_attempted"] is True


def test_ready_race_processing_ack_is_preserved_when_ingress_readiness_closes(main_module, ingress_runtime):
    rt = ingress_runtime
    rt.processing_broker.on_enqueue = lambda command: close_ready(rt)
    response = webhook(main_module)
    assert response.status_code == 200
    assert len(rt.processing_broker.calls) == 1
    assert next(d for d in rt.details() if d.entry.kind == "batch").body["phase"] == "SCHEDULED"


def test_ready_race_agent_result_is_staged_before_retry_without_model_reentry(processing_runtime, task_api):
    rt = processing_runtime
    command = rt.buffer()
    rt.agent.on_prepare = lambda: close_ready(rt)
    with pytest.raises(task_api.RetryRequested) as raised:
        task_api.process_batch(command, rt)
    assert next(d for d in rt.details() if d.entry.kind == "processing").body["phase"] == "RESULT_READY"
    rt.dependencies[domain.DependencyName.SECRET] = True
    rt.agent.on_prepare = None
    assert task_api.process_batch(raised.value.command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert len(rt.agent.calls) == 1


def test_ready_race_provider_ack_is_not_reclassified_as_retry(processing_runtime, task_api):
    rt = processing_runtime
    command = rt.buffer()
    task_api.process_batch(command, rt)
    rt.transport.on_send = lambda: close_ready(rt)
    assert task_api.send_outbound(rt.outbound_broker.calls[-1], rt) is task_api.SendOutcome.SENT
    assert len(rt.transport.calls) == 1


@pytest.mark.parametrize("boundary", ["session", "acquire", "authorization", "last_lease_assert"])
@pytest.mark.parametrize("_case", [None], ids=["flow-19"])
def test_ready_race_sender_stops_before_provider(_case, processing_runtime, task_api, monkeypatch, boundary):
    from contextlib import contextmanager
    rt = processing_runtime
    command = rt.buffer()
    task_api.process_batch(command, rt)
    outbound = rt.outbound_broker.calls[-1]
    before = rt.lease_calls
    if boundary == "session":
        original = rt.session_factory
        @contextmanager
        def session():
            with original() as db:
                close_ready(rt)
                yield db
        monkeypatch.setattr(rt, "session_factory", session)
    elif boundary == "acquire":
        rt.store.client.after_operation["acquire"] = lambda: close_ready(rt)
    else:
        original = rt.coordinator.may_send
        def may_send(*args, **kwargs):
            result = original(*args, **kwargs)
            if boundary == "authorization":
                close_ready(rt)
            else:
                rt.store.client.after_operation["assert"] = lambda: close_ready(rt)
            return result
        monkeypatch.setattr(rt.coordinator, "may_send", may_send)
    with pytest.raises(task_api.RetryRequested):
        task_api.send_outbound(outbound, rt)
    assert rt.transport.calls == []
    if boundary == "session":
        assert rt.lease_calls == before


@pytest.mark.parametrize("boundary", ["acquire", "prepare_mutation"])
def test_ready_race_cleanup_stops_before_delete(admin_runtime, scheduler_module, boundary):
    import asyncio
    from app.models import ConversationContext
    rt = admin_runtime
    rt.seed_contact(ADMIN_PHONE, age_minutes=120)
    rt.store.client.after_operation[boundary] = lambda: close_ready(rt)
    asyncio.run(scheduler_module.check_inactive_contexts(rt))
    with rt._factory() as db:
        assert db.get(ConversationContext, ADMIN_PHONE) is not None


@pytest.mark.parametrize("boundary", ["reserve_enqueue", "checkpoint"])
def test_ready_race_recovery_stops_broker_and_checkpoint(admin_runtime, monkeypatch, boundary):
    from app.conversation_redis import RECOVERY_CHECKPOINT_KEY
    rt = admin_runtime
    rt.buffer()
    rt.clock.advance(timedelta(seconds=61))
    if boundary == "reserve_enqueue":
        rt.store.client.after_operation["reserve_enqueue"] = lambda: close_ready(rt)
    else:
        original = rt.store._ready
        def ready():
            original()
            if sys._getframe(1).f_code.co_name == "save_recovery_checkpoint":
                close_ready(rt)
        monkeypatch.setattr(rt.store, "_ready", ready)
    before = len(rt.processing_broker.calls)
    recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert len(rt.processing_broker.calls) == before
    if boundary == "checkpoint":
        assert rt.store.client.get(RECOVERY_CHECKPOINT_KEY) is None


@pytest.mark.parametrize("path", ["mutation", "batch"])
@pytest.mark.parametrize("dependency,offset,composed,close_at", [
    (name, 1, False, "anchor") for name in (domain.DependencyName.SECRET, domain.DependencyName.SQL,
        domain.DependencyName.REDIS, domain.DependencyName.BROKER)] + [
    (name, offset, True, "anchor") for name in domain.DependencyName for offset in (-1, 0, 1)] + [
    (domain.DependencyName.SQL, 1, True, "cas_scan")])
def test_recovery_snapshot_quarantine_rechecks_ready_after_anchor_read(processing_runtime, task_api, monkeypatch,
                                                                      path, dependency, offset, composed, close_at):
    from app.conversation_redis import contact_keys
    rt = processing_runtime
    command = rt.buffer()
    rt.store.fail_next_atomic("finalize_committed")
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    client = rt.store.client
    runtime = rt
    settings = SimpleNamespace(webhook_secret="synthetic-webhook-secret")
    if composed:
        monkeypatch.setattr(client, "ping", lambda: rt.dependencies[domain.DependencyName.REDIS])
        monkeypatch.setattr(rt.processing_broker, "probe", lambda: rt.dependencies[domain.DependencyName.BROKER])
        runtime = recovery_api().compose_runtime(settings=settings, client=client,
            config=rt.store.config, session_factory=rt.session_factory,
            sql_probe=lambda: rt.dependencies[domain.DependencyName.SQL], agent=rt.agent,
            processing_broker=rt.processing_broker, outbound_broker=rt.outbound_broker,
            transport=rt.transport, clock=rt.clock)
        # Inject a failed epoch probe, not a different lease/store or runtime.
        probes = runtime.readiness_status.__self__._probes
        epoch_probe = probes[domain.DependencyName.EPOCH]
        monkeypatch.setitem(probes, domain.DependencyName.EPOCH,
                            lambda: rt.dependencies[domain.DependencyName.EPOCH] and epoch_probe())
        assert runtime.readiness_status().ready
    rt.store.save_recovery_checkpoint(None, (0 if path == "mutation" else 1, None, None))
    rt.clock.advance(timedelta(seconds=600 + offset))
    keys = contact_keys(command.phone)
    retained = {key: value for key, value in client.values.items()
                if key == keys.anchor or key == keys.generation or key.startswith(keys.staging_prefix)
                or key.startswith(keys.processing_prefix) or key.startswith(keys.mutation_prefix)}
    assert any(key.startswith(keys.staging_prefix) for key in retained)
    indexes = deepcopy(client.sets)
    original = client.get
    closed = []
    def close_dependency():
        closed.append(True)
        rt.dependencies[dependency] = False
        if dependency is domain.DependencyName.SECRET:
            settings.webhook_secret = ""
    def get(key):
        result = original(key)
        if (close_at == "anchor" and key == keys.anchor and not closed
                and sys._getframe(2).f_code.co_name == "_snapshot"):
            close_dependency()
        return result
    monkeypatch.setattr(client, "get", get)
    if close_at == "cas_scan":
        original_scan = client.scan_iter
        def scan(*args, **kwargs):
            result = original_scan(*args, **kwargs)
            if not closed and sys._getframe(3).f_code.co_name == "_compact_mutation_fence":
                close_dependency()
            return result
        monkeypatch.setattr(client, "scan_iter", scan)
    # An ahead scan timestamp is only a hint, including the just-before case.
    report = recovery_api().RecoveryService(runtime, max_pages=1).run_once(rt.clock.now() + timedelta(seconds=2))
    assert closed == [True]
    assert report.quarantined == 0
    assert command.phone not in repr(report) and command.coordination_epoch not in repr(report)
    assert client.operation_calls.get("quarantine_mutation", 0) == 0
    assert {key: client.values.get(key) for key in retained} == retained
    assert client.sets == indexes
    rt.dependencies[dependency] = True
    settings.webhook_secret = "synthetic-webhook-secret"
    if composed and dependency is domain.DependencyName.REDIS:
        # Actual Redis unavailability also prevents lease release. Recovery must
        # respect the surviving owner until its bounded TTL, never force unlock.
        assert client.get(keys.lease) is not None
        held = recovery_api().RecoveryService(runtime).run_once(rt.clock.now() + timedelta(seconds=2))
        assert held.quarantined == 0 and held.failed > 0
        assert {key: client.values.get(key) for key in retained} == retained
        assert client.sets == indexes
        rt.clock.advance(timedelta(seconds=rt.store.config.contact_lease_ttl_seconds + 1))
    elif offset < 0:
        healthy = recovery_api().RecoveryService(runtime).run_once(rt.clock.now() + timedelta(seconds=2))
        assert (healthy.quarantined, healthy.failed) == (0, 0)
        assert {key: client.values.get(key) for key in retained} == retained
        assert client.sets == indexes
        rt.clock.advance(timedelta(seconds=1))
    resumed = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    assert (resumed.quarantined, resumed.failed) == (1, 0)
    assert client.operation_calls.get("quarantine_mutation", 0) == 1
    assert not any(key.startswith(keys.staging_prefix) for key in client.values)
    again = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    assert (again.quarantined, again.failed, again.rescheduled) == (0, 0, 0)
    assert client.operation_calls.get("quarantine_mutation", 0) == 1
    assert len(rt.agent.calls) == len(rt.processing_broker.calls) == 1
    assert rt.outbound_broker.calls == rt.transport.calls == []


@pytest.mark.parametrize("branch,dependency,composed,offset_us,skew_days", [
    (branch, dependency, composed, offset, skew)
    for branch in ("nested_mutation", "nested_batch") for dependency in domain.DependencyName
    for composed in (False, True) for offset in (-1, 0, 1) for skew in (-1, 1)] + [
    (branch, dependency, composed, 0, 1)
    for branch in ("invalid_details", "prepared_abort", "claimed_exhaust", "scheduled_exhaust", "reserve_enqueue")
    for dependency in domain.DependencyName for composed in (False, True)])
def test_recovery_destructive_transitions_keep_evidence_when_gate_closes(processing_runtime, task_api, monkeypatch, request,
                                                                       branch, dependency, composed, offset_us, skew_days):
    from app.conversation_redis import contact_keys, RECOVERY_CHECKPOINT_KEY
    from tests.fakes import compose_recovery_fixture, RecoveryWriteAudit
    rt = processing_runtime
    command = rt.buffer()
    if branch.startswith("nested") or branch == "prepared_abort":
        rt.store.fail_next_atomic("enter_committing" if branch == "prepared_abort" else "finalize_committed")
        with pytest.raises(task_api.RetryRequested):
            task_api.process_batch(command, rt)
    elif branch in ("claimed_exhaust", "invalid_details"):
        with rt.store.contact_lease(command.phone) as lease:
            rt.store.claim_or_resume_batch(command, rt.clock.now(), lease)
    client, keys = rt.store.client, contact_keys(command.phone)
    runtime = compose_recovery_fixture(rt, monkeypatch) if composed else rt
    store = runtime.store
    if branch == "invalid_details":
        processing_key = next(key for key in client.values if key.startswith(keys.processing_prefix))
        data = json.loads(client.values[processing_key])
        data["body"]["claim_token"] = ""
        client.values[processing_key] = json.dumps(data)
    start = rt.clock.now()
    if branch.startswith("nested"):
        rt.clock.advance(timedelta(seconds=600, microseconds=-1))
    else:
        rt.clock.advance(timedelta(seconds=901 if branch == "scheduled_exhaust" else 61 if branch == "reserve_enqueue" else 601))
    mutation_page = branch in ("nested_mutation", "prepared_abort")
    store.save_recovery_checkpoint(None, (0 if mutation_page else 1, None, None))
    scanner_name = "recoverable_mutations" if mutation_page else "recoverable_batches"
    scanner = getattr(store, scanner_name)
    page = (scanner(start + timedelta(days=1)) if mutation_page else scanner(now=start + timedelta(days=1)))
    # Hold an actual discovered page across caller skew; only the leased clock
    # and current state can authorize a transition after this stale hint.
    monkeypatch.setattr(store, scanner_name, lambda *args, **kwargs: page)
    retained = {key: value for key, value in client.values.items() if key not in (keys.lease, RECOVERY_CHECKPOINT_KEY)}
    indexes = deepcopy(client.sets)
    before = (len(rt.agent.calls), len(rt.processing_broker.calls), len(rt.outbound_broker.calls),
              len(rt.transport.calls), sum(s.events.count("commit_entered") for s in rt.sessions))
    from sqlalchemy import event
    engine = rt._factory.kw["bind"]
    assert engine.url.database in (None, "", ":memory:")
    sql_effects = []
    def observe_sql(connection, cursor, statement, parameters, context, executemany):
        if not statement.lstrip().upper().startswith("SELECT "):
            sql_effects.append("non_select")
    event.listen(engine, "before_cursor_execute", observe_sql)
    request.addfinalizer(lambda: event.remove(engine, "before_cursor_execute", observe_sql))
    closed, snapshots = [], []
    audit = RecoveryWriteAudit(runtime, monkeypatch)
    def close():
        if not closed:
            closed.append(True)
            rt.dependencies[dependency] = False
    original_snapshot = store._snapshot
    def snapshot(*args, **kwargs):
        snapshots.append(True)
        result = original_snapshot(*args, **kwargs)
        if branch.startswith("nested") and len(snapshots) == 1:
            rt.clock.advance(timedelta(microseconds=1 + offset_us))
        return result
    monkeypatch.setattr(store, "_snapshot", snapshot)
    original_get = client.get
    def get(key):
        value = original_get(key)
        if branch.startswith("nested") and key == keys.anchor and len(snapshots) == 2:
            close()
        return value
    monkeypatch.setattr(client, "get", get)
    if branch == "invalid_details":
        original_load = store._processing_load
        def load(*args, **kwargs):
            result = original_load(*args, **kwargs)
            close()
            return result
        monkeypatch.setattr(store, "_processing_load", load)
    target = {"prepared_abort": "abort_prepared", "claimed_exhaust": "exhaust_batch",
              "scheduled_exhaust": "exhaust_batch", "reserve_enqueue": "reserve_enqueue"}.get(branch)
    original_scan = client.scan_iter
    def scan(*args, **kwargs):
        result = original_scan(*args, **kwargs)
        frame = sys._getframe(1)
        while frame is not None and frame.f_code.co_name != "_atomic":
            frame = frame.f_back
        if target is not None and frame is not None and frame.f_locals["operation"] == target:
            close()
        return result
    monkeypatch.setattr(client, "scan_iter", scan)
    report = recovery_api().RecoveryService(runtime, max_pages=1).run_once(start + timedelta(days=skew_days))
    assert closed == ([] if branch.startswith("nested") and offset_us < 0 else [True])
    assert report.quarantined == report.aborted == report.exhausted == report.rescheduled == 0
    assert {key: client.values.get(key) for key in retained} == retained
    assert client.sets == indexes
    assert before == (len(rt.agent.calls), len(rt.processing_broker.calls), len(rt.outbound_broker.calls),
                      len(rt.transport.calls), sum(s.events.count("commit_entered") for s in rt.sessions))
    assert sql_effects == []
    assert audit.violations == []
    monkeypatch.setattr(store, scanner_name, scanner)
    monkeypatch.setattr(store, "_snapshot", original_snapshot)
    monkeypatch.setattr(client, "get", original_get)
    monkeypatch.setattr(client, "scan_iter", original_scan)
    if branch == "invalid_details":
        monkeypatch.setattr(store, "_processing_load", original_load)
    rt.dependencies[dependency] = True
    if branch.startswith("nested") and offset_us < 0:
        rt.clock.advance(timedelta(microseconds=1))
    # The known processing broker response allows only its post-effect record.
    rt.processing_broker.on_enqueue = audit.witness_processing
    resumed = recovery_api().RecoveryService(runtime, max_pages=2).run_once(rt.clock.now())
    expected = "quarantined" if branch.startswith("nested") or branch == "invalid_details" else (
        "aborted" if branch == "prepared_abort" else "rescheduled" if branch == "reserve_enqueue" else "exhausted")
    assert getattr(resumed, expected) == 1 and resumed.failed == 0
    writes_after = dict(client.operation_calls)
    again = recovery_api().RecoveryService(runtime, max_pages=1).run_once(rt.clock.now())
    assert again.quarantined == again.aborted == again.exhausted == again.rescheduled == again.failed == 0
    for operation in ("quarantine_mutation", "abort_prepared", "exhaust_batch", "reserve_enqueue"):
        assert client.operation_calls.get(operation, 0) == writes_after.get(operation, 0)
    assert audit.violations == []
    assert audit.opcodes <= {"SET", "DEL", "SADD", "SREM", "ACQUIRE", "PEXPIRE"}
    assert sql_effects == []


def observe_recovery_sql(rt, request):
    from sqlalchemy import event
    engine = rt._factory.kw["bind"]
    assert engine.url.database in (None, "", ":memory:")
    effects = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if not statement.lstrip().upper().startswith("SELECT "):
            effects.append("non_select")
    event.listen(engine, "before_cursor_execute", observe)
    request.addfinalizer(lambda: event.remove(engine, "before_cursor_execute", observe))
    return effects


@pytest.mark.parametrize("phase", ["SCHEDULED", "RESULT_READY"])
@pytest.mark.parametrize("composed", [False, True])
@pytest.mark.parametrize("dependency", list(domain.DependencyName))
@pytest.mark.parametrize("offset_us", [-1, 0, 1])
def test_recovery_ack_deadline_preserves_evidence(processing_runtime, monkeypatch, request,
                                                phase, composed, dependency, offset_us):
    from app.conversation_redis import contact_keys, RECOVERY_CHECKPOINT_KEY
    from tests.fakes import compose_recovery_fixture, RecoveryWriteAudit
    rt = processing_runtime
    command = rt.buffer()
    if phase == "RESULT_READY":
        with rt.store.contact_lease(command.phone) as lease:
            claim = rt.store.claim_or_resume_batch(command, rt.clock.now(), lease)
            rt.store.stage_agent_result(command, claim.attempt,
                domain.AgentResult("synthetic result", [], None, {}, domain.AgentIntent.SAVE_CONTEXT), rt.clock.now(), lease)
    runtime = compose_recovery_fixture(rt, monkeypatch) if composed else rt
    client, keys = rt.store.client, contact_keys(command.phone)
    batch = next(d for d in rt.details() if d.entry.kind == "batch")
    deadline = datetime.fromtimestamp(batch.body[
        "processing_deadline" if phase == "RESULT_READY" else "dispatch_deadline"], timezone.utc)
    rt.clock.set(deadline - timedelta(microseconds=2))
    before = (len(rt.processing_broker.calls), len(rt.agent.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    sql = observe_recovery_sql(rt, request)
    audit = RecoveryWriteAudit(runtime, monkeypatch)
    evidence = []
    def acknowledge(command):
        audit.witness_processing(command)
        rt.clock.set(deadline + timedelta(microseconds=offset_us))
        rt.dependencies[dependency] = False
        evidence.append(({key: value for key, value in client.values.items()
                          if key not in (keys.lease, RECOVERY_CHECKPOINT_KEY)}, deepcopy(client.sets)))
    rt.processing_broker.on_enqueue = acknowledge
    report = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    assert len(evidence) == 1
    values, indexes = evidence[0]
    assert report.quarantined == report.completed == report.exhausted == 0
    assert report.rescheduled == (1 if offset_us < 0 else 0)
    if offset_us >= 0:
        assert {key: client.values.get(key) for key in values} == values
        assert client.sets == indexes
    else:
        for key, value in values.items():
            if key.startswith((keys.buffer_prefix, keys.staging_prefix, keys.processing_prefix, keys.dedupe)):
                assert client.values[key] == value
    assert (len(rt.processing_broker.calls), len(rt.agent.calls), len(rt.outbound_broker.calls), len(rt.transport.calls)) == (
        before[0] + 1, *before[1:])
    assert sql == []
    assert audit.violations == []
    rt.dependencies[dependency] = True
    rt.clock.set(deadline + timedelta(microseconds=1))
    rt.processing_broker.on_enqueue = lambda _: pytest.fail("expired work reenqueued")
    resumed = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    assert (resumed.exhausted, resumed.completed, resumed.failed) == (1, 0, 0)
    writes = client.operation_calls.get("exhaust_batch", 0)
    again = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    assert again.exhausted == again.quarantined == again.completed == again.rescheduled == again.failed == 0
    assert client.operation_calls.get("exhaust_batch", 0) == writes
    assert sql == []
    assert audit.violations == []


@pytest.mark.parametrize("corruption", ["attempted_false", "different_identity"])
@pytest.mark.parametrize("boundary", ["claim", "transition", "atomic"])
def test_recovery_ack_identity_race_preserves_evidence(processing_runtime, task_api, monkeypatch, request,
                                                     corruption, boundary):
    from app.conversation_redis import contact_keys, RECOVERY_CHECKPOINT_KEY
    from tests.fakes import RecoveryWriteAudit
    rt = processing_runtime
    command = rt.buffer()
    rt.store.fail_next_atomic("complete_batch")
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    rt.clock.advance(timedelta(seconds=61))
    client, keys = rt.store.client, contact_keys(command.phone)
    audit = RecoveryWriteAudit(rt, monkeypatch)
    audit.witness_outbound(command)
    original = rt.store.claim_or_resume_batch
    transition = rt.store._transition
    evidence = []
    def corrupt_evidence():
        close_ready(rt)
        key = next(key for key in client.values if key.startswith(keys.processing_prefix))
        data = json.loads(client.values[key])
        data["body"]["outbound_attempted" if corruption == "attempted_false" else "outbound_attempt_id"] = (
            False if corruption == "attempted_false" else str(uuid4()))
        client.values[key] = json.dumps(data)
        evidence.append(({key: value for key, value in client.values.items()
                          if key not in (keys.lease, RECOVERY_CHECKPOINT_KEY)}, deepcopy(client.sets)))
    def change_identity(*args, **kwargs):
        corrupt_evidence()
        return original(*args, **kwargs)
    def change_before_transition(*args, **kwargs):
        if kwargs.get("operation") == "claim_or_resume_batch":
            corrupt_evidence()
        return transition(*args, **kwargs)
    if boundary == "claim":
        monkeypatch.setattr(rt.store, "claim_or_resume_batch", change_identity)
    elif boundary == "transition":
        monkeypatch.setattr(rt.store, "_transition", change_before_transition)
    else:
        client.before_operation["claim_or_resume_batch"] = corrupt_evidence
    before = (len(rt.processing_broker.calls), len(rt.agent.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    cas_before = client.operation_calls.get("claim_or_resume_batch", 0)
    sql = observe_recovery_sql(rt, request)
    report = recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert len(evidence) == 1
    # Atomic attempts still compare the witnessed raw receipt before writes;
    # earlier mismatches never even submit the claim CAS.
    assert client.operation_calls.get("claim_or_resume_batch", 0) == cas_before + (boundary == "atomic")
    values, indexes = evidence[0]
    assert {key: client.values.get(key) for key in values} == values
    assert client.sets == indexes
    assert report.completed == report.quarantined == report.rescheduled == report.exhausted == 0
    assert before == (len(rt.processing_broker.calls), len(rt.agent.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    assert sql == []
    assert audit.violations == []
    monkeypatch.setattr(rt.store, "claim_or_resume_batch", original)
    monkeypatch.setattr(rt.store, "_transition", transition)
    rt.dependencies[domain.DependencyName.SECRET] = True
    resumed = recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert (resumed.quarantined, resumed.failed, resumed.completed, resumed.rescheduled) == (1, 0, 0, 0)
    writes = dict(client.operation_calls)
    again = recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert again.quarantined == again.failed == again.completed == again.rescheduled == 0
    for operation in ("claim_or_resume_batch", "complete_batch", "reserve_enqueue", "validate"):
        assert client.operation_calls.get(operation, 0) == writes.get(operation, 0)
    assert before == (len(rt.processing_broker.calls), len(rt.agent.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    assert sql == []
    assert audit.violations == []


@pytest.mark.parametrize("boundary", ["claim", "transition"])
def test_recovery_ack_exact_reservation_survives_coherent_replacement(processing_runtime, task_api, monkeypatch, boundary):
    from app.conversation_redis import contact_keys, RECOVERY_CHECKPOINT_KEY
    rt = processing_runtime
    command = rt.buffer()
    rt.store.fail_next_atomic("complete_batch")
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    rt.clock.advance(timedelta(seconds=61))
    client, keys = rt.store.client, contact_keys(command.phone)
    method = "claim_or_resume_batch" if boundary == "claim" else "_transition"
    original = getattr(rt.store, method)
    evidence = []
    def replace_receipt(*args, **kwargs):
        if boundary == "claim" or kwargs.get("operation") == "claim_or_resume_batch":
            close_ready(rt)
            key = next(key for key in client.values if key.startswith(keys.processing_prefix))
            value = json.loads(client.values[key])
            replacement_id = str(uuid4())
            value["body"]["outbound_attempt_id"] = replacement_id
            value["body"]["outbound_reservation"]["reservation_id"] = replacement_id
            client.values[key] = json.dumps(value)
            evidence.append(({key: value for key, value in client.values.items()
                              if key not in (keys.lease, RECOVERY_CHECKPOINT_KEY)}, deepcopy(client.sets)))
        return original(*args, **kwargs)
    monkeypatch.setattr(rt.store, method, replace_receipt)
    before = client.operation_calls.get("claim_or_resume_batch", 0), len(rt.processing_broker.calls)
    report = recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert len(evidence) == 1
    values, indexes = evidence[0]
    assert {key: client.values.get(key) for key in values} == values and client.sets == indexes
    assert (client.operation_calls.get("claim_or_resume_batch", 0), len(rt.processing_broker.calls)) == before
    assert report.completed == report.quarantined == report.rescheduled == 0


@pytest.mark.parametrize("regression", ["deadline_destruction", "stale_identity"])
def test_recovery_ack_audit_rejects_real_regression_plans(processing_runtime, task_api, monkeypatch, regression):
    from app.conversation_redis import ATOMIC_SCRIPT, contact_keys
    from tests.fakes import RecoveryWriteAudit
    rt = processing_runtime
    command = rt.buffer()
    if regression == "stale_identity":
        rt.store.fail_next_atomic("complete_batch")
        with pytest.raises(task_api.RetryRequested):
            task_api.process_batch(command, rt)
    rt.clock.advance(timedelta(seconds=61))
    client, keys = rt.store.client, contact_keys(command.phone)
    audit = RecoveryWriteAudit(rt, monkeypatch)
    if regression == "stale_identity":
        audit.witness_outbound(command)
    else:
        def acknowledge(command):
            audit.witness_processing(command)
            close_ready(rt)
        rt.processing_broker.on_enqueue = acknowledge
    evaluate = client.eval
    injected = []
    def mutate(script, count, *args):
        if script == ATOMIC_SCRIPT and not injected:
            redis_keys, plan = args[:count], json.loads(args[count])
            target = "finish_enqueue" if regression == "deadline_destruction" else "claim_or_resume_batch"
            if plan["operation"] == target:
                injected.append(True)
                close_ready(rt)
                if regression == "deadline_destruction":
                    # Reintroduce the reviewed expiry write inside a genuine
                    # acknowledged plan, executing it through ScriptRedis.
                    key = keys.buffer_prefix + command.batch_id
                    plan["deadline_us"] = int(rt.clock.now().timestamp() * 1_000_000)
                    plan["deadline_writes"] = [*plan["writes"], {"op": "DEL", "key": redis_keys.index(key) + 1}]
                else:
                    # Remove the identity fence, not the observer: change the
                    # persisted evidence and let the stale plan accept it.
                    key = next(key for key in client.values if key.startswith(keys.processing_prefix))
                    value = json.loads(client.values[key])
                    value["body"]["outbound_attempted"] = False
                    client.values[key] = json.dumps(value)
                    for check in plan["checks"]:
                        if redis_keys[check["key"] - 1] == key:
                            check["value"] = client.values[key]
                args = (*redis_keys, json.dumps(plan))
        return evaluate(script, count, *args)
    monkeypatch.setattr(client, "eval", mutate)
    report = recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert injected == [True]
    if regression == "deadline_destruction":
        assert keys.buffer_prefix + command.batch_id not in client.values
        assert "finish_enqueue" in audit.violations
    else:
        assert report.completed == 1  # The injected regression really wrote.
        assert "claim_or_resume_batch" in audit.violations


def exercise_malformed_ack_recovery(rt, task_api, monkeypatch, request, boundary, composed, dependency, corruption):
    from app.conversation_redis import ATOMIC_SCRIPT, contact_keys, RECOVERY_CHECKPOINT_KEY
    from tests.fakes import compose_recovery_fixture, RecoveryWriteAudit
    command = rt.buffer()
    rt.store.fail_next_atomic("complete_batch")
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    rt.clock.advance(timedelta(seconds=61))
    runtime = compose_recovery_fixture(rt, monkeypatch) if composed else rt
    store, client, keys = runtime.store, rt.store.client, contact_keys(command.phone)
    audit = RecoveryWriteAudit(runtime, monkeypatch)
    audit.witness_outbound(command)
    sql = observe_recovery_sql(rt, request)
    effects = (len(rt.agent.calls), len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    evidence, closed_writes, quarantines = [], [], []
    evaluate = client.eval
    def observe_plan(script, count, *args):
        if script == ATOMIC_SCRIPT:
            plan = json.loads(args[count])
            if plan["quarantine"]:
                quarantines.append(plan["operation"])
            if (not rt.dependencies[dependency] and plan["operation"] not in ("acquire", "assert", "renew", "release")
                    and (plan["writes"] or plan["deadline_writes"] or plan["quarantine"])):
                closed_writes.append(plan["operation"])
        return evaluate(script, count, *args)
    monkeypatch.setattr(client, "eval", observe_plan)
    method = "claim_or_resume_batch" if boundary == "claim" else "complete_batch"
    original = getattr(store, method)
    def close_with_malformed_receipt(*args, **kwargs):
        rt.dependencies[dependency] = False
        key = next(key for key in client.values if key.startswith(keys.processing_prefix))
        record = json.loads(client.values[key])
        body, receipt = record["body"], record["body"]["outbound_reservation"]
        if corruption.startswith("both_"):
            malformed = {"both_null": None, "both_empty": "", "both_invalid": "not-a-uuid",
                         "both_number": 7, "both_bool": False, "both_list": [], "both_dict": {},
                         "both_uppercase": "00000000-0000-4000-8000-0000000000AB"}[corruption]
            body["outbound_attempt_id"] = receipt["reservation_id"] = malformed
        elif corruption == "missing_ids":
            body.pop("outbound_attempt_id")
            receipt.pop("reservation_id")
        elif corruption == "missing_reservation":
            body.pop("outbound_reservation")
        elif corruption == "missing_attempted":
            body.pop("outbound_attempted")
        elif corruption == "attempt_null":
            body["outbound_attempt_id"] = None
        elif corruption == "reservation_null":
            receipt["reservation_id"] = None
        elif corruption == "asymmetric_equal_types":
            body["outbound_attempt_id"], receipt["reservation_id"] = 0, False
        elif corruption == "wrong_reservation_type":
            body["outbound_reservation"] = []
        elif corruption == "missing_typed_field":
            receipt.pop("claim_token")
        elif corruption == "invalid_typed_field":
            receipt["result_fingerprint"] = None
        elif corruption == "extra_typed_field":
            receipt["unexpected"] = "synthetic"
        else:
            pytest.fail("unsupported synthetic corruption")
        client.values[key] = json.dumps(record)
        evidence.append(({key: value for key, value in client.values.items()
                          if key not in (keys.lease, RECOVERY_CHECKPOINT_KEY)}, deepcopy(client.sets)))
        return original(*args, **kwargs)
    monkeypatch.setattr(store, method, close_with_malformed_receipt)
    closed = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    assert len(evidence) == 1
    values, indexes = evidence[0]
    assert {key: value for key, value in client.values.items()
            if key not in (keys.lease, RECOVERY_CHECKPOINT_KEY)} == values
    assert client.sets == indexes
    assert closed.completed == closed.quarantined == closed.rescheduled == closed.exhausted == 0
    assert closed_writes == quarantines == sql == audit.violations == []
    assert effects == (len(rt.agent.calls), len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    monkeypatch.setattr(store, method, original)
    rt.dependencies[dependency] = True
    resumed = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    writes = dict(client.operation_calls)
    again = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    # Collect both ready passes before asserting: the reviewed defect reported
    # failed twice, never making the canonical quarantine transition.
    assert (resumed.quarantined, resumed.failed, again.quarantined, again.failed) == (1, 0, 0, 0)
    assert resumed.completed == resumed.rescheduled == resumed.exhausted == 0
    assert again.completed == again.rescheduled == again.exhausted == 0
    assert quarantines == ["validate"]
    for operation in ("claim_or_resume_batch", "complete_batch", "reserve_enqueue", "validate"):
        assert client.operation_calls.get(operation, 0) == writes.get(operation, 0)
    assert effects == (len(rt.agent.calls), len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    assert sql == closed_writes == audit.violations == []


@pytest.mark.parametrize("boundary", ["claim", "completion"])
@pytest.mark.parametrize("composed", [False, True])
@pytest.mark.parametrize("dependency", list(domain.DependencyName))
@pytest.mark.parametrize("corruption", ["both_null", "both_empty", "both_invalid"])
def test_recovery_malformed_ack_receipts_converge(processing_runtime, task_api, monkeypatch, request,
                                                boundary, composed, dependency, corruption):
    exercise_malformed_ack_recovery(processing_runtime, task_api, monkeypatch, request,
                                   boundary, composed, dependency, corruption)


@pytest.mark.parametrize("boundary", ["claim", "completion"])
@pytest.mark.parametrize("composed", [False, True])
@pytest.mark.parametrize("corruption", ["both_number", "both_bool", "both_list", "both_dict", "both_uppercase",
    "missing_ids", "missing_reservation", "missing_attempted", "attempt_null", "reservation_null",
    "asymmetric_equal_types", "wrong_reservation_type", "missing_typed_field", "invalid_typed_field", "extra_typed_field"])
def test_recovery_malformed_ack_type_and_asymmetry_controls(processing_runtime, task_api, monkeypatch, request,
                                                          boundary, composed, corruption):
    exercise_malformed_ack_recovery(processing_runtime, task_api, monkeypatch, request,
                                   boundary, composed, domain.DependencyName.SECRET, corruption)


@pytest.mark.parametrize("boundary", ["snapshot", "validation", "claim", "completion", "claim_transition",
                                      "completion_transition", "claim_atomic", "completion_atomic"])
@pytest.mark.parametrize("composed", [False, True])
@pytest.mark.parametrize("dependency", list(domain.DependencyName))
def test_recovery_present_reservation_without_ack_is_validated(processing_runtime, task_api, monkeypatch, request,
                                                              boundary, composed, dependency):
    """Guard the present-reservation branch independently of ACK marker keys."""
    from app.conversation_redis import ATOMIC_SCRIPT, contact_keys
    from tests.fakes import compose_recovery_fixture, RecoveryWriteAudit
    rt = processing_runtime
    command = rt.buffer()
    rt.store.fail_next_atomic("complete_batch")
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    rt.clock.advance(timedelta(seconds=61))
    runtime = compose_recovery_fixture(rt, monkeypatch) if composed else rt
    store, client, keys = runtime.store, rt.store.client, contact_keys(command.phone)
    audit = RecoveryWriteAudit(runtime, monkeypatch)
    audit.witness_outbound(command)
    sql = observe_recovery_sql(rt, request)
    effects = (len(rt.agent.calls), len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    evidence, closed_writes, quarantines = [], [], []
    def remove_markers():
        if evidence:
            return
        rt.dependencies[dependency] = False
        key = next(key for key in client.values if key.startswith(keys.processing_prefix))
        record = json.loads(client.values[key])
        record["body"].pop("outbound_attempted")
        record["body"].pop("outbound_attempt_id")
        record["body"]["outbound_reservation"]["reservation_id"] = "not-a-uuid"
        client.values[key] = json.dumps(record)
        evidence.append(({key: value for key, value in client.values.items() if key != keys.lease}, deepcopy(client.sets)))
    evaluate = client.eval
    def observe(script, count, *args):
        result = evaluate(script, count, *args)
        if script == ATOMIC_SCRIPT:
            plan = json.loads(args[count])
            if plan["quarantine"] and result == "generation":
                quarantines.append(plan["operation"])
            # A late atomic hook can still submit a stale-fenced attempt; it
            # must never apply any write after the raw evidence has changed.
            if (not rt.dependencies[dependency] and result in ("ok", "pending", "generation")
                    and plan["operation"] not in ("acquire", "assert", "renew", "release")
                    and (plan["writes"] or plan["deadline_writes"] or plan["quarantine"])):
                closed_writes.append(plan["operation"])
        return result
    monkeypatch.setattr(client, "eval", observe)
    method = {"snapshot": "_snapshot", "validation": "_validate_batch_details",
              "claim": "claim_or_resume_batch", "completion": "complete_batch"}.get(boundary, "_transition")
    target = "complete_batch" if boundary.startswith("completion") else "claim_or_resume_batch"
    original = getattr(store, method)
    def intercept(*args, **kwargs):
        if not boundary.endswith("transition") or kwargs.get("operation") == target:
            remove_markers()
        return original(*args, **kwargs)
    if boundary.endswith("atomic"):
        client.before_operation[target] = remove_markers
    else:
        monkeypatch.setattr(store, method, intercept)
    closed = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    assert len(evidence) == 1
    values, indexes = evidence[0]
    assert {key: value for key, value in client.values.items() if key != keys.lease} == values
    assert client.sets == indexes
    assert closed.completed == closed.rescheduled == closed.quarantined == closed.exhausted == 0
    assert closed_writes == quarantines == sql == audit.violations == []
    assert effects == (len(rt.agent.calls), len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    monkeypatch.setattr(store, method, original)
    rt.dependencies[dependency] = True
    resumed = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    first_values, first_indexes = dict(client.values), deepcopy(client.sets)
    writes = dict(client.operation_calls)
    again = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    assert (resumed.quarantined, resumed.failed, again.quarantined, again.failed) == (1, 0, 0, 0)
    assert resumed.rescheduled == resumed.completed == resumed.exhausted == 0
    assert again.rescheduled == again.completed == again.exhausted == 0
    assert quarantines == ["validate"]
    assert client.values == first_values and client.sets == first_indexes
    for operation in ("claim_or_resume_batch", "complete_batch", "reserve_enqueue", "validate"):
        assert client.operation_calls.get(operation, 0) == writes.get(operation, 0)
    assert effects == (len(rt.agent.calls), len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    assert sql == closed_writes == audit.violations == []


@pytest.mark.parametrize("composed", [False, True])
@pytest.mark.parametrize("dependency", list(domain.DependencyName))
def test_recovery_valid_reservation_without_ack_reschedules_once(processing_runtime, task_api, monkeypatch, request,
                                                               composed, dependency):
    from tests.fakes import compose_recovery_fixture, RecoveryWriteAudit
    rt = processing_runtime
    command = rt.buffer()
    rt.store.fail_next_atomic("record_outbound_attempt")
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    rt.clock.advance(timedelta(seconds=61))
    processing = next(detail for detail in rt.details() if detail.entry.kind == "processing")
    assert "outbound_reservation" in processing.body
    assert "outbound_attempted" not in processing.body and "outbound_attempt_id" not in processing.body
    runtime = compose_recovery_fixture(rt, monkeypatch) if composed else rt
    client = rt.store.client
    audit = RecoveryWriteAudit(runtime, monkeypatch)
    rt.processing_broker.on_enqueue = audit.witness_processing
    sql = observe_recovery_sql(rt, request)
    effects = (len(rt.agent.calls), len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    rt.dependencies[dependency] = False
    values, indexes = dict(client.values), deepcopy(client.sets)
    assert recovery_api().RecoveryService(runtime).run_once(rt.clock.now()) == domain.RecoveryReport()
    assert client.values == values and client.sets == indexes
    assert effects == (len(rt.agent.calls), len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.transport.calls))
    rt.dependencies[dependency] = True
    resumed = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    assert (resumed.rescheduled, resumed.quarantined, resumed.failed, resumed.completed) == (1, 0, 0, 0)
    after = len(rt.processing_broker.calls)
    assert after == effects[1] + 1  # Only the legitimate processing reschedule.
    again = recovery_api().RecoveryService(runtime).run_once(rt.clock.now())
    assert again.rescheduled == again.quarantined == again.failed == again.completed == 0
    assert (len(rt.agent.calls), len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.transport.calls)) == (
        effects[0], after, effects[2], effects[3])
    assert sql == audit.violations == []


@pytest.mark.parametrize("ack_path", ["processing_broker", "persisted_outbound"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_recovery_ack_records_only_identity_bound_effect_without_destructive_repair(processing_runtime, task_api,
                                                                                 monkeypatch, ack_path, corrupt):
    from app.conversation_redis import contact_keys, RECOVERY_CHECKPOINT_KEY
    from tests.fakes import RecoveryWriteAudit
    rt = processing_runtime
    command = rt.buffer()
    rt.store.fail_next_atomic("reserve_outbound_enqueue" if ack_path == "processing_broker" else "complete_batch")
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    rt.clock.advance(timedelta(seconds=61))
    client, keys = rt.store.client, contact_keys(command.phone)
    before = len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.agent.calls), len(rt.transport.calls)
    audit = RecoveryWriteAudit(rt, monkeypatch)
    if ack_path == "persisted_outbound":
        audit.witness_outbound(command)
    evidence = []
    def close_and_capture():
        if evidence:
            return
        close_ready(rt)
        if corrupt:
            key = next(key for key in client.values if key.startswith(keys.processing_prefix))
            data = json.loads(client.values[key])
            data["body"]["claim_token"] = ""
            client.values[key] = json.dumps(data)
        evidence.append(({key: value for key, value in client.values.items()
                          if key not in (keys.lease, RECOVERY_CHECKPOINT_KEY)}, deepcopy(client.sets)))
    if ack_path == "processing_broker":
        def broker_ack(command):
            audit.witness_processing(command)
            close_and_capture()
        rt.processing_broker.on_enqueue = broker_ack
    else:
        original_scan = client.scan_iter
        def scan(*args, **kwargs):
            result = original_scan(*args, **kwargs)
            frame = sys._getframe(1)
            while frame is not None and frame.f_code.co_name != "_atomic":
                frame = frame.f_back
            if frame is not None and frame.f_locals["operation"] == "claim_or_resume_batch":
                close_and_capture()
            return result
        monkeypatch.setattr(client, "scan_iter", scan)
    report = recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert len(evidence) == 1
    assert report.quarantined == 0
    if corrupt:
        values, indexes = evidence[0]
        assert {key: client.values.get(key) for key in values} == values
        assert client.sets == indexes
        assert report.completed == report.rescheduled == 0
    else:
        assert (report.rescheduled, report.completed) == ((1, 0) if ack_path == "processing_broker" else (0, 1))
    assert (len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.agent.calls), len(rt.transport.calls)) == (
        before[0] + (ack_path == "processing_broker"), *before[1:])
    assert audit.violations == []
    rt.dependencies[domain.DependencyName.SECRET] = True
    if corrupt and ack_path == "processing_broker":
        rt.clock.advance(timedelta(seconds=61))  # Respect the existing visibility reservation.
    resumed = recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert (resumed.quarantined, resumed.failed) == ((1, 0) if corrupt else (0, 0))
    writes_after = dict(client.operation_calls)
    again = recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert again.quarantined == again.failed == again.rescheduled == again.completed == 0
    for operation in ("finish_enqueue", "claim_or_resume_batch", "complete_batch", "quarantine_mutation"):
        assert client.operation_calls.get(operation, 0) == writes_after.get(operation, 0)
    assert audit.violations == []


@pytest.mark.parametrize("operation", ["quarantine_mutation", "abort_prepared", "exhaust_batch", "reserve_enqueue",
                                      "validate", "checkpoint"])
def test_recovery_opcode_audit_detects_removed_adjacent_guard(processing_runtime, task_api, monkeypatch, operation):
    from tests.fakes import RecoveryWriteAudit
    from app.conversation_redis import contact_keys
    rt = processing_runtime
    command = rt.buffer()
    if operation in ("quarantine_mutation", "abort_prepared"):
        rt.store.fail_next_atomic("finalize_committed" if operation == "quarantine_mutation" else "enter_committing")
        with pytest.raises(task_api.RetryRequested):
            task_api.process_batch(command, rt)
    elif operation == "validate":
        with rt.store.contact_lease(command.phone) as lease:
            rt.store.claim_or_resume_batch(command, rt.clock.now(), lease)
        prefix = contact_keys(command.phone).processing_prefix
        key = next(key for key in rt.store.client.values if key.startswith(prefix))
        detail = json.loads(rt.store.client.values[key])
        detail["body"]["claim_token"] = ""
        rt.store.client.values[key] = json.dumps(detail)
    rt.clock.advance(timedelta(seconds=61 if operation == "reserve_enqueue" else 901))
    audit = RecoveryWriteAudit(rt, monkeypatch)
    rt.processing_broker.on_enqueue = audit.witness_processing
    original = rt.store._atomic
    def atomic(phone, name, *args, **kwargs):
        if name == operation:
            kwargs["require_ready"] = None  # Deliberate synthetic regression.
        return original(phone, name, *args, **kwargs)
    monkeypatch.setattr(rt.store, "_atomic", atomic)
    if operation == "checkpoint":
        original_checkpoint = rt.store.save_recovery_checkpoint
        def checkpoint(*args, **kwargs):
            kwargs["require_ready"] = None
            return original_checkpoint(*args, **kwargs)
        monkeypatch.setattr(rt.store, "save_recovery_checkpoint", checkpoint)
    recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert operation in audit.observed
    assert operation in audit.violations


def test_recovery_operational_trim_is_guarded_and_idempotent(processing_runtime, monkeypatch):
    from tests.fakes import RecoveryWriteAudit
    from app.conversation_redis import QUARANTINE_INDEX_KEY, RECOVERY_CHECKPOINT_KEY, contact_digest, contact_keys
    rt = processing_runtime
    phone = "5551999990000"
    with rt.store.contact_lease(phone) as lease, rt.session_factory() as db:
        rt.coordinator.pause_for_secretary(db, phone, "secretary_manual_pause", rt.clock.now(), lease, "old-receipt")
        def ambiguous():
            raise RuntimeError("synthetic ambiguous commit")
        db.hooks["commit_entered"] = ambiguous
        with pytest.raises(domain.ConversationMutationAmbiguous):
            rt.coordinator.unpause(db, phone, rt.clock.now(), lease, "uncertain-operation")
    rt.clock.advance(timedelta(days=8))
    client, keys = rt.store.client, contact_keys(phone)
    # A stale discovery plus missing secondary marker exercises the operational
    # fallback; the durable primary fence still proves quarantine, not a commit.
    client.sets[QUARANTINE_INDEX_KEY].discard(contact_digest(phone))
    page = domain.RecoveryPage((), None, ((phone, "uncertain-operation"),), 1, 0)
    monkeypatch.setattr(rt.store, "recoverable_mutations", lambda *args, **kwargs: page)
    audit = RecoveryWriteAudit(rt, monkeypatch)
    original_scan = client.scan_iter
    closed = []
    def scan(*args, **kwargs):
        result = original_scan(*args, **kwargs)
        frame = sys._getframe(1)
        while frame is not None and frame.f_code.co_name != "_atomic":
            frame = frame.f_back
        if not closed and frame is not None and frame.f_locals["operation"] == "trim_quarantine_receipts":
            closed.append(True)
            close_ready(rt)
        return result
    monkeypatch.setattr(client, "scan_iter", scan)
    retained = {key: value for key, value in client.values.items() if key not in (keys.lease, RECOVERY_CHECKPOINT_KEY)}
    indexes = deepcopy(client.sets)
    report = recovery_api().RecoveryService(rt, max_pages=1).run_once(rt.clock.now())
    assert closed == [True] and report.quarantined == 0
    assert client.operation_calls.get("trim_quarantine_receipts", 0) == 0
    assert {key: client.values.get(key) for key in retained} == retained
    assert client.sets == indexes and audit.violations == []
    rt.dependencies[domain.DependencyName.SECRET] = True
    recovery_api().RecoveryService(rt, max_pages=1).run_once(rt.clock.now())
    assert client.operation_calls.get("trim_quarantine_receipts", 0) == 1
    recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert client.operation_calls.get("trim_quarantine_receipts", 0) == 1
    assert "trim_quarantine_receipts" in audit.observed and audit.violations == []
    assert rt.agent.calls == rt.processing_broker.calls == rt.outbound_broker.calls == rt.transport.calls == []


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_recovery_stale_discovery_preserves_fresh_committing_until_actual_deadline(processing_runtime, task_api, monkeypatch, offset):
    rt = processing_runtime
    command = rt.buffer()
    rt.clock.advance(timedelta(seconds=61))
    original = rt.store.recoverable_batches
    invoked = []
    def discover(*args, **kwargs):
        page = original(*args, **kwargs)
        if not invoked:
            invoked.append(True)
            rt.store.fail_next_atomic("finalize_committed")
            with pytest.raises(task_api.RetryRequested):
                task_api.process_batch(command, rt)
            rt.clock.advance(timedelta(seconds=600 + offset))
        return page
    monkeypatch.setattr(rt.store, "recoverable_batches", discover)
    report = recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert report.failed == 0
    assert report.quarantined == (0 if offset < 0 else 1)
    assert bool(rt.store.is_quarantined(command.phone)) is (offset >= 0)
    assert len(rt.agent.calls) == 1
    assert rt.transport.calls == rt.outbound_broker.calls == []
    assert len(rt.processing_broker.calls) == 1
    if offset < 0:
        assert rt.envelopes()
    again = recovery_api().RecoveryService(rt).run_once(rt.clock.now())
    assert again.quarantined == again.failed == again.rescheduled == 0


@pytest.mark.parametrize("start_result", [False, True, "exception"])
def test_ready_scheduler_shutdown_only_after_affirmative_start(main_module, admin_runtime, monkeypatch, start_result):
    import asyncio
    calls = []
    monkeypatch.setattr(main_module, "init_db", lambda: None)
    def start(runtime):
        calls.append("start")
        if start_result == "exception":
            raise RuntimeError("private-token")
        return start_result
    monkeypatch.setattr(main_module, "start_scheduler", start)
    monkeypatch.setattr(main_module, "stop_scheduler", lambda: calls.append("stop"))
    async def run():
        async with main_module.lifespan(main_module.app):
            assert (await main_module.health_check())["status"] == "healthy"
    asyncio.run(run())
    assert calls == (["start", "stop"] if start_result is True else ["start"])


@pytest.mark.parametrize("ready", [False, True])
def test_ready_lifespan_preserves_schema_initialization_only_after_gate(main_module, admin_runtime, monkeypatch, ready):
    import asyncio
    main_module.app.state.conversation_runtime = admin_runtime
    admin_runtime.dependencies[domain.DependencyName.SQL] = ready
    started, stopped, initialized = [], [], []
    monkeypatch.setattr(main_module, "init_db", lambda: initialized.append(True))
    monkeypatch.setattr(main_module, "start_scheduler", lambda rt=None: started.append(rt) or True)
    monkeypatch.setattr(main_module, "stop_scheduler", lambda: stopped.append(True))
    async def run():
        async with main_module.lifespan(main_module.app):
            assert main_module.app.state.conversation_runtime is admin_runtime
    asyncio.run(run())
    assert initialized == ([True] if ready else [])
    assert started == ([admin_runtime] if ready else [])
    assert stopped == ([True] if ready else [])


def test_ready_worker_and_recovery_share_one_composed_runtime(main_module, admin_runtime, monkeypatch):
    api = recovery_api()
    monkeypatch.delattr(main_module.app.state, "conversation_runtime")
    calls = []
    def build(**kwargs):
        calls.append(kwargs)
        return admin_runtime
    monkeypatch.setattr(api, "build_runtime", build)
    assert hasattr(main_module, "get_conversation_runtime"), "worker composition missing"
    assert main_module.get_conversation_runtime() is admin_runtime
    assert main_module.get_conversation_runtime() is admin_runtime
    assert len(calls) == 1
    assert calls[0]["processing_task"] is main_module.process_message_task
    assert calls[0]["outbound_task"] is main_module.send_message_task
    assert hasattr(main_module, "recover_conversations_task"), "beat recovery body missing"
    assert main_module.recover_conversations_task() == {
        "scanned": 0, "rescheduled": 0, "completed": 0, "aborted": 0,
        "quarantined": 0, "exhausted": 0, "skipped": 0, "failed": 0}


@pytest.mark.parametrize("dependency", list(domain.DependencyName))
@pytest.mark.parametrize("_case", [None], ids=["flow-69"])
def test_ready_closed_gates_all_phase_one_effects(_case, main_module, admin_runtime, scheduler_module, task_api, monkeypatch, dependency):
    import asyncio
    api = recovery_api()
    rt = admin_runtime
    command = rt.buffer()
    task_api.process_batch(command, rt)
    outbound = rt.outbound_broker.calls[-1]
    rt.dependencies[dependency] = False
    probes = {name: (lambda name=name: rt.dependencies[name]) for name in domain.DependencyName}
    rt.readiness_status = api.DependencyReadiness(probes).check
    before = (rt.store.snapshot(), rt.lease_calls, rt.session_calls,
              len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.agent.calls))
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    with pytest.raises(task_api.RetryRequested):
        task_api.send_outbound(outbound, rt)
    response = webhook(main_module)
    assert response.status_code == 503
    asyncio.run(scheduler_module.check_inactive_contexts(rt))
    assert api.RecoveryService(rt).run_once(rt.clock.now()) == domain.RecoveryReport()
    assert before == (rt.store.snapshot(), rt.lease_calls, rt.session_calls,
              len(rt.processing_broker.calls), len(rt.outbound_broker.calls), len(rt.agent.calls))
    assert rt.transport.calls == []


def test_ready_composition_uses_bounded_sql_and_only_select_one(session_factory):
    from sqlalchemy import event
    api = recovery_api()
    with session_factory() as db:
        engine = db.bind
        assert engine.url.database in (None, "", ":memory:")
    queries, options = [], []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, many: queries.append(statement))
    _, probe = api.bounded_sql_dependencies("postgresql://synthetic.invalid/synthetic",
        engine_factory=lambda url, **kwargs: options.append(kwargs) or engine)
    assert probe() is True
    assert queries == ["SELECT 1"]
    assert options[0]["connect_args"]["connect_timeout"] == 2
    assert "statement_timeout=2000" in options[0]["connect_args"]["options"]
    assert options[0]["pool_timeout"] == 2


def test_ready_broker_probe_is_bounded_and_never_publishes(main_module, monkeypatch):
    from pathlib import Path
    import celery
    class FakeCelery:
        def __init__(self, *args, **kwargs):
            self.conf = {}
    monkeypatch.setattr(celery, "Celery", FakeCelery)
    spec = importlib.util.spec_from_file_location("synthetic_celery_probe", Path(__file__).parents[1] / "app" / "celery_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "probe_broker"), "bounded broker probe missing"
    from contextlib import contextmanager
    class Connection:
        connected = True
        def ensure_connection(self, **kwargs):
            assert kwargs == {"max_retries": 0}
    @contextmanager
    def connection_for_read(**kwargs):
        assert kwargs["connect_timeout"] == 2
        assert kwargs["transport_options"]["socket_timeout"] == 2
        yield Connection()
    assert module.probe_broker(SimpleNamespace(connection_for_read=connection_for_read)) is True
    schedule = module.celery_app.conf.get("beat_schedule", {})
    assert any(entry["task"] == "app.main.recover_conversations_task" for entry in schedule.values())


def test_recovery_healthy_claim_is_not_listed_and_busy_contact_does_not_block_next(processing_runtime, monkeypatch):
    from contextlib import contextmanager
    api = recovery_api()
    rt = processing_runtime
    healthy = rt.buffer(phone="5551999990090")
    stale = rt.buffer(phone="5551999990091")
    other = rt.buffer(phone="5551999990092")
    rt.clock.advance(timedelta(seconds=61))
    with rt.store.contact_lease(healthy.phone) as lease:
        rt.store.claim_or_resume_batch(healthy, rt.clock.now(), lease)
    page = rt.store.recoverable_batches(now=rt.clock.now())
    assert healthy.phone not in [command.phone for command in page.commands]
    original = rt.store.contact_lease
    @contextmanager
    def contact_lease(phone):
        if phone == stale.phone:
            raise domain.ContactLockUnavailable(domain.FailureReason.CONTACT_LOCK_UNAVAILABLE)
        with original(phone) as lease:
            yield lease
    monkeypatch.setattr(rt.store, "contact_lease", contact_lease)
    before = len(rt.processing_broker.calls)
    report = api.RecoveryService(rt).run_once(rt.clock.now())
    assert report.failed == 1 and report.rescheduled == 1
    assert [c.phone for c in rt.processing_broker.calls[before:]] == [other.phone]


def test_recovery_committed_without_ack_only_republishes_internal_work(task_api, processing_runtime):
    api = recovery_api()
    rt = processing_runtime
    command = rt.buffer()
    rt.outbound_broker.next_result = domain.EnqueueResult.AMBIGUOUS
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    before = (len(rt.agent.calls), len(rt.outbound_broker.calls))
    rt.clock.advance(timedelta(seconds=601))
    assert api.RecoveryService(rt).run_once(rt.clock.now()).rescheduled == 1
    assert before == (len(rt.agent.calls), len(rt.outbound_broker.calls))
    assert rt.transport.calls == []


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("_case", [None], ids=["flow-75"])
def test_recovery_reschedules_stale_dispatch_without_agent_or_transport(_case, processing_runtime, staged):
    api = recovery_api()
    rt = processing_runtime
    command = rt.buffer()
    if staged:
        with rt.store.contact_lease(command.phone) as lease:
            claim = rt.store.claim_or_resume_batch(command, rt.clock.now(), lease)
    rt.clock.advance(timedelta(seconds=61))
    before = len(rt.processing_broker.calls)
    service = api.RecoveryService(rt)
    report = service.run_once(rt.clock.now())
    assert report.rescheduled == 1
    assert len(rt.processing_broker.calls) == before + 1
    recovered = rt.processing_broker.calls[-1]
    assert recovered.batch_id == command.batch_id
    if staged:
        assert recovered.processing_id == claim.attempt.processing_id
        assert recovered.staging_id == command.batch_id
    assert service.run_once(rt.clock.now()).rescheduled == 0
    assert rt.agent.calls == rt.outbound_broker.calls == rt.transport.calls == []


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("_case", [None], ids=["flow-70"])
def test_recovery_exhausts_only_at_configured_horizon_and_purges_content(_case, processing_runtime, staged):
    api = recovery_api()
    rt = processing_runtime
    command = rt.buffer()
    if staged:
        with rt.store.contact_lease(command.phone) as lease:
            rt.store.claim_or_resume_batch(command, rt.clock.now(), lease)
    horizon = rt.store.config.processing_retry_seconds if staged else rt.store.config.dispatch_retry_seconds
    rt.clock.advance(timedelta(seconds=horizon))
    before = len(rt.processing_broker.calls)
    service = api.RecoveryService(rt)
    assert service.run_once(rt.clock.now()).exhausted == 1
    assert service.run_once(rt.clock.now()).exhausted == 0
    assert rt.envelopes() == []
    assert len(rt.processing_broker.calls) == before
    assert rt.agent.calls == rt.transport.calls == []


@pytest.mark.parametrize("boundary", ["complete_batch", "finalize_committed"])
def test_recovery_applied_attempt_uses_receipts_and_never_repeats_sql_or_outbound(task_api, processing_runtime, boundary):
    api = recovery_api()
    rt = processing_runtime
    command = rt.buffer()
    rt.store.fail_next_atomic(boundary)
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    before_agent, before_outbound = len(rt.agent.calls), len(rt.outbound_broker.calls)
    before_commits = sum(s.events.count("commit_entered") for s in rt.sessions)
    rt.clock.advance(timedelta(seconds=601))
    service = api.RecoveryService(rt)
    report = service.run_once(rt.clock.now())
    if boundary == "finalize_committed":
        assert report.quarantined == 1
        assert rt.store.is_quarantined(command.phone)
    else:
        assert report.completed == 1
        assert any(d.body.get("phase") == "PROCESSED" for d in rt.details() if d.entry.kind == "batch")
    assert len(rt.agent.calls) == before_agent
    assert len(rt.outbound_broker.calls) == before_outbound
    assert sum(s.events.count("commit_entered") for s in rt.sessions) == before_commits
    assert service.run_once(rt.clock.now()).completed == 0
    assert rt.transport.calls == []


def test_recovery_standalone_prepared_is_aborted_and_committing_is_quarantined(processing_runtime):
    api = recovery_api()
    rt = processing_runtime
    phones = ("5551999990000", "5551999990001")
    operations = []
    for phone in phones:
        with rt.store.contact_lease(phone) as lease, rt.session_factory() as db:
            rt.coordinator.resolve_ingress(db, phone, rt.clock.now(), lease)
            operation = str(uuid4())
            target = domain.MutationTarget("PAUSE_MANUAL", "synthetic", "synthetic", domain.ConversationCycle.PAUSED,
                paused_until=rt.clock.now() + timedelta(hours=2), reason=domain.PauseReason.DASHBOARD.value)
            attempt = rt.store.prepare_mutation(phone, target.kind, target.fingerprint, lease, operation, rt.clock.now(), target=target)
            if phone == phones[1]:
                rt.store.enter_committing(phone, operation, lease, rt.clock.now(), attempt.processing_deadline)
            operations.append(operation)
    rt.clock.advance(timedelta(seconds=601))
    service = api.RecoveryService(rt)
    report = service.run_once(rt.clock.now())
    assert (report.aborted, report.quarantined) == (1, 1)
    with rt.store.contact_lease(phones[0]) as lease:
        assert rt.store.inspect_mutation(phones[0], operations[0], lease).phase is domain.MutationPhase.ABORTED
    assert rt.store.is_quarantined(phones[1])
    assert service.run_once(rt.clock.now()).aborted == 0
    assert rt.agent.calls == rt.processing_broker.calls == rt.outbound_broker.calls == rt.transport.calls == []


def test_recovery_test_phone_keeps_request_local_capture_and_done_is_never_replayed(task_api, processing_runtime):
    api = recovery_api()
    rt = processing_runtime
    command = rt.buffer(phone=domain.ConversationCoordinator.TEST_PHONE)
    rt.clock.advance(timedelta(seconds=61))
    before = list(rt.processing_broker.calls)
    assert api.RecoveryService(rt).run_once(rt.clock.now()).rescheduled == 1
    assert rt.processing_broker.calls == before
    assert rt.agent.calls == rt.transport.calls == rt.outbound_broker.calls == []
    task_api.process_batch(command, rt)
    before = len(rt.processing_broker.calls), len(rt.agent.calls), len(rt.outbound_broker.calls)
    rt.clock.advance(timedelta(seconds=1000))
    assert api.RecoveryService(rt).run_once(rt.clock.now()).completed == 0
    assert before == (len(rt.processing_broker.calls), len(rt.agent.calls), len(rt.outbound_broker.calls))


def test_recovery_cursor_progresses_past_healthy_and_busy_contact(processing_runtime):
    from contextlib import contextmanager
    api = recovery_api()
    rt = processing_runtime
    commands = [rt.buffer(phone=f"55519999900{i:02d}") for i in range(6)]
    rt.clock.advance(timedelta(seconds=61))
    rt.store.client.sscan_chunk_limit = 2
    service = api.RecoveryService(rt, page_size=1, max_pages=1)
    before = len(rt.processing_broker.calls)
    for _ in range(30):
        report = service.run_once(rt.clock.now())
        assert report.scanned <= 1
    assert len(rt.processing_broker.calls) == before + len(commands)
    assert rt.agent.calls == rt.transport.calls == []


def test_recovery_checkpoint_shared_across_fresh_services_prevents_first_page_starvation(processing_runtime):
    api = recovery_api()
    rt = processing_runtime
    for i in range(7):
        rt.buffer(phone=f"55519999900{i:02d}")
    rt.clock.advance(timedelta(seconds=61))
    before = len(rt.processing_broker.calls)
    for _ in range(40):
        api.RecoveryService(rt, page_size=1, max_pages=1).run_once(rt.clock.now())
    assert len(rt.processing_broker.calls) == before + 7


def test_recovery_poisoned_index_does_not_starve_valid_contact(processing_runtime):
    from app.conversation_redis import DISPATCH_INDEX_KEY
    api = recovery_api()
    rt = processing_runtime
    rt.buffer()
    rt.clock.advance(timedelta(seconds=61))
    rt.store.client.sets[DISPATCH_INDEX_KEY].add("000:private-token")
    before = len(rt.processing_broker.calls)
    reports = [api.RecoveryService(rt, page_size=1, max_pages=1).run_once(rt.clock.now()) for _ in range(10)]
    assert len(rt.processing_broker.calls) == before + 1
    assert any(report.failed for report in reports)
    assert "private-token" not in repr(reports)


def test_recovery_future_scan_time_cannot_abort_a_live_preparation(processing_runtime):
    rt = processing_runtime
    phone = "5551999990000"
    with rt.store.contact_lease(phone) as lease, rt.session_factory() as db:
        rt.coordinator.resolve_ingress(db, phone, rt.clock.now(), lease)
        operation = str(uuid4())
        target = domain.MutationTarget("PAUSE_MANUAL", "synthetic", "synthetic", domain.ConversationCycle.PAUSED)
        rt.store.prepare_mutation(phone, target.kind, target.fingerprint, lease, operation, rt.clock.now(), target=target)
        assert rt.store.recover_mutation(phone, operation, rt.clock.now() + timedelta(days=1), lease) == "skipped"
        assert rt.store.inspect_mutation(phone, operation, lease).phase is domain.MutationPhase.PREPARED


def test_recovery_gate_closes_after_discovery_before_lease_or_sql(processing_runtime, monkeypatch):
    api = recovery_api()
    rt = processing_runtime
    rt.buffer()
    rt.clock.advance(timedelta(seconds=61))
    original = rt.store.recoverable_batches
    def discover(*args, **kwargs):
        page = original(*args, **kwargs)
        rt.dependencies[domain.DependencyName.SQL] = False
        return page
    monkeypatch.setattr(rt.store, "recoverable_batches", discover)
    before = (rt.lease_calls, rt.session_calls, len(rt.processing_broker.calls))
    report = api.RecoveryService(rt).run_once(rt.clock.now())
    assert report.rescheduled == 0
    assert before == (rt.lease_calls, rt.session_calls, len(rt.processing_broker.calls))


def test_recovery_live_committing_attempt_is_excluded_until_its_deadline(processing_runtime, task_api):
    api = recovery_api()
    rt = processing_runtime
    command = rt.buffer()
    rt.store.fail_next_atomic("finalize_committed")
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    rt.clock.advance(timedelta(seconds=61))
    before = rt.lease_calls, rt.session_calls
    report = api.RecoveryService(rt).run_once(rt.clock.now())
    assert report.failed == report.rescheduled == report.quarantined == 0
    assert before == (rt.lease_calls, rt.session_calls)


def test_ready_build_runtime_constructs_bounded_clients_and_canonical_contract(main_module, admin_runtime, monkeypatch):
    import redis
    from app.conversation_tasks import ConversationRuntime
    api = recovery_api()
    created = []
    def from_url(url, **options):
        created.append(options)
        admin_runtime.store.client.connection_pool = SimpleNamespace(connection_kwargs=options)
        return admin_runtime.store.client
    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    monkeypatch.setattr(api, "bounded_sql_dependencies", lambda url: (admin_runtime.session_factory, lambda: True))
    class ProcessingBroker:
        def __init__(self, task, *, probe):
            self.task, self._probe = task, probe
        def probe(self):
            return self._probe()
    class OutboundBroker:
        def __init__(self, task):
            self.task = task
    monkeypatch.setitem(sys.modules, "app.celery_app", SimpleNamespace(
        CeleryProcessingBroker=ProcessingBroker, CeleryOutboundBroker=OutboundBroker,
        probe_broker=lambda app: True))
    runtime = api.build_runtime(settings=main_module.settings,
        processing_task=main_module.process_message_task, outbound_task=main_module.send_message_task,
        celery=main_module.celery_app)
    assert isinstance(runtime, ConversationRuntime)
    assert runtime.coordinator.store is runtime.store
    assert runtime.store.client is admin_runtime.store.client
    assert runtime.readiness_status().ready is True
    assert len(created) == 1
    options = created[0]
    assert options["socket_connect_timeout"] == options["socket_timeout"] == 2
    assert options["retry_on_timeout"] is False and options["retry_on_error"] == []
    assert options["retry"]._retries == 0 and options["decode_responses"] is True
    assert admin_runtime.lease_calls == admin_runtime.session_calls == 0


def _anchor(runtime, phone):
    with runtime.store.contact_lease(phone) as lease:
        return runtime.store.read_anchor(lease)


def _admin_request(client, action, *, hours=3.5, phone=ADMIN_PHONE):
    if action == "create":
        return client.post("/api/paused-contacts", json={"phone": phone, "hours": hours})
    if action == "extend":
        return client.put(f"/api/paused-contacts/{phone}/extend", json={"hours": hours})
    return client.delete(f"/api/paused-contacts/{phone}")


@pytest.mark.parametrize("_case", [None], ids=["flow-30"])
def test_dashboard_manual_pause_create_extend_unpause_rotates_and_preserves_context(_case, admin_client, admin_runtime, session_factory):
    from app.models import ConversationContext, PausedContact
    rt = admin_runtime
    rt.seed_contact(ADMIN_PHONE)
    rt.seed_contact(SIMULATOR_PHONE, paused_hours=5)
    other = rt.store.contact_snapshot(SIMULATOR_PHONE)
    generation = _anchor(rt, ADMIN_PHONE).last_generation
    for action, expected in [("create", "2026-09-12T15:30:00+00:00"),
                             ("extend", "2026-09-12T19:00:00+00:00"), ("unpause", None)]:
        response = _admin_request(admin_client, action)
        assert response.status_code == 200
        if expected:
            assert response.json()["paused_until"] == expected
        anchor = _anchor(rt, ADMIN_PHONE)
        assert anchor.last_generation != generation
        generation = anchor.last_generation
        assert anchor.cycle is (domain.ConversationCycle.OPEN if action == "unpause" else domain.ConversationCycle.PAUSED)
        with session_factory() as db:
            assert db.get(ConversationContext, ADMIN_PHONE).messages[0]["content"] == "synthetic history"
            assert (db.get(PausedContact, ADMIN_PHONE) is None) is (action == "unpause")
        assert rt.store.contact_snapshot(SIMULATOR_PHONE) == other
    assert rt.legacy_sessions == 0
    assert sum(s.events.count("commit_returned") for s in rt.sessions) == 3
    assert len({id(s.session) for s in rt.sessions}) == 3


@pytest.mark.parametrize("action,hours", [("create", 8760), ("extend", 8759)])
@pytest.mark.parametrize("_case", [None], ids=["flow-43"])
def test_dashboard_manual_pause_accepts_exact_365_day_result(_case, admin_client, admin_runtime, action, hours):
    if action == "extend":
        admin_runtime.seed_contact(ADMIN_PHONE, paused_hours=1)
    response = _admin_request(admin_client, action, hours=hours)
    assert response.status_code == 200
    assert response.json()["paused_until"] == "2027-09-12T12:00:00+00:00"
    assert admin_runtime.legacy_sessions == 0


@pytest.mark.parametrize("action", ["create", "extend"])
@pytest.mark.parametrize("hours", [0, -1, "24", None, True, float("inf"), 1e308, 10 ** 400, 1e-308, 8760 + 1 / 3600])
@pytest.mark.parametrize("_case", [None], ids=["flow-31"])
def test_dashboard_manual_pause_rejects_invalid_duration_before_preparation(_case, admin_client, admin_runtime, action, hours):
    rt = admin_runtime
    rt.seed_contact(ADMIN_PHONE, paused_hours=1)
    before = rt.store.contact_snapshot(ADMIN_PHONE)
    prepared = rt.store.client.operation_calls.get("prepare_mutation", 0)
    path = "/api/paused-contacts" if action == "create" else f"/api/paused-contacts/{ADMIN_PHONE}/extend"
    response = admin_client.request("POST" if action == "create" else "PUT", path,
        content=json.dumps({"phone": ADMIN_PHONE, "hours": hours}), headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert rt.store.contact_snapshot(ADMIN_PHONE) == before
    assert rt.store.client.operation_calls.get("prepare_mutation", 0) == prepared
    assert not any("commit_entered" in s.events for s in rt.sessions)


@pytest.mark.parametrize("_case", [None], ids=["flow-43"])
def test_dashboard_extend_rejects_365_days_plus_one_second_result(_case, admin_client, admin_runtime):
    rt = admin_runtime
    rt.seed_contact(ADMIN_PHONE, paused_hours=1)
    before = rt.store.contact_snapshot(ADMIN_PHONE)
    response = _admin_request(admin_client, "extend", hours=8759 + 1 / 3600)
    assert response.status_code == 400
    assert rt.store.contact_snapshot(ADMIN_PHONE) == before


@pytest.mark.parametrize("_case", [None], ids=["flow-43"])
def test_dashboard_manual_pause_datetime_overflow_precedes_lease(_case, admin_client, admin_runtime):
    rt = admin_runtime
    rt.clock.set(datetime.max.replace(tzinfo=timezone.utc))
    response = _admin_request(admin_client, "create", hours=1)
    assert response.status_code == 400
    assert rt.lease_calls == rt.session_calls == 0


def test_manual_pause_duration_must_represent_a_future_deadline():
    with pytest.raises(domain.InvalidManualPauseDuration):
        domain.manual_pause_deadline(datetime(2026, 9, 12, 12, tzinfo=timezone.utc), 1e-308)


@pytest.mark.parametrize("action", ["create", "extend", "unpause"])
@pytest.mark.parametrize("phone", ["invalid", "123", "0551999990000", "1" * 16])
def test_dashboard_invalid_phone_never_acquires_lease(admin_client, admin_runtime, action, phone):
    response = _admin_request(admin_client, action, phone=phone)
    assert response.status_code == 400
    assert admin_runtime.lease_calls == admin_runtime.session_calls == 0


@pytest.mark.parametrize("action", ["extend", "unpause"])
def test_dashboard_missing_paused_contact_is_404_without_mutation(admin_client, admin_runtime, action):
    response = _admin_request(admin_client, action)
    assert response.status_code == 404
    assert not any("commit_entered" in s.events for s in admin_runtime.sessions)
    assert admin_runtime.store.client.operation_calls.get("prepare_mutation", 0) == 0


@pytest.mark.parametrize("message", ["Mensagem sintética", "/pausar", "/pause"])
@pytest.mark.parametrize("_case", [None], ids=["flow-33"])
def test_test_chat_simulator_uses_central_batch_and_local_capture(_case, admin_client, admin_runtime, session_factory, message):
    from app.models import ConversationContext, PausedContact
    rt = admin_runtime
    rt.seed_contact(ADMIN_PHONE, paused_hours=5)
    other = rt.store.contact_snapshot(ADMIN_PHONE)
    response = admin_client.post("/test/chat", json={"message": message, "phone": ADMIN_PHONE, "fromMe": True})
    assert response.status_code == 200
    assert response.json()["phone"] == SIMULATOR_PHONE
    assert response.json()["response"] == ("Para falar com a Beatriz, envie ATENDIMENTO." if message.startswith("/") else "Resposta sintética")
    assert len(rt.agent.calls) == (0 if message.startswith("/") else 1)
    assert all(call[1] == SIMULATOR_PHONE for call in rt.agent.calls)
    assert rt.outbound_broker.calls == rt.processing_broker.calls == rt.transport.calls == []
    assert rt.store.contact_snapshot(ADMIN_PHONE) == other
    assert any(item.body.get("disposition") == "PROCESSED" for item in rt.details(SIMULATOR_PHONE))
    with session_factory() as db:
        assert db.get(PausedContact, SIMULATOR_PHONE) is None
        assert (db.get(ConversationContext, SIMULATOR_PHONE) is None) is message.startswith("/")
    assert rt.legacy_sessions == 0


def test_test_chat_simulator_paused_contact_drops_without_agent(admin_client, admin_runtime):
    rt = admin_runtime
    rt.seed_contact(SIMULATOR_PHONE, paused_hours=3)
    response = admin_client.post("/test/chat", json={"message": "Mensagem sintética"})
    assert response.status_code == 200
    assert "pausado" in response.json()["response"]
    assert rt.agent.calls == rt.outbound_broker.calls == rt.transport.calls == []
    assert any(item.body.get("disposition") == "DROPPED" for item in rt.details(SIMULATOR_PHONE))


def test_reset_simulator_deletes_only_test_state_in_one_fenced_transaction(admin_client, admin_runtime, session_factory):
    from app.models import Appointment, ConversationContext, PausedContact
    rt = admin_runtime
    for phone in (SIMULATOR_PHONE, ADMIN_PHONE):
        rt.seed_contact(phone, paused_hours=3, appointment=True)
    old = _anchor(rt, SIMULATOR_PHONE).last_generation
    other = rt.store.contact_snapshot(ADMIN_PHONE)
    response = admin_client.post("/test/reset", json={"phone": ADMIN_PHONE})
    assert response.status_code == 200
    assert response.json()["phone"] == SIMULATOR_PHONE
    with session_factory() as db:
        for model in (ConversationContext, PausedContact):
            assert db.get(model, SIMULATOR_PHONE) is None
            assert db.get(model, ADMIN_PHONE) is not None
        assert db.query(Appointment).filter_by(patient_phone=SIMULATOR_PHONE).count() == 0
        assert db.query(Appointment).filter_by(patient_phone=ADMIN_PHONE).count() == 1
    anchor = _anchor(rt, SIMULATOR_PHONE)
    assert anchor.last_generation != old and anchor.cycle is domain.ConversationCycle.OPEN
    assert rt.store.contact_snapshot(ADMIN_PHONE) == other
    assert sum(s.events.count("commit_returned") for s in rt.sessions) == 1
    assert rt.legacy_sessions == 0


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("_case", [None], ids=["flow-53"])
def test_scheduler_inactive_context_closes_and_preserves_administrative_pause(_case, scheduler_module, admin_runtime, session_factory, paused):
    import asyncio
    from app.models import ConversationContext, PausedContact
    rt = admin_runtime
    rt.seed_contact(ADMIN_PHONE, age_minutes=61, paused_hours=3 if paused else None)
    rt.seed_contact(SIMULATOR_PHONE, age_minutes=60)
    old = _anchor(rt, ADMIN_PHONE).last_generation
    other = rt.store.contact_snapshot(SIMULATOR_PHONE)
    asyncio.run(scheduler_module.check_inactive_contexts(rt))
    with session_factory() as db:
        assert db.get(ConversationContext, ADMIN_PHONE) is None
        assert db.get(ConversationContext, SIMULATOR_PHONE) is not None
        pause = db.get(PausedContact, ADMIN_PHONE)
        assert (pause is not None) is paused
        if paused:
            assert pause.paused_until == datetime(2026, 9, 12, 15)
            assert pause.reason == "secretary_dashboard_pause"
    anchor = _anchor(rt, ADMIN_PHONE)
    assert anchor.last_generation != old
    assert anchor.cycle is (domain.ConversationCycle.PAUSED if paused else domain.ConversationCycle.CLOSED)
    assert rt.store.contact_snapshot(SIMULATOR_PHONE) == other
    assert sum(s.events.count("commit_returned") for s in rt.sessions) == 1


@pytest.mark.parametrize("_case", [None], ids=["flow-32"])
def test_scheduler_inactive_refresh_at_conditional_delete_preserves_updated_context(_case, scheduler_module, admin_runtime, session_factory):
    import asyncio
    from sqlalchemy import update
    from app.models import ConversationContext
    rt = admin_runtime
    rt.seed_contact(ADMIN_PHONE, age_minutes=61)
    refreshed = []
    def refresh():
        with session_factory() as db:
            assert db.bind.url.database in (None, "", ":memory:")
            db.execute(update(ConversationContext).where(ConversationContext.phone == ADMIN_PHONE).values(last_activity=datetime(2026, 9, 12, 12)))
            db.commit()
        refreshed.append(True)
    rt.persistent_session_hooks["before_context_delete"] = refresh
    asyncio.run(scheduler_module.check_inactive_contexts(rt))
    assert refreshed == [True]
    with session_factory() as db:
        assert db.get(ConversationContext, ADMIN_PHONE).last_activity == datetime(2026, 9, 12, 12)
    anchor = _anchor(rt, ADMIN_PHONE)
    assert anchor.cycle is domain.ConversationCycle.OPEN
    assert anchor.mutation_fence is None
    assert any(item.body.get("phase") == "ABORTED" for item in rt.details(ADMIN_PHONE)
               if item.entry.kind == "mutation")
    assert not any("commit_entered" in s.events for s in rt.sessions)


def test_scheduler_inactive_lock_failure_isolated_from_other_contact(scheduler_module, admin_runtime, session_factory, monkeypatch):
    import asyncio
    from contextlib import contextmanager
    from app.models import ConversationContext
    rt = admin_runtime
    for phone in (ADMIN_PHONE, SIMULATOR_PHONE):
        rt.seed_contact(phone, age_minutes=61)
    original = rt.store.contact_lease
    blocked = rt.store.contact_snapshot(ADMIN_PHONE)
    @contextmanager
    def fail_one_contact(phone):
        if phone == ADMIN_PHONE:
            raise domain.ContactLockUnavailable(domain.FailureReason.CONTACT_LOCK_UNAVAILABLE)
        with original(phone) as lease:
            yield lease
    monkeypatch.setattr(rt.store, "contact_lease", fail_one_contact)
    asyncio.run(scheduler_module.check_inactive_contexts(rt))
    with session_factory() as db:
        remaining = db.query(ConversationContext.phone).all()
        assert remaining == [(ADMIN_PHONE,)]
    assert rt.store.contact_snapshot(ADMIN_PHONE) == blocked
    assert sum(s.events.count("commit_returned") for s in rt.sessions) == 1


def test_test_chat_simulator_uses_reservation_capture_ack_completion_order(admin_client, admin_runtime, task_api, monkeypatch):
    rt = admin_runtime
    trace = []
    original_process = task_api.process_batch
    def process(command, local):
        assert isinstance(command, domain.ProcessingCommand)
        assert command.phone == SIMULATOR_PHONE
        assert local.store is rt.store and local.coordinator is rt.coordinator
        assert local.transport is None
        return original_process(command, local)
    monkeypatch.setattr(task_api, "process_batch", process)
    for name, label in [("reserve_outbound_enqueue", "reserved"),
                        ("record_outbound_attempt", "acknowledged"), ("complete_batch", "completed")]:
        original = getattr(rt.store, name)
        def at_boundary(*args, _original=original, _label=label, **kwargs):
            result = _original(*args, **kwargs)
            if _label == "reserved":
                assert isinstance(result, domain.OutboundReservation)
            trace.append(_label)
            return result
        monkeypatch.setattr(rt.store, name, at_boundary)
    original_capture = task_api._SimulatorCapture.enqueue_outbound
    def capture(broker, outbound):
        assert trace == ["reserved"]
        result = original_capture(broker, outbound)
        assert broker.outbound == [outbound]
        assert result is domain.EnqueueResult.CONFIRMED
        trace.append("captured")
        return result
    monkeypatch.setattr(task_api._SimulatorCapture, "enqueue_outbound", capture)
    response = admin_client.post("/test/chat", json={"message": "synthetic"})
    assert response.status_code == 200
    assert trace == ["reserved", "captured", "acknowledged", "completed"]
    assert rt.processing_broker.calls == rt.outbound_broker.calls == rt.transport.calls == []


def test_dashboard_normalizes_formatted_phone_once_in_adapter(admin_client, main_module, monkeypatch):
    original = main_module.normalize_phone
    calls = []
    def normalize(raw):
        calls.append(raw)
        return original(raw)
    monkeypatch.setattr(main_module, "normalize_phone", normalize)
    response = _admin_request(admin_client, "create", phone="+55 (51) 99999-0011")
    assert response.status_code == 200
    assert response.json()["phone"] == ADMIN_PHONE
    assert calls == ["+55 (51) 99999-0011"]


def test_reset_rolls_back_context_and_pause_if_appointment_delete_fails(admin_client, admin_runtime, session_factory):
    from app.models import Appointment, ConversationContext, PausedContact
    rt = admin_runtime
    rt.seed_contact(SIMULATOR_PHONE, paused_hours=3, appointment=True)
    def fail():
        raise RuntimeError("synthetic appointment delete failure")
    rt.session_hooks["before_appointment_delete"] = fail
    response = admin_client.post("/test/reset")
    assert response.status_code == 503
    assert rt.sessions[-1].events.count("before_context_delete") == 1
    assert rt.sessions[-1].events.count("before_appointment_delete") == 1
    assert "commit_entered" not in rt.sessions[-1].events
    with session_factory() as db:
        assert db.get(ConversationContext, SIMULATOR_PHONE) is not None
        assert db.get(PausedContact, SIMULATOR_PHONE) is not None
        assert db.query(Appointment).filter_by(patient_phone=SIMULATOR_PHONE).count() == 1


@pytest.mark.parametrize("_case", [None], ids=["flow-32"])
def test_scheduler_refresh_after_scan_before_lease_preserves_context(_case, scheduler_module, admin_runtime, session_factory, monkeypatch):
    import asyncio
    from contextlib import contextmanager
    from sqlalchemy import update
    from app.models import ConversationContext
    rt = admin_runtime
    rt.seed_contact(ADMIN_PHONE, age_minutes=61)
    original = rt.store.contact_lease
    refreshed = []
    @contextmanager
    def lease_after_refresh(phone):
        if not refreshed:
            with session_factory() as db:
                assert db.bind.url.database in (None, "", ":memory:")
                db.execute(update(ConversationContext).where(ConversationContext.phone == phone).values(
                    last_activity=datetime(2026, 9, 12, 12)))
                db.commit()
            refreshed.append(phone)
        with original(phone) as lease:
            yield lease
    monkeypatch.setattr(rt.store, "contact_lease", lease_after_refresh)
    asyncio.run(scheduler_module.check_inactive_contexts(rt))
    assert refreshed == [ADMIN_PHONE]
    with session_factory() as db:
        assert db.get(ConversationContext, ADMIN_PHONE).last_activity == datetime(2026, 9, 12, 12)
    assert not any("commit_entered" in s.events for s in rt.sessions)


def test_scheduler_inactive_cutoff_converts_injected_clock_to_sql_utc(scheduler_module, admin_runtime, session_factory):
    import asyncio
    from app.models import ConversationContext
    rt = admin_runtime
    rt.seed_contact(ADMIN_PHONE, age_minutes=61)
    rt.seed_contact(SIMULATOR_PHONE, age_minutes=59)
    rt.clock.set(datetime(2026, 9, 12, 9, tzinfo=timezone(timedelta(hours=-3))))
    asyncio.run(scheduler_module.check_inactive_contexts(rt))
    with session_factory() as db:
        assert db.get(ConversationContext, ADMIN_PHONE) is None
        assert db.get(ConversationContext, SIMULATOR_PHONE) is not None


@pytest.fixture
def task_api():
    assert importlib.util.find_spec("app.conversation_tasks") is not None, "recoverable task bodies are missing"
    return importlib.import_module("app.conversation_tasks")


@pytest.fixture
def processing_runtime(session_factory, monkeypatch):
    from app.simple_config import settings
    from tests.fakes import ProcessingRuntime
    original_connect = socket.socket.connect
    def guarded_connect(sock, address):
        caller = sys._getframe(1)
        if (caller.f_code.co_name == "_fallback_socketpair"
                and caller.f_globals.get("__name__") == "socket"
                and address[0] in ("127.0.0.1", "::1")):
            return original_connect(sock, address)
        raise AssertionError("external access is forbidden")
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    return ProcessingRuntime(session_factory, domain.ConversationConfig.from_settings(settings))


@pytest.mark.parametrize("intent,kind", [
    pytest.param(domain.AgentIntent.SAVE_CONTEXT, domain.OutboundKind.NORMAL, id="flow-56-normal-result-sql-output"),
    pytest.param(domain.AgentIntent.PAUSE_FOR_SECRETARY, domain.OutboundKind.TRANSFER_CONFIRMATION, id="flow-20-nested-transfer"),
    (domain.AgentIntent.CLOSE_CONTEXT, domain.OutboundKind.CLOSURE_CONFIRMATION),
])
def test_process_batch_commits_winning_result_before_enqueue_and_done(task_api, processing_runtime, session_factory, intent, kind):
    from app.models import ConversationContext, PausedContact
    rt = processing_runtime
    command = rt.buffer()
    rt.agent.intent = intent
    acquired = rt.lease_calls
    def at_enqueue(outbound):
        assert outbound.kind is kind
        assert rt.sessions[-1].events.count("commit_returned") == 1
        details = rt.store.read_details(rt.active_lease)
        assert not any(item.body.get("disposition") == "PROCESSED" for item in details)
        processing = next(item for item in details if item.entry.kind == "processing")
        reservation = domain.OutboundReservation.from_payload(processing.body["outbound_reservation"])
        assert reservation.generation == outbound.generation
        assert reservation.processing_id == outbound.processing_id
        assert reservation.operation_id == outbound.operation_id
        assert processing.body.get("outbound_attempted") is not True
    original = rt.coordinator.apply_agent_result
    def apply(*args, **kwargs):
        rt.active_lease = args[-1]
        return original(*args, **kwargs)
    rt.coordinator.apply_agent_result = apply
    rt.outbound_broker.on_enqueue = at_enqueue
    outcome = task_api.process_batch(command, rt)
    assert outcome is task_api.ProcessingOutcome.PROCESSED
    assert rt.lease_calls == acquired + 1
    assert len(rt.agent.calls) == len(rt.outbound_broker.calls) == 1
    assert rt.transport.calls == []
    with session_factory() as db:
        context, pause = db.get(ConversationContext, PHONE), db.get(PausedContact, PHONE)
        assert (context is not None) is (intent is domain.AgentIntent.SAVE_CONTEXT)
        assert (pause is not None) is (intent is domain.AgentIntent.PAUSE_FOR_SECRETARY)
        if context:
            assert context.messages[-1]["content"] == "Resposta sintética"
        if pause:
            assert pause.paused_until == (rt.clock.now() + timedelta(hours=24)).replace(tzinfo=None)
    assert rt.envelopes() == []
    assert any(item.body.get("disposition") == "PROCESSED" for item in rt.details())
    sessions = rt.session_calls
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.TERMINAL
    assert rt.session_calls == sessions
    assert len(rt.agent.calls) == len(rt.outbound_broker.calls) == 1


@pytest.mark.parametrize("_case", [None], ids=["flow-24"])
def test_process_batch_retry_after_result_ready_reuses_result_and_explicit_ids(_case, task_api, processing_runtime):
    rt = processing_runtime
    command = rt.buffer()
    rt.store.fail_next_atomic("prepare_mutation")
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    retry = caught.value.command
    assert retry.batch_id == command.batch_id
    assert retry.staging_id == command.batch_id
    assert retry.processing_id and retry.operation_id
    assert retry.generation == command.generation
    assert len(rt.agent.calls) == 1
    assert rt.outbound_broker.calls == []
    assert task_api.process_batch(retry, rt) is task_api.ProcessingOutcome.PROCESSED
    assert len(rt.agent.calls) == len(rt.outbound_broker.calls) == 1


@pytest.mark.parametrize("_case", [None], ids=["flow-58"])
def test_process_batch_sql_commit_then_lost_finalization_never_repeats_dml(_case, task_api, processing_runtime):
    rt = processing_runtime
    command = rt.buffer()
    rt.store.fail_next_atomic("finalize_committed")
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    assert rt.sessions[-1].events.count("commit_returned") == 1
    sessions = rt.session_calls
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(caught.value.command, rt)
    assert rt.session_calls == sessions
    assert len(rt.agent.calls) == 1
    assert rt.outbound_broker.calls == []


def test_process_batch_result_at_deadline_has_no_sql_or_outbound(task_api, processing_runtime):
    rt = processing_runtime
    command = rt.buffer()
    rt.agent.on_prepare = lambda: rt.clock.advance(timedelta(seconds=600))
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    assert not any("commit_entered" in session.events for session in rt.sessions)
    assert rt.outbound_broker.calls == []
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.TERMINAL


def test_process_batch_outbound_before_complete_failure_reuses_commit_without_sql(task_api, processing_runtime):
    rt = processing_runtime
    command = rt.buffer()
    rt.store.fail_next_atomic("complete_batch")
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    assert len(rt.outbound_broker.calls) == 1
    rt.clock.advance(timedelta(seconds=601))
    assert task_api.process_batch(caught.value.command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert len(rt.agent.calls) == 1
    assert sum(s.events.count("commit_entered") for s in rt.sessions) == 1
    # The external enqueue boundary is intentionally not an exactly-once claim.
    assert len(rt.outbound_broker.calls) == 2


def test_process_batch_invalidated_committed_work_is_failed_without_blocking_next_conversation(task_api, processing_runtime):
    rt = processing_runtime
    command = rt.buffer("old synthetic input")
    rt.outbound_broker.next_result = domain.EnqueueResult.DEFINITIVE_FAILURE
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    retry = caught.value.command
    old_outbound = rt.outbound_broker.calls[-1]
    old_commits = sum(s.events.count("commit_entered") for s in rt.sessions)
    pause = rt.pause()
    rt.outbound_broker.next_result = domain.EnqueueResult.CONFIRMED
    assert task_api.process_batch(retry, rt) is task_api.ProcessingOutcome.TERMINAL
    with rt.store.contact_lease(PHONE) as lease:
        anchor = rt.store.read_anchor(lease)
        details = rt.store.read_details(lease)
        assert str(anchor.last_generation) == pause.generation
        assert anchor.cycle is domain.ConversationCycle.PAUSED
        old_batch = next(item for item in details if item.entry.kind == "batch" and item.entry.id == command.batch_id)
        old_processing = next(item for item in details if item.entry.kind == "processing" and item.entry.id == retry.processing_id)
        receipt = next(item for item in details if item.entry.kind == "dedupe" and item.body.get("batch_id") == command.batch_id)
        assert old_batch.terminal and old_batch.body["phase"] == "EXHAUSTED"
        assert old_processing.terminal
        assert receipt.terminal and receipt.body["disposition"] == "FAILED"
        assert not any(item.entry.kind == "staging" and item.entry.id == command.batch_id for item in details)
        assert rt.store.inspect_mutation(PHONE, retry.operation_id, lease).phase is domain.MutationPhase.COMMITTED
    assert all(c.batch_id != command.batch_id for c in rt.store.recoverable_batches().commands)
    assert "old synthetic input" not in str(rt.store.contact_snapshot(PHONE))
    assert len(rt.agent.calls) == len(rt.outbound_broker.calls) == 1
    assert sum(s.events.count("commit_entered") for s in rt.sessions) == old_commits
    assert task_api.send_outbound(old_outbound, rt) is task_api.SendOutcome.DISCARDED
    assert rt.transport.calls == []
    rt.clock.set(pause.paused_until)
    newer = rt.buffer("new synthetic input")
    assert task_api.process_batch(newer, rt) is task_api.ProcessingOutcome.PROCESSED
    assert rt.agent.calls[-1][0] == "new synthetic input"


@pytest.mark.parametrize("content,kind,expected", [
    ("/pause", "text", "Para falar com a Beatriz, envie ATENDIMENTO."),
    ("/pausar", "text", "Para falar com a Beatriz, envie ATENDIMENTO."),
    *[(label, "media", f"Desculpe, não consigo receber {label}. Se puder me explicar por texto, consigo te ajudar!\n\nCaso prefira, posso te transferir para nossa secretária Beatriz.")
      for label in ("imagem", "áudio", "vídeo", "documento", "figurinha")],
])
def test_process_batch_fixed_reply_preserves_context_and_bypasses_claude(task_api, processing_runtime, session_factory, content, kind, expected):
    from app.models import ConversationContext
    rt = processing_runtime
    task_api.process_batch(rt.buffer(), rt)
    with session_factory() as db:
        row = db.get(ConversationContext, PHONE)
        original = deepcopy((row.messages, row.current_flow, row.flow_data, row.last_activity))
    calls = len(rt.agent.calls)
    commits = sum(s.events.count("commit_entered") for s in rt.sessions)
    command = rt.buffer(content, kind=kind)
    rt.clock.advance(timedelta(seconds=1))
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert len(rt.agent.calls) == calls
    assert sum(s.events.count("commit_entered") for s in rt.sessions) == commits
    with session_factory() as db:
        row = db.get(ConversationContext, PHONE)
        assert (row.messages, row.current_flow, row.flow_data, row.last_activity) == original
    assert rt.outbound_broker.calls[-1].text == expected
    assert rt.outbound_broker.calls[-1].kind is domain.OutboundKind.NORMAL


def test_process_batch_fixed_retry_uses_staged_result_and_no_sql(task_api, processing_runtime):
    rt = processing_runtime
    command = rt.buffer("/pause")
    rt.store.fail_next_atomic("prepare_fixed_response")
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    assert any(item.body.get("phase") == "RESULT_READY" for item in rt.details())
    assert task_api.process_batch(caught.value.command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert rt.agent.calls == []
    assert not any("commit_entered" in s.events for s in rt.sessions)


def staged_fixed_before_deadline(runtime):
    from dataclasses import replace
    command = runtime.buffer("/pause")
    with runtime.store.contact_lease(PHONE) as lease:
        claim = runtime.store.claim_or_resume_batch(command, runtime.clock.now(), lease)
        result = domain.AgentResult("Para falar com a Beatriz, envie ATENDIMENTO.", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        runtime.store.stage_agent_result(command, claim.attempt, result, runtime.clock.now(), lease)
    runtime.clock.set(claim.attempt.processing_deadline - timedelta(seconds=1))
    return replace(command, processing_id=claim.attempt.processing_id,
        operation_id=claim.attempt.operation_id, staging_id=command.batch_id), claim.attempt.processing_deadline


@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("boundary", ["before_reservation", "atomic_reservation"])
def test_no_sql_deadline_before_outbound_reservation_has_zero_enqueue(task_api, processing_runtime, offset, boundary):
    rt = processing_runtime
    command, deadline = staged_fixed_before_deadline(rt)
    hook = lambda: rt.clock.set(deadline + timedelta(seconds=offset))
    if boundary == "before_reservation":
        rt.store.client.after_operation["prepare_fixed_response"] = hook
    else:
        rt.store.client.before_operation["reserve_outbound_enqueue"] = hook
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    assert rt.outbound_broker.calls == []
    assert rt.agent.calls == rt.transport.calls == []
    assert not any("commit_entered" in session.events for session in rt.sessions)
    with rt.store.contact_lease(PHONE) as lease:
        assert rt.store.dispatch(command, lease).phase is domain.DispatchPhase.EXHAUSTED
        assert any(item.body.get("disposition") == "FAILED" for item in rt.store.read_details(lease))


def test_no_sql_reservation_before_deadline_survives_clock_passage_until_local_enqueue(task_api, processing_runtime):
    rt = processing_runtime
    command, deadline = staged_fixed_before_deadline(rt)
    rt.store.client.after_operation["reserve_outbound_enqueue"] = lambda: rt.clock.set(deadline)
    def not_acknowledged_yet(_):
        assert '"outbound_reservation"' in str(rt.store.contact_snapshot(PHONE))
        assert '"outbound_attempted":true' not in str(rt.store.contact_snapshot(PHONE))
    rt.outbound_broker.on_enqueue = not_acknowledged_yet
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert rt.clock.now() == deadline
    assert len(rt.outbound_broker.calls) == 1
    assert rt.agent.calls == []
    assert not any("commit_entered" in s.events for s in rt.sessions)
    with rt.store.contact_lease(PHONE) as lease:
        assert rt.store.dispatch(command, lease).phase is domain.DispatchPhase.PROCESSED
        assert any(item.body.get("disposition") == "PROCESSED" for item in rt.store.read_details(lease))


@pytest.mark.parametrize("outcome", [domain.EnqueueResult.DEFINITIVE_FAILURE, domain.EnqueueResult.AMBIGUOUS])
def test_no_sql_reserved_broker_retry_keeps_same_reservation_after_deadline(task_api, processing_runtime, outcome):
    rt = processing_runtime
    command, deadline = staged_fixed_before_deadline(rt)
    rt.outbound_broker.next_result = outcome
    rt.outbound_broker.on_enqueue = lambda _: rt.clock.set(deadline)
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    with rt.store.contact_lease(PHONE) as lease:
        details = rt.store.read_details(lease)
        processing = next(item for item in details if item.entry.kind == "processing")
        reservation = processing.body.get("outbound_reservation")
        assert reservation is not None
        assert processing.body.get("outbound_attempted") is not True
        assert rt.store.dispatch(command, lease).phase is domain.DispatchPhase.STAGED
        assert not any(item.body.get("disposition") == "PROCESSED" for item in details)
        rt.store.exhaust_batch(command, rt.clock.now(), lease)
        assert rt.store.dispatch(command, lease).phase is domain.DispatchPhase.STAGED
    # Recovery must still schedule the reserved batch past its old deadline.
    rt.clock.advance(timedelta(seconds=601))
    with rt.store.contact_lease(PHONE) as lease:
        assert rt.store.ensure_consumer(rt.processing_broker, command, rt.clock.now(), lease) is domain.EnsureConsumerResult.SCHEDULED
    rt.outbound_broker.on_enqueue = None
    rt.outbound_broker.next_result = domain.EnqueueResult.CONFIRMED
    assert task_api.process_batch(caught.value.command, rt) is task_api.ProcessingOutcome.PROCESSED
    with rt.store.contact_lease(PHONE) as lease:
        processing = next(item for item in rt.store.read_details(lease) if item.entry.kind == "processing")
        assert processing.body["outbound_reservation"] == reservation
        assert processing.body["outbound_attempted"] is True
    assert rt.agent.calls == []
    assert len(rt.outbound_broker.calls) == 2  # No exactly-once claim at this external boundary.
    assert not any("commit_entered" in s.events for s in rt.sessions)


@pytest.mark.parametrize("field", ["reservation_id", "batch_id", "processing_id", "operation_id",
    "coordination_epoch", "generation", "claim_token", "result_fingerprint"])
@pytest.mark.parametrize("boundary", ["record_outbound_attempt", "complete_batch", "terminal_complete"])
def test_outbound_boundary_rejects_substituted_reservation(task_api, processing_runtime, field, boundary):
    from dataclasses import replace
    from uuid import uuid4
    rt = processing_runtime
    command, _ = staged_fixed_before_deadline(rt)
    with rt.store.contact_lease(PHONE) as lease:
        claim = rt.store.claim_or_resume_batch(command, rt.clock.now(), lease)
        rt.store.prepare_fixed_response(command, claim.attempt, rt.clock.now(), lease)
        reservation = rt.store.reserve_outbound_enqueue(command, claim.attempt, rt.clock.now(), lease)
        if boundary in ("complete_batch", "terminal_complete"):
            rt.store.record_outbound_attempt(command, claim.attempt, rt.clock.now(), lease, reservation=reservation)
        if boundary == "terminal_complete":
            rt.store.complete_batch(command, claim.attempt, rt.clock.now(), lease, reservation=reservation)
        changed = replace(reservation, **{field: "0" * 64 if field == "result_fingerprint" else str(uuid4())})
        before = rt.store.contact_snapshot(PHONE)
        with pytest.raises(domain.ConversationMutationPending):
            getattr(rt.store, "complete_batch" if boundary == "terminal_complete" else boundary)(
                command, claim.attempt, rt.clock.now(), lease, reservation=changed)
        assert rt.store.contact_snapshot(PHONE) == before
        expected = domain.DispatchPhase.PROCESSED if boundary == "terminal_complete" else domain.DispatchPhase.STAGED
        assert rt.store.dispatch(command, lease).phase is expected


@pytest.mark.parametrize("kind", ["text", "pause_help"])
def test_process_batch_cannot_complete_before_outbound_attempt_boundary(task_api, processing_runtime, kind):
    rt = processing_runtime
    command = rt.buffer("Para falar com a Beatriz, envie ATENDIMENTO." if kind == "pause_help" else "mensagem", kind=kind)
    # A definitive broker failure leaves the committed/applied result recoverable.
    rt.outbound_broker.next_result = domain.EnqueueResult.DEFINITIVE_FAILURE
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    with rt.store.contact_lease(PHONE) as lease:
        claim = rt.store.claim_or_resume_batch(caught.value.command, rt.clock.now(), lease)
        reservation = rt.store.reserve_outbound_enqueue(caught.value.command, claim.attempt, rt.clock.now(), lease)
        with pytest.raises(domain.ConversationMutationPending):
            rt.store.complete_batch(caught.value.command, claim.attempt, rt.clock.now(), lease, reservation=reservation)


@pytest.mark.parametrize("boundary", ["reserve_outbound_enqueue", "record_outbound_attempt", "complete_batch"])
@pytest.mark.parametrize("fault", ["claim_token", "processing_id", "operation_id", "coordination_epoch", "generation", "owner"])
def test_outbound_reservation_boundaries_reject_stale_processing_claim(task_api, processing_runtime, boundary, fault):
    from dataclasses import replace
    from uuid import uuid4
    rt = processing_runtime
    command, _ = staged_fixed_before_deadline(rt)
    with rt.store.contact_lease(PHONE) as first_lease:
        claim = rt.store.claim_or_resume_batch(command, rt.clock.now(), first_lease)
        rt.store.prepare_fixed_response(command, claim.attempt, rt.clock.now(), first_lease)
        reservation = rt.store.reserve_outbound_enqueue(command, claim.attempt, rt.clock.now(), first_lease)
        rt.store.record_outbound_attempt(command, claim.attempt, rt.clock.now(), first_lease, reservation=reservation)
    with rt.store.contact_lease(PHONE) as lease:
        # A fresh lease alone cannot impersonate the previous processing owner.
        if fault != "owner":
            claim = rt.store.claim_or_resume_batch(command, rt.clock.now(), lease)
        attempt = claim.attempt if fault == "owner" else replace(claim.attempt, **{fault: str(uuid4())})
        before = rt.store.contact_snapshot(PHONE)
        kwargs = {} if boundary == "reserve_outbound_enqueue" else {"reservation": reservation}
        with pytest.raises(domain.ConversationMutationPending):
            getattr(rt.store, boundary)(command, attempt, rt.clock.now(), lease, **kwargs)
        assert rt.store.contact_snapshot(PHONE) == before


@pytest.mark.parametrize("boundary", ["reserve_outbound_enqueue", "record_outbound_attempt", "complete_batch"])
def test_reserved_no_sql_invalidated_generation_cannot_record_or_complete(task_api, processing_runtime, boundary):
    from uuid import uuid4
    rt = processing_runtime
    command, _ = staged_fixed_before_deadline(rt)
    with rt.store.contact_lease(PHONE) as lease:
        claim = rt.store.claim_or_resume_batch(command, rt.clock.now(), lease)
        rt.store.prepare_fixed_response(command, claim.attempt, rt.clock.now(), lease)
        reservation = rt.store.reserve_outbound_enqueue(command, claim.attempt, rt.clock.now(), lease)
        rt.store.record_outbound_attempt(command, claim.attempt, rt.clock.now(), lease, reservation=reservation)
        with rt.session_factory() as db:
            pause = rt.coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", rt.clock.now(), lease, str(uuid4()))
        kwargs = {} if boundary == "reserve_outbound_enqueue" else {"reservation": reservation}
        with pytest.raises(domain.ConversationMutationPending):
            getattr(rt.store, boundary)(command, claim.attempt, rt.clock.now(), lease, **kwargs)
        rt.store.exhaust_batch(command, rt.clock.now(), lease)
        assert rt.store.dispatch(command, lease).phase is domain.DispatchPhase.EXHAUSTED
        assert str(rt.store.read_anchor(lease).last_generation) == pause.generation
        assert any(item.body.get("disposition") == "FAILED" for item in rt.store.read_details(lease))
        assert not any(item.entry.kind == "staging" for item in rt.store.read_details(lease))
    assert rt.agent.calls == rt.outbound_broker.calls == rt.transport.calls == []


@pytest.mark.parametrize("boundary", ["reserve_outbound_enqueue", "record_outbound_attempt", "complete_batch"])
@pytest.mark.parametrize("replace_fingerprint", [False, True])
def test_outbound_reservation_rejects_substituted_staged_result(task_api, processing_runtime, boundary, replace_fingerprint):
    import hashlib
    from app.conversation_redis import _json
    rt = processing_runtime
    command, _ = staged_fixed_before_deadline(rt)
    with rt.store.contact_lease(PHONE) as lease:
        claim = rt.store.claim_or_resume_batch(command, rt.clock.now(), lease)
        rt.store.prepare_fixed_response(command, claim.attempt, rt.clock.now(), lease)
        reservation = rt.store.reserve_outbound_enqueue(command, claim.attempt, rt.clock.now(), lease)
        rt.store.record_outbound_attempt(command, claim.attempt, rt.clock.now(), lease, reservation=reservation)
        details = rt.store.read_details(lease)
        staging = next(item for item in details if item.entry.kind == "staging")
        result = {**staging.body["result"], "text": "synthetic-replaced-output"}
        updates = [rt.store._changed(staging, body={**staging.body, "result": result})]
        if replace_fingerprint:
            processing = next(item for item in details if item.entry.kind == "processing")
            updates.append(rt.store._changed(processing, body={**processing.body,
                "result_fingerprint": hashlib.sha256(_json(result).encode()).hexdigest()}))
        rt.store.compare_and_set(lease, rt.store.read_anchor(lease), rt.store._replace_details(details, *updates))
        before = rt.store.contact_snapshot(PHONE)
        kwargs = {} if boundary == "reserve_outbound_enqueue" else {"reservation": reservation}
        with pytest.raises(domain.ConversationMutationPending):
            getattr(rt.store, boundary)(command, claim.attempt, rt.clock.now(), lease, **kwargs)
        assert rt.store.contact_snapshot(PHONE) == before
    assert rt.outbound_broker.calls == []


def test_no_sql_reservation_uses_persisted_deadline_not_caller_deadline(task_api, processing_runtime):
    from dataclasses import replace
    rt = processing_runtime
    command, deadline = staged_fixed_before_deadline(rt)
    with rt.store.contact_lease(PHONE) as lease:
        claim = rt.store.claim_or_resume_batch(command, rt.clock.now(), lease)
        rt.store.prepare_fixed_response(command, claim.attempt, rt.clock.now(), lease)
        rt.store.client.before_operation["reserve_outbound_enqueue"] = lambda: rt.clock.set(deadline)
        forged = replace(claim.attempt, processing_deadline=deadline + timedelta(days=1))
        with pytest.raises(domain.ConversationMutationPending):
            rt.store.reserve_outbound_enqueue(command, forged, rt.clock.now(), lease)
        assert rt.store.dispatch(command, lease).phase is domain.DispatchPhase.EXHAUSTED
    assert rt.outbound_broker.calls == []


@pytest.mark.parametrize("fault", ["before_atomic", "lost_ack", "lost_owner"])
def test_outbound_reservation_fault_precedes_broker_and_preserves_recoverable_result(task_api, processing_runtime, fault):
    from app.conversation_redis import contact_keys
    rt = processing_runtime
    command, deadline = staged_fixed_before_deadline(rt)
    if fault == "before_atomic":
        rt.store.fail_next_atomic("reserve_outbound_enqueue")
    elif fault == "lost_ack":
        def lose_ack():
            rt.clock.set(deadline)
            raise RuntimeError("synthetic-private-reservation-error")
        rt.store.client.after_operation["reserve_outbound_enqueue"] = lose_ack
    else:
        rt.store.client.after_operation["reserve_outbound_enqueue"] = lambda: rt.store.client.values.pop(contact_keys(PHONE).lease)
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    assert "synthetic-private" not in "".join(traceback.format_exception(caught.value))
    assert rt.outbound_broker.calls == []
    details = rt.details()
    processing = next(item for item in details if item.entry.kind == "processing")
    reservation = processing.body.get("outbound_reservation")
    assert (reservation is None) is (fault == "before_atomic")
    assert processing.body.get("outbound_attempted") is not True
    assert not any(item.body.get("disposition") == "PROCESSED" for item in details)
    assert task_api.process_batch(caught.value.command, rt) is task_api.ProcessingOutcome.PROCESSED
    final = next(item for item in rt.details() if item.entry.kind == "processing")
    if reservation is not None:
        assert final.body["outbound_reservation"] == reservation
    assert len(rt.outbound_broker.calls) == 1
    assert rt.agent.calls == []
    assert not any("commit_entered" in session.events for session in rt.sessions)


@pytest.mark.parametrize("fault", ["missing", "extra", "phone", "generation", "kind", "ref_extra", "ref_date", "ref_type", "processing_id"])
def test_outbound_command_rejects_invalid_json_payload_without_exposing_values(task_api, fault):
    from uuid import uuid4
    payload = domain.OutboundEnvelope(PHONE, "synthetic-private-text", domain.OutboundKind.NORMAL,
        str(uuid4()), str(uuid4()), str(uuid4())).to_dict()
    if fault == "missing":
        del payload["text"]
    elif fault == "extra":
        payload["private"] = "synthetic-private-value"
    elif fault == "ref_extra":
        payload["pause_ref"] = {"generation": payload["generation"], "paused_until": "2026-09-12T12:00:00+00:00",
            "reason": "user_requested_human_assistance", "extra": "private"}
    elif fault in ("ref_date", "ref_type"):
        payload["pause_ref"] = {"generation": payload["generation"],
            "paused_until": "2026-09-12T12:00:00" if fault == "ref_date" else 1,
            "reason": "user_requested_human_assistance"}
    else:
        payload[fault] = "synthetic-private-value"
    with pytest.raises(domain.ConversationDomainError) as caught:
        domain.OutboundEnvelope.from_payload(payload)
    assert caught.value.reason_code == "invalid_task_command"
    assert "synthetic-private" not in "".join(traceback.format_exception(caught.value))


def test_outbound_command_roundtrip_uses_canonical_processing_type(task_api):
    from uuid import uuid4
    assert task_api.ProcessingCommand is domain.ProcessingCommand
    generation, operation = str(uuid4()), str(uuid4())
    ref = domain.PauseTransitionRef(generation, datetime(2026, 9, 13, tzinfo=timezone.utc), "user_requested_human_assistance")
    outbound = domain.OutboundEnvelope(PHONE, "Resposta", domain.OutboundKind.TRANSFER_CONFIRMATION,
        generation, str(uuid4()), operation, pause_ref=ref)
    assert domain.OutboundEnvelope.from_payload(json.loads(json.dumps(outbound.to_payload()))) == outbound


@pytest.mark.parametrize("wrapper", ["process_message_task", "send_message_task"])
def test_task_wrapper_invalid_payload_never_retries_or_creates_effects(main_module, processing_runtime, wrapper):
    from tests.fakes import RetryTask
    task = RetryTask()
    with pytest.raises(domain.ConversationDomainError) as caught:
        getattr(main_module, wrapper)(task, {"private": "synthetic-private-value"})
    assert caught.value.reason_code == "invalid_task_command"
    assert task.calls == []
    assert processing_runtime.lease_calls == 0


@pytest.mark.parametrize("fault", [
    pytest.param("coordination", id="redis-preparation-retry"),
    pytest.param("sql", id="flow-23-sql-flush-retry"),
])
def test_task_wrapper_processing_retry_preserves_staging_ids_and_celery_retry(fault, main_module, processing_runtime, task_api, monkeypatch, caplog):
    from celery.exceptions import Retry
    from tests.fakes import RetryTask
    rt, task = processing_runtime, RetryTask()
    monkeypatch.setattr(main_module.app.state, "conversation_runtime", rt, raising=False)
    command = rt.buffer()
    if fault == "coordination":
        rt.store.fail_next_atomic("prepare_mutation")
    else:
        from sqlalchemy.exc import SQLAlchemyError
        original_flush = Session.flush
        def fail_once(db, *args, **kwargs):
            if not (db.new or db.dirty or db.deleted):
                return original_flush(db, *args, **kwargs)
            monkeypatch.setattr(Session, "flush", original_flush)
            raise SQLAlchemyError("synthetic-private-flush-error")
        monkeypatch.setattr(Session, "flush", fail_once)
    with caplog.at_level(logging.INFO):
        logging.getLogger("unrelated_control").info("visible_control")
        with pytest.raises(Retry):
            main_module.process_message_task(task, command.to_payload())
    assert len(task.calls) == 1
    retry = domain.ProcessingCommand.from_payload(task.calls[0]["args"][0])
    assert retry.processing_id and retry.operation_id and retry.staging_id == command.batch_id
    assert task.calls[0]["kwargs"] == {}
    assert rt.outbound_broker.calls == rt.transport.calls == []
    assert main_module.process_message_task(RetryTask(), retry.to_payload()) == "PROCESSED"
    captured = "\n".join(record.getMessage() for record in caplog.records)
    assert "visible_control" in captured
    assert PHONE not in captured and "Mensagem sintética" not in captured


def test_task_wrapper_sender_retries_typed_errors_and_discards_without_retry(main_module, processing_runtime, task_api, monkeypatch):
    from celery.exceptions import Retry
    from tests.fakes import RetryTask
    rt, task = processing_runtime, RetryTask()
    monkeypatch.setattr(main_module.app.state, "conversation_runtime", rt, raising=False)
    task_api.process_batch(rt.buffer(), rt)
    outbound = rt.outbound_broker.calls[-1]
    rt.dependencies[domain.DependencyName.SQL] = False
    with pytest.raises(Retry):
        main_module.send_message_task(task, outbound.to_payload())
    assert len(task.calls) == 1
    assert task.calls[0]["args"] == [outbound.to_payload()]
    rt.dependencies[domain.DependencyName.SQL] = True
    rt.pause()
    assert main_module.send_message_task(task, outbound.to_payload()) == "DISCARDED"
    assert len(task.calls) == 1


def test_task_wrapper_unexpected_errors_and_existing_celery_retry_are_not_retried(main_module, processing_runtime, monkeypatch):
    from celery.exceptions import Retry
    from tests.fakes import RetryTask
    rt, task = processing_runtime, RetryTask()
    monkeypatch.setattr(main_module.app.state, "conversation_runtime", rt, raising=False)
    command = rt.buffer()
    for error in (Retry("synthetic"), RuntimeError("synthetic")):
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(main_module, "process_batch", fail)
        with pytest.raises(type(error)):
            main_module.process_message_task(task, command.to_payload())
    assert task.calls == []


def test_task_celery_brokers_publish_json_commands_and_keep_task_routes(main_module, processing_runtime, task_api, monkeypatch, caplog):
    from pathlib import Path
    import celery
    class FakeCelery:
        def __init__(self, *args, **kwargs):
            self.conf = {}
    monkeypatch.setattr(celery, "Celery", FakeCelery)
    spec = importlib.util.spec_from_file_location("synthetic_celery_config", Path(__file__).parents[1] / "app" / "celery_app.py")
    module = importlib.util.module_from_spec(spec)
    with caplog.at_level(logging.INFO):
        spec.loader.exec_module(module)
    assert "synthetic.invalid" not in caplog.text
    calls = []
    task = SimpleNamespace(apply_async=lambda **kwargs: calls.append(kwargs))
    broker = module.CeleryProcessingBroker(task, probe=lambda: True)
    command = processing_runtime.buffer()
    assert broker.probe() is True
    assert broker.enqueue_processing(command) is domain.EnqueueResult.CONFIRMED
    assert json.loads(json.dumps(calls[-1]["args"])) == [command.to_payload()]
    assert calls[-1]["countdown"] == 10
    task_api.process_batch(command, processing_runtime)
    outbound = processing_runtime.outbound_broker.calls[-1]
    assert module.CeleryOutboundBroker(task).enqueue_outbound(outbound) is domain.EnqueueResult.CONFIRMED
    assert calls[-1]["args"] == [outbound.to_payload()]
    assert module.celery_app.conf["task_routes"] == {
        "app.main.send_message_task": {"queue": "send_queue"},
        "app.main.process_message_task": {"queue": "celery"}}


@pytest.mark.parametrize("_case", [None], ids=["flow-66"])
def test_process_batch_ack_loss_after_claim_returns_explicit_resume_command(_case, task_api, processing_runtime):
    rt = processing_runtime
    command = rt.buffer()
    def lost():
        raise RuntimeError("synthetic lost acknowledgement")
    rt.store.client.after_operation["claim_or_resume_batch"] = lost
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    retry = caught.value.command
    assert retry.processing_id and retry.operation_id and retry.staging_id == command.batch_id
    rt.clock.advance(timedelta(seconds=45))
    assert task_api.process_batch(retry, rt) is task_api.ProcessingOutcome.PROCESSED


@pytest.mark.parametrize("_case", [None], ids=["flow-28"])
def test_process_batch_agent_failure_can_retry_only_same_staging_after_claim_expiry(_case, task_api, processing_runtime):
    rt = processing_runtime
    command = rt.buffer("primeiro lote")
    def failure():
        raise domain.AgentUnavailable(domain.FailureReason.AGENT_UNAVAILABLE)
    rt.agent.on_prepare = failure
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    retry = caught.value.command
    newer = rt.buffer("novo lote")
    rt.agent.on_prepare = None
    assert task_api.process_batch(retry, rt) is task_api.ProcessingOutcome.DUPLICATE
    rt.clock.advance(timedelta(seconds=45))
    assert task_api.process_batch(retry, rt) is task_api.ProcessingOutcome.PROCESSED
    assert [call[0] for call in rt.agent.calls] == ["primeiro lote", "primeiro lote"]
    assert [entry["content"] for entry in rt.envelopes()] == ["novo lote"]
    assert task_api.process_batch(newer, rt) is task_api.ProcessingOutcome.PROCESSED
    assert rt.agent.calls[-1][0] == "novo lote"


@pytest.mark.parametrize("boundary", ["stage_agent_result", "record_outbound_attempt"])
def test_process_batch_lost_ack_reuses_result_without_model_reentry(task_api, processing_runtime, boundary):
    rt = processing_runtime
    command = rt.buffer()
    def lost():
        raise RuntimeError("synthetic private ack loss")
    rt.store.client.after_operation[boundary] = lost
    with pytest.raises(task_api.RetryRequested) as caught:
        task_api.process_batch(command, rt)
    assert task_api.process_batch(caught.value.command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert len(rt.agent.calls) == 1
    assert sum(s.events.count("commit_entered") for s in rt.sessions) == 1


@pytest.mark.parametrize("fault", ["readiness", "lease", "generation", "session"])
@pytest.mark.parametrize("_case", [None], ids=["flow-22"])
def test_process_batch_dependency_failure_before_agent_has_no_patient_response(_case, task_api, processing_runtime, fault, monkeypatch):
    from app.conversation_redis import contact_keys
    rt = processing_runtime
    command = rt.buffer()
    leases, sessions = rt.lease_calls, rt.session_calls
    if fault == "readiness":
        rt.dependencies[domain.DependencyName.SQL] = False
    elif fault == "lease":
        rt.store.fail_next_atomic("acquire")
    elif fault == "generation":
        rt.store.client.values.pop(contact_keys(PHONE).generation, None)
    else:
        def failure():
            raise RuntimeError("synthetic-private-session-error")
        monkeypatch.setattr(rt, "session_factory", failure)
    with pytest.raises(task_api.RetryRequested):
        task_api.process_batch(command, rt)
    assert rt.agent.calls == rt.outbound_broker.calls == rt.transport.calls == []
    if fault == "readiness":
        assert (rt.lease_calls, rt.session_calls) == (leases, sessions)


def test_process_batch_mixed_fixed_and_text_keeps_fixed_inputs_out_of_agent(task_api, processing_runtime):
    rt = processing_runtime
    command = rt.buffer("Qual o horário?")
    rt.buffer("/pausar")
    rt.buffer("imagem", kind="media")
    assert task_api.process_batch(command, rt) is task_api.ProcessingOutcome.PROCESSED
    assert [call[0] for call in rt.agent.calls] == ["Qual o horário?"]
    text = rt.outbound_broker.calls[0].text
    assert "Para falar com a Beatriz, envie ATENDIMENTO." in text
    assert "Desculpe, não consigo receber imagem." in text
    assert "Resposta sintética" in text


@pytest.mark.parametrize("_case", [None], ids=["flow-29"])
def test_sender_runs_async_transport_inside_live_lease_and_fresh_session(_case, task_api, processing_runtime):
    rt = processing_runtime
    task_api.process_batch(rt.buffer(), rt)
    outbound = rt.outbound_broker.calls[-1]
    sessions = rt.session_calls
    original = rt.transport.send_message
    async def send(phone, text):
        with pytest.raises(domain.ContactLockUnavailable):
            rt.pause()
        return original(phone, text)
    rt.transport.send_message = send
    assert task_api.send_outbound(outbound, rt) is task_api.SendOutcome.SENT
    assert rt.session_calls == sessions + 1
    assert rt.transport.calls == [(PHONE, "Resposta sintética")]


@pytest.mark.parametrize("boundary", ["outbound", "processing"])
def test_task_celery_broker_exception_is_ambiguous_without_sensitive_error(main_module, processing_runtime, monkeypatch, boundary):
    from pathlib import Path
    import celery
    class FakeCelery:
        def __init__(self, *args, **kwargs):
            self.conf = {}
    monkeypatch.setattr(celery, "Celery", FakeCelery)
    spec = importlib.util.spec_from_file_location("synthetic_celery_error", Path(__file__).parents[1] / "app" / "celery_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def error(**kwargs):
        raise RuntimeError("synthetic-private-broker-error")
    task = SimpleNamespace(apply_async=error)
    command = processing_runtime.buffer()
    if boundary == "processing":
        result = module.CeleryProcessingBroker(task, probe=lambda: True).enqueue_processing(command)
    else:
        from uuid import uuid4
        result = module.CeleryOutboundBroker(task).enqueue_outbound(domain.OutboundEnvelope(
            PHONE, "synthetic", domain.OutboundKind.NORMAL, command.generation, str(uuid4()), str(uuid4())))
    assert result is domain.EnqueueResult.AMBIGUOUS


PHONE = "5551999990000"
OTHER_PHONE = "5551888880000"
CLINIC_INFO = {
    "nome_clinica": "Clínica sintética",
    "endereco": "Endereço sintético",
    "telefone": "Contato sintético",
    "horario_atendimento": {"segunda": "08:00-18:00"},
    "dias_fechados": ["25/12/2026"],
    "links": {
        "agendar": "https://clinic.synthetic.invalid/agendar/?origem=teste",
        "receita": "https://clinic.synthetic.invalid/receita/",
    },
}


def webhook(main, **payload_args):
    import asyncio
    return asyncio.run(main.whatsapp_webhook(WebhookRequest(main.app, webhook_payload(**payload_args))))


@pytest.mark.parametrize("jid,fields", [
    ("(51) 99999-0000", {}), (PHONE, {}), (PHONE + "@s.whatsapp.net", {}),
    (PHONE + "@c.us", {}), ("123456789012345@lid", {"cleanedSenderPn": PHONE}),
    ("123456789012345@lid", {"senderPn": PHONE + "@s.whatsapp.net"}),
    ("123456789012345@lid", {"senderPn": PHONE + "@c.us"}),
])
@pytest.mark.parametrize("nested", [True, False])
@pytest.mark.parametrize("_case", [None], ids=["flow-05"])
def test_webhook_identity_normalizes_once_to_one_canonical_lease(_case, main_module, ingress_runtime, monkeypatch, jid, fields, nested):
    normalized = []
    original = main_module.normalize_phone
    def normalize(raw):
        normalized.append(raw)
        return original(raw)
    monkeypatch.setattr(main_module, "normalize_phone", normalize)
    response = webhook(main_module, jid=jid, key_fields=fields, nested=nested)
    assert response.status_code == 200
    assert ingress_runtime.lease_calls == 1
    assert len(normalized) == 1
    assert len(ingress_runtime.processing_broker.calls) == 1
    assert ingress_runtime.processing_broker.calls[0].phone == PHONE
    assert ingress_runtime.envelopes()[0]["message_id"] == "synthetic-message-id"


@pytest.mark.parametrize("media", [
    pytest.param(None, id="flow-06-paused-text"),
    pytest.param("audioMessage", id="flow-07-paused-audio"),
    pytest.param("imageMessage", id="flow-07-paused-image"),
    pytest.param("videoMessage", id="flow-07-paused-video"),
    pytest.param("documentMessage", id="flow-07-paused-document"),
    pytest.param("stickerMessage", id="flow-07-paused-sticker"),
])
def test_webhook_paused_text_and_media_are_dropped_before_batch_and_remain_dropped(main_module, ingress_runtime, media):
    ref = ingress_runtime.pause()
    response = webhook(main_module, media=media, text="synthetic-discarded-content")
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ignored"}
    details = ingress_runtime.details()
    receipt = next(item for item in details if item.entry.kind == "dedupe")
    assert receipt.body["disposition"] == "DROPPED"
    assert receipt.entry.expected_until >= ref.paused_until + timedelta(days=7, seconds=300)
    assert not any(item.entry.kind in ("batch", "buffer", "staging") for item in details)
    serialized = json.dumps(ingress_runtime.store.snapshot(), default=str)
    assert "synthetic-discarded-content" not in serialized
    assert "synthetic-media-url" not in serialized
    assert "synthetic-message-id" not in serialized
    ingress_runtime.clock.set(ref.paused_until + timedelta(seconds=1))
    before = ingress_runtime.store.contact_snapshot(PHONE)
    assert ingress_runtime.processing_broker.calls == []
    response = webhook(main_module, media=media, text="synthetic-discarded-content")
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ignored"}
    assert ingress_runtime.store.contact_snapshot(PHONE) == before
    assert ingress_runtime.processing_broker.calls == []


@pytest.mark.parametrize("alias", ["/pausar", "/pause"])
@pytest.mark.parametrize("_case", [None], ids=["flow-12"])
def test_webhook_patient_pause_alias_buffers_fixed_help_only_while_active(_case, main_module, ingress_runtime, session_factory, alias):
    from app.models import PausedContact
    response = webhook(main_module, text=alias)
    assert response.status_code == 200
    envelope = ingress_runtime.envelopes()[0]
    assert envelope["kind"] == "pause_help"
    assert envelope["content"] == "Para falar com a Beatriz, envie ATENDIMENTO."
    with session_factory() as db:
        assert db.get(PausedContact, PHONE) is None
    ingress_runtime.pause()
    calls = len(ingress_runtime.processing_broker.calls)
    response = webhook(main_module, text=alias, message_id="paused-alias")
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ignored"}
    assert len(ingress_runtime.processing_broker.calls) == calls
    assert any(item.body.get("disposition") == "DROPPED" for item in ingress_runtime.details())


@pytest.mark.parametrize("alias", [
    pytest.param("/pausar", id="flow-13-secretary-pausar"),
    pytest.param("/pause", id="flow-45-secretary-pause-replay"),
])
def test_webhook_secretary_pause_renews_24h_but_duplicate_id_never_renews(main_module, ingress_runtime, session_factory, alias):
    from app.models import PausedContact
    start = ingress_runtime.clock.now()
    response = webhook(main_module, text=alias, from_me=True)
    assert response.status_code == 200
    assert ingress_runtime.lease_calls == 1
    details = ingress_runtime.details()
    receipt = next(item for item in details if item.entry.kind == "dedupe")
    assert receipt.body["disposition"] == "APPLIED"
    operation_id = receipt.body["operation_id"]
    assert sum(item.entry.kind == "mutation" for item in details) == 1
    ingress_runtime.clock.advance(timedelta(hours=1))
    response = webhook(main_module, text=alias, from_me=True)
    assert response.status_code == 200
    assert next(item for item in ingress_runtime.details() if item.entry.kind == "dedupe").body["operation_id"] == operation_id
    with session_factory() as db:
        assert db.get(PausedContact, PHONE).paused_until == (start + timedelta(hours=24)).replace(tzinfo=None)
    response = webhook(main_module, text=alias, from_me=True, message_id="renewed-command")
    assert response.status_code == 200
    with session_factory() as db:
        assert db.get(PausedContact, PHONE).paused_until == (start + timedelta(hours=25)).replace(tzinfo=None)
    assert ingress_runtime.processing_broker.calls == []


@pytest.mark.parametrize("_case", [None], ids=["flow-38"])
def test_webhook_secretary_pause_retries_same_prepared_operation(_case, main_module, ingress_runtime, monkeypatch, session_factory):
    from sqlalchemy.orm import Session
    from app.models import PausedContact
    original = Session.flush
    calls = []
    def fail_once(db, *args, **kwargs):
        if not calls and (db.new or db.dirty):
            calls.append(True)
            raise RuntimeError("synthetic-sensitive-sql-error")
        return original(db, *args, **kwargs)
    monkeypatch.setattr(Session, "flush", fail_once)
    response = webhook(main_module, text="/pause", from_me=True)
    assert response.status_code == 503
    details = ingress_runtime.details()
    receipt = next(item for item in details if item.entry.kind == "dedupe")
    operation = receipt.body["operation_id"]
    assert receipt.body["disposition"] is None
    with session_factory() as db:
        assert db.get(PausedContact, PHONE) is None
    ingress_runtime.clock.advance(timedelta(seconds=10))
    response = webhook(main_module, text="/pause", from_me=True)
    assert response.status_code == 200
    details = ingress_runtime.details()
    receipt = next(item for item in details if item.entry.kind == "dedupe")
    assert receipt.body["operation_id"] == operation
    assert receipt.body["disposition"] == "APPLIED"
    assert sum(item.entry.kind == "mutation" for item in details) == 1


@pytest.mark.parametrize("_case", [None], ids=["flow-54"])
def test_webhook_ordinary_origin_from_me_is_ignored_with_terminal_receipt(_case, main_module, ingress_runtime):
    response = webhook(main_module, text="Resposta da clínica", from_me=True)
    assert response.status_code == 200
    assert ingress_runtime.lease_calls == 1
    details = ingress_runtime.details()
    assert [item.body["disposition"] for item in details if item.entry.kind == "dedupe"] == ["IGNORED"]
    before = ingress_runtime.store.contact_snapshot(PHONE)
    response = webhook(main_module, text="Resposta da clínica", from_me=True)
    assert response.status_code == 200
    assert ingress_runtime.store.contact_snapshot(PHONE) == before
    assert ingress_runtime.processing_broker.calls == []


@pytest.mark.parametrize("media,label", [("audioMessage", "áudio"), ("imageMessage", "imagem"),
    ("videoMessage", "vídeo"), ("documentMessage", "documento"), ("stickerMessage", "figurinha")])
def test_webhook_active_media_buffers_only_required_content(main_module, ingress_runtime, media, label):
    response = webhook(main_module, media=media)
    assert response.status_code == 200
    envelope = ingress_runtime.envelopes()[0]
    assert envelope["kind"] == "media"
    assert envelope["content"] == label
    assert "synthetic-media-url" not in json.dumps(envelope)


@pytest.mark.parametrize("_case", [None], ids=["flow-10"])
def test_webhook_pause_expiry_at_exact_deadline_opens_new_generation(_case, main_module, ingress_runtime, session_factory):
    from app.models import PausedContact
    ref = ingress_runtime.pause()
    ingress_runtime.clock.set(ref.paused_until)
    response = webhook(main_module)
    assert response.status_code == 200
    envelope = ingress_runtime.envelopes()[0]
    assert envelope["generation"] != ref.generation
    with session_factory() as db:
        assert db.get(PausedContact, PHONE) is None


@pytest.mark.parametrize("failure", [
    pytest.param(domain.EnqueueResult.DEFINITIVE_FAILURE, id="flow-46-definitive-broker-failure"),
    pytest.param(domain.EnqueueResult.AMBIGUOUS, id="flow-76-ambiguous-reservation"),
])
def test_webhook_broker_failure_keeps_one_batch_and_replay_respects_due_time(main_module, ingress_runtime, failure):
    ingress_runtime.processing_broker.next_result = failure
    response = webhook(main_module)
    assert response.status_code == 503
    first = ingress_runtime.processing_broker.calls[0]
    assert len(ingress_runtime.envelopes()) == 1
    response = webhook(main_module)
    assert response.status_code == 503
    assert len(ingress_runtime.processing_broker.calls) == 1
    ingress_runtime.clock.advance(timedelta(seconds=60))
    ingress_runtime.processing_broker.next_result = domain.EnqueueResult.CONFIRMED
    response = webhook(main_module)
    assert response.status_code == 200
    assert ingress_runtime.processing_broker.calls == [first, first]
    assert len(ingress_runtime.envelopes()) == 1


@pytest.mark.parametrize("body_changes,terminal", [
    ({"disposition": "UNKNOWN_DISPOSITION"}, False),
    ({"schema": "unknown_schema"}, False),
    *[({"disposition": value}, False) for value in ("PROCESSED", "DROPPED", "APPLIED", "IGNORED", "FAILED")],
    ({}, True),
    ({"disposition": "DUPLICATE"}, False),
    ({"disposition": None}, False),
])
@pytest.mark.parametrize("bypass_shortcut", [False, True])
@pytest.mark.parametrize("_case", [None], ids=["flow-77"])
def test_webhook_invalid_retained_receipt_fails_closed_before_sql_or_dispatch(_case,
        main_module, ingress_runtime, session_factory, monkeypatch, body_changes, terminal, bypass_shortcut):
    from dataclasses import replace
    from sqlalchemy import event

    ingress_runtime.processing_broker.next_result = domain.EnqueueResult.AMBIGUOUS
    assert webhook(main_module).status_code == 503
    command = ingress_runtime.processing_broker.calls[0]
    with ingress_runtime.store.contact_lease(PHONE) as lease:
        assert ingress_runtime.store.dispatch(command, lease).phase is domain.DispatchPhase.PENDING
        anchor = ingress_runtime.store.read_anchor(lease)
        details = ingress_runtime.store.read_details(lease)
        altered = tuple(replace(item, entry=replace(item.entry, version=item.entry.version + 1),
                                body={**item.body, **body_changes}, terminal=terminal)
                        if item.entry.kind == "dedupe" else item for item in details)
        ingress_runtime.store.compare_and_set(lease, anchor, altered)
    if bypass_shortcut:
        # Exercise accept_ingress itself through the complete authenticated route.
        monkeypatch.setattr(ingress_runtime.coordinator, "is_terminal_ingress", lambda *args: False)
    before = ingress_runtime.store.snapshot()
    before_leases, before_sessions = ingress_runtime.lease_calls, ingress_runtime.session_calls
    before_operations = dict(ingress_runtime.store.client.operation_calls)
    sql_calls = []
    def record_sql(*args, **kwargs):
        sql_calls.append("unexpected_sql")
    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        response = webhook(main_module)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert response.status_code == 503
    assert json.loads(response.body) == {"status": "temporarily_unavailable"}
    assert ingress_runtime.session_calls == before_sessions + int(bypass_shortcut)
    assert sql_calls == []
    assert ingress_runtime.lease_calls == before_leases + 1
    assert ingress_runtime.processing_broker.calls == [command]
    assert ingress_runtime.store.snapshot() == before
    for operation in ("initialize", "finalize_ingress_once", "reserve_enqueue", "prepare_mutation"):
        assert ingress_runtime.store.client.operation_calls.get(operation, 0) == before_operations.get(operation, 0)


@pytest.mark.parametrize("corruption,value", [
    ("batch_id", "invalid-batch-id"), ("batch_id", None), ("batch_id", 123), ("batch_id", []),
    ("anchor_missing", None), ("anchor_malformed", None), ("detail_malformed", None),
    ("batch_relation", None),
])
@pytest.mark.parametrize("bypass_shortcut", [False, True])
@pytest.mark.parametrize("without_message_id", [False, True])
@pytest.mark.parametrize("_case", [None], ids=["flow-60"])
def test_webhook_snapshot_corruption_is_not_clean_absence_before_sql(_case,
        main_module, ingress_runtime, session_factory, monkeypatch, corruption, value, bypass_shortcut, without_message_id):
    from dataclasses import replace
    from sqlalchemy import event
    from app.conversation_redis import contact_keys

    ingress_runtime.processing_broker.next_result = domain.EnqueueResult.AMBIGUOUS
    assert webhook(main_module).status_code == 503
    command = ingress_runtime.processing_broker.calls[0]
    with ingress_runtime.store.contact_lease(PHONE) as lease:
        assert ingress_runtime.store.dispatch(command, lease).phase is domain.DispatchPhase.PENDING
        anchor = ingress_runtime.store.read_anchor(lease)
        details = ingress_runtime.store.read_details(lease)
        if corruption == "batch_id":
            altered = tuple(replace(item, entry=replace(item.entry, version=item.entry.version + 1),
                                    body={**item.body, "batch_id": value})
                            if item.entry.kind == "dedupe" else item for item in details)
            ingress_runtime.store.compare_and_set(lease, anchor, altered)
        elif corruption == "batch_relation":
            ingress_runtime.store.compare_and_set(lease, anchor, tuple(item for item in details if item.entry.kind != "buffer"))
        elif corruption == "anchor_missing":
            ingress_runtime.store.client.values.pop(contact_keys(PHONE).anchor)
        elif corruption == "anchor_malformed":
            ingress_runtime.store.client.values[contact_keys(PHONE).anchor] = "[]"
        else:
            receipt = next(item for item in details if item.entry.kind == "dedupe")
            ingress_runtime.store.client.values[ingress_runtime.store._detail_key(PHONE, receipt.entry)] = "not-json"
    if bypass_shortcut:
        monkeypatch.setattr(ingress_runtime.coordinator, "is_terminal_ingress", lambda *args: False)
    sessions, leases = ingress_runtime.session_calls, ingress_runtime.lease_calls
    operations = dict(ingress_runtime.store.client.operation_calls)
    sql_calls = []
    def record_sql(*args, **kwargs):
        sql_calls.append("statement")
    def forbidden_sql_effect(*args, **kwargs):
        sql_calls.append("flush_or_commit")
        raise AssertionError("corruption reached a SQL effect")
    monkeypatch.setattr(Session, "flush", forbidden_sql_effect)
    monkeypatch.setattr(Session, "commit", forbidden_sql_effect)
    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        response = webhook(main_module, message_id=None if without_message_id else "synthetic-message-id")
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert response.status_code == 503
    assert json.loads(response.body) == {"status": "temporarily_unavailable"}
    assert ingress_runtime.session_calls == sessions + int(bypass_shortcut)
    assert sql_calls == []
    assert ingress_runtime.lease_calls == leases + 1
    assert ingress_runtime.processing_broker.calls == [command]
    assert ingress_runtime.store.is_quarantined(PHONE)
    for operation in ("initialize", "finalize_ingress_once", "reserve_enqueue", "prepare_mutation"):
        assert ingress_runtime.store.client.operation_calls.get(operation, 0) == operations.get(operation, 0)


@pytest.mark.parametrize("_case", [None], ids=["flow-60"])
def test_webhook_virgin_clean_absence_keeps_sql_backed_initialization(_case, main_module, ingress_runtime, session_factory):
    from sqlalchemy import event

    before = ingress_runtime.store.snapshot()
    with ingress_runtime.store.contact_lease(PHONE) as lease:
        with pytest.raises(domain.ConversationGenerationUnavailable) as caught:
            ingress_runtime.store.read_details(lease)
        assert caught.value.reason_code is domain.FailureReason.GENERATION_UNAVAILABLE
        assert isinstance(caught.value, domain.ConversationCoordinationAbsent)
    assert ingress_runtime.store.snapshot() == before
    assert not ingress_runtime.store.is_quarantined(PHONE)
    sessions, leases = ingress_runtime.session_calls, ingress_runtime.lease_calls
    sql_calls = []
    def record_sql(connection, cursor, statement, *args):
        sql_calls.append(statement.split(None, 1)[0])
    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        response = webhook(main_module)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "buffered"}
    assert ingress_runtime.session_calls == sessions + 1
    assert ingress_runtime.lease_calls == leases + 1
    assert "SELECT" in sql_calls
    assert ingress_runtime.store.client.operation_calls["initialize"] == 1
    assert len(ingress_runtime.processing_broker.calls) == 1
    assert len(ingress_runtime.envelopes()) == 1


@pytest.mark.parametrize("_case", [None], ids=["flow-62"])
def test_webhook_replay_after_dispatch_deadline_is_terminal_without_rebuffer(_case, main_module, ingress_runtime):
    assert webhook(main_module).status_code == 200
    ingress_runtime.clock.advance(timedelta(seconds=900))
    response = webhook(main_module)
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ignored"}
    assert ingress_runtime.envelopes() == []
    assert len(ingress_runtime.processing_broker.calls) == 1
    with ingress_runtime.store.contact_lease(PHONE) as lease:
        claim = ingress_runtime.store.claim_or_resume_batch(ingress_runtime.processing_broker.calls[0], ingress_runtime.clock.now(), lease)
    assert claim.outcome is domain.ClaimOutcome.TERMINAL
    assert claim.envelopes == ()


@pytest.mark.parametrize("_case", [None], ids=["flow-37"])
def test_webhook_without_message_id_accepts_without_replay_guarantee(_case, main_module, ingress_runtime):
    assert webhook(main_module, message_id=None).status_code == 200
    assert webhook(main_module, message_id=None).status_code == 200
    assert len(ingress_runtime.envelopes()) == 2


@pytest.mark.parametrize("operation", ["acquire", "initialize", "finalize_ingress_once"])
@pytest.mark.parametrize("_case", [None], ids=["flow-21"])
def test_webhook_coordination_failure_is_503_without_legacy_fallback(_case, main_module, ingress_runtime, operation):
    ingress_runtime.store.fail_next_atomic(operation)
    response = webhook(main_module)
    assert response.status_code == 503
    assert json.loads(response.body) == {"status": "temporarily_unavailable"}
    assert ingress_runtime.processing_broker.calls == []


@pytest.mark.parametrize("message", [{"extendedTextMessage": {"text": "Texto estendido"}},
    {"imageMessage": {"caption": "Legenda", "url": "synthetic-media-url"}}])
def test_webhook_extended_text_and_image_caption_keep_existing_text_behavior(main_module, ingress_runtime, message):
    import asyncio
    payload = webhook_payload()
    payload["data"]["messages"]["message"] = message
    response = asyncio.run(main_module.whatsapp_webhook(WebhookRequest(main_module.app, payload)))
    assert response.status_code == 200
    envelope = ingress_runtime.envelopes()[0]
    assert envelope["kind"] == "text"
    assert envelope["content"] in ("Texto estendido", "Legenda")


def test_webhook_lease_loss_after_atomic_buffer_never_enqueues(main_module, ingress_runtime):
    from app.conversation_redis import contact_keys
    def lose(*args):
        ingress_runtime.store.client.values.pop(contact_keys(PHONE).lease, None)
    ingress_runtime.store.client.after_operation["finalize_ingress_once"] = lose
    response = webhook(main_module)
    assert response.status_code == 503
    assert ingress_runtime.processing_broker.calls == []


def test_webhook_secretary_ignored_does_not_expire_pause_or_open_closed_cycle(main_module, ingress_runtime):
    from uuid import uuid4
    with ingress_runtime.store.contact_lease(PHONE) as lease, ingress_runtime.session_factory() as db:
        ingress_runtime.coordinator.resolve_ingress(db, PHONE, ingress_runtime.clock.now(), lease)
        ref = ingress_runtime.coordinator.close_context(db, PHONE, ingress_runtime.clock.now(), lease, str(uuid4()))
    response = webhook(main_module, from_me=True)
    assert response.status_code == 200
    with ingress_runtime.store.contact_lease(PHONE) as lease:
        anchor = ingress_runtime.store.read_anchor(lease)
    assert str(anchor.last_generation) == ref.generation
    assert anchor.cycle is domain.ConversationCycle.CLOSED
    pause = ingress_runtime.pause()
    ingress_runtime.clock.set(pause.paused_until)
    response = webhook(main_module, from_me=True, message_id="ignored-after-pause")
    assert response.status_code == 200
    with ingress_runtime.store.contact_lease(PHONE) as lease:
        anchor = ingress_runtime.store.read_anchor(lease)
    assert str(anchor.last_generation) == pause.generation
    assert anchor.cycle is domain.ConversationCycle.PAUSED


def test_webhook_committing_command_retry_never_repeats_sql(main_module, ingress_runtime, session_factory):
    from app.models import PausedContact
    ingress_runtime.store.fail_next_atomic("finalize_committed")
    first = webhook(main_module, from_me=True, text="/pause")
    assert first.status_code == 503
    with session_factory() as db:
        deadline = db.get(PausedContact, PHONE).paused_until
    ingress_runtime.clock.advance(timedelta(seconds=10))
    second = webhook(main_module, from_me=True, text="/pause")
    assert second.status_code == 503
    with session_factory() as db:
        assert db.get(PausedContact, PHONE).paused_until == deadline
    details = ingress_runtime.details()
    assert sum(item.entry.kind == "mutation" for item in details) == 1
    assert next(item.body for item in details if item.entry.kind == "dedupe")["disposition"] is None


def test_webhook_pause_does_not_block_other_canonical_contact(main_module, ingress_runtime):
    ingress_runtime.pause()
    assert webhook(main_module, jid=OTHER_PHONE).status_code == 200
    assert ingress_runtime.processing_broker.calls[0].phone == OTHER_PHONE
    assert ingress_runtime.envelopes(PHONE) == []


@pytest.mark.parametrize("_case", [None], ids=["flow-71"])
def test_webhook_staged_replay_uses_processing_deadline_not_old_dispatch_deadline(_case, main_module, ingress_runtime):
    assert webhook(main_module).status_code == 200
    command = ingress_runtime.processing_broker.calls[0]
    ingress_runtime.clock.advance(timedelta(seconds=850))
    with ingress_runtime.store.contact_lease(PHONE) as lease:
        claim = ingress_runtime.store.claim_or_resume_batch(command, ingress_runtime.clock.now(), lease)
    assert claim.outcome is domain.ClaimOutcome.CLAIMED
    ingress_runtime.clock.advance(timedelta(seconds=50))
    assert webhook(main_module).status_code == 200
    with ingress_runtime.store.contact_lease(PHONE) as lease:
        dispatch = ingress_runtime.store.dispatch(command, lease)
    assert dispatch.phase is domain.DispatchPhase.STAGED
    assert len(ingress_runtime.envelopes()) == 1


def test_webhook_lease_counter_includes_setup_and_every_later_acquisition(main_module, ingress_runtime):
    assert ingress_runtime.lease_calls == 0
    ingress_runtime.pause()
    assert ingress_runtime.lease_calls == 1
    ingress_runtime.details()
    assert ingress_runtime.lease_calls == 2
    assert webhook(main_module).status_code == 200
    assert ingress_runtime.lease_calls == 3
    assert webhook(main_module).status_code == 200
    assert ingress_runtime.lease_calls == 4
    assert webhook(main_module, jid=OTHER_PHONE).status_code == 200
    assert ingress_runtime.lease_calls == 5


def test_webhook_lease_counter_does_not_consume_or_repeat_one_shot_fault_hooks(main_module, ingress_runtime):
    hooks = []
    ingress_runtime.store.client.before_operation["acquire"] = lambda: hooks.append("called")
    assert webhook(main_module).status_code == 200
    assert webhook(main_module, message_id="second-message").status_code == 200
    assert hooks == ["called"]
    assert ingress_runtime.lease_calls == 2


@pytest.mark.parametrize("disposition", ["DROPPED", "APPLIED", "IGNORED", "FAILED", "PROCESSED"])
@pytest.mark.parametrize("bypass_shortcut", [False, True])
def test_webhook_terminal_replay_reads_no_sql_or_session_and_mutates_no_state(
        main_module, ingress_runtime, session_factory, monkeypatch, disposition, bypass_shortcut):
    from sqlalchemy import event
    options = {}
    if disposition == "DROPPED":
        ingress_runtime.pause()
    elif disposition in ("APPLIED", "IGNORED"):
        options["from_me"] = True
        if disposition == "APPLIED":
            options["text"] = "/pause"
    assert webhook(main_module, **options).status_code == 200
    if disposition in ("FAILED", "PROCESSED"):
        command = ingress_runtime.processing_broker.calls[0]
        if disposition == "FAILED":
            ingress_runtime.clock.advance(timedelta(seconds=900))
            with ingress_runtime.store.contact_lease(PHONE) as lease:
                ingress_runtime.store.exhaust_batch(command, ingress_runtime.clock.now(), lease)
        else:
            with ingress_runtime.store.contact_lease(PHONE) as lease, session_factory() as db:
                claim = ingress_runtime.store.claim_or_resume_batch(command, ingress_runtime.clock.now(), lease)
                result = domain.AgentResult("Resposta sintética", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
                ingress_runtime.store.stage_agent_result(command, claim.attempt, result, ingress_runtime.clock.now(), lease)
                ingress_runtime.coordinator.apply_agent_result(db, PHONE, result, claim.attempt.processing_id,
                    claim.attempt.operation_id, ingress_runtime.clock.now(), lease)
                reservation = ingress_runtime.store.reserve_outbound_enqueue(command, claim.attempt, ingress_runtime.clock.now(), lease)
                ingress_runtime.store.record_outbound_attempt(command, claim.attempt, ingress_runtime.clock.now(), lease, reservation=reservation)
                ingress_runtime.store.complete_batch(command, claim.attempt, ingress_runtime.clock.now(), lease, reservation=reservation)
    receipts = [item for item in ingress_runtime.details() if item.entry.kind == "dedupe"]
    assert len(receipts) == 1 and receipts[0].body["disposition"] == disposition and receipts[0].terminal
    before = ingress_runtime.store.snapshot()
    before_leases = ingress_runtime.lease_calls
    before_sessions = ingress_runtime.session_calls
    before_broker = list(ingress_runtime.processing_broker.calls)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append("unexpected_boundary")
        raise AssertionError("terminal replay crossed effect boundary")
    if bypass_shortcut:
        monkeypatch.setattr(ingress_runtime.coordinator, "is_terminal_ingress", lambda *args: False)
    else:
        monkeypatch.setattr(ingress_runtime, "session_factory", forbidden)
    for target, method in ((ingress_runtime.coordinator, "_ensure"),
                           (ingress_runtime.coordinator, "resolve_ingress"), (ingress_runtime.store, "ensure_consumer"),
                           (ingress_runtime.store, "finalize_ingress_once")):
        monkeypatch.setattr(target, method, forbidden)
    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", forbidden)
    try:
        response = webhook(main_module, **options)
    finally:
        event.remove(engine, "before_cursor_execute", forbidden)
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ignored"}
    assert calls == []
    assert ingress_runtime.session_calls == before_sessions + int(bypass_shortcut)
    assert ingress_runtime.lease_calls == before_leases + 1
    assert ingress_runtime.store.snapshot() == before
    assert ingress_runtime.processing_broker.calls == before_broker


@pytest.mark.parametrize("fault", ["epoch_absent", "epoch_mismatch", "generation", "fingerprint", "manifest", "missing_receipt"])
def test_webhook_terminal_replay_does_not_bypass_invalid_coordination(main_module, ingress_runtime, fault):
    assert webhook(main_module, from_me=True).status_code == 200
    if fault.startswith("epoch_"):
        ingress_runtime.store.inject_fault(fault)
    elif fault == "missing_receipt":
        receipt = next(item for item in ingress_runtime.details() if item.entry.kind == "dedupe")
        ingress_runtime.store.delete_detail(PHONE, receipt.entry)
    else:
        ingress_runtime.store.corrupt_contact(PHONE, fault)
    sessions = ingress_runtime.session_calls
    response = webhook(main_module, from_me=True)
    assert response.status_code == 503
    assert ingress_runtime.session_calls == sessions
    assert ingress_runtime.processing_broker.calls == []


def test_webhook_expired_terminal_receipt_does_not_suppress_new_ingress(main_module, ingress_runtime):
    assert webhook(main_module, from_me=True).status_code == 200
    receipt = next(item for item in ingress_runtime.details() if item.entry.kind == "dedupe")
    ingress_runtime.clock.set(receipt.entry.expected_until)
    sessions = ingress_runtime.session_calls
    response = webhook(main_module)
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "buffered"}
    assert ingress_runtime.session_calls == sessions + 1
    assert len(ingress_runtime.envelopes()) == 1


def test_webhook_no_matching_retained_receipt_accepts_new_message(main_module, ingress_runtime):
    assert webhook(main_module, from_me=True).status_code == 200
    sessions = ingress_runtime.session_calls
    response = webhook(main_module, message_id="new-synthetic-message")
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "buffered"}
    assert ingress_runtime.session_calls == sessions + 1
    assert len(ingress_runtime.envelopes()) == 1
    assert len(ingress_runtime.processing_broker.calls) == 1


@pytest.fixture
def agent_module(monkeypatch):
    effects = ForbiddenAgentEffects()
    monkeypatch.setattr(socket.socket, "connect", effects.boundary("network"))
    for method in ("query", "execute", "add", "delete", "flush", "commit", "rollback"):
        monkeypatch.setattr(Session, method, effects.boundary("db"))
    for method in ("readiness", "contact_lease", "stage_agent_result"):
        monkeypatch.setattr(RedisConversationStore, method, effects.boundary("store"))
    transport = SimpleNamespace(send_message=effects.boundary("whatsapp"))
    monkeypatch.setitem(sys.modules, "app.whatsapp_service", SimpleNamespace(
        whatsapp_service=transport,
        WhatsAppService=effects.boundary("whatsapp_constructor"),
    ))

    # The production singleton is imported only with synthetic dependencies.
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: ScriptedClaude())
    monkeypatch.setattr(utils, "load_clinic_info", lambda: deepcopy(CLINIC_INFO))
    module = importlib.import_module("app.ai_agent")
    monkeypatch.setattr(module, "Anthropic", effects.boundary("claude_constructor"))
    monkeypatch.setattr(module, "load_clinic_info", effects.boundary("clinic_file"))
    yield module
    assert effects.calls == []


@pytest.fixture
def clock():
    # Monday 09:00 in Sao Paulo; the machine's current time is irrelevant.
    return ManualClock(datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))


@pytest.fixture
def snapshot(clock):
    return domain.ConversationSnapshot(
        phone=PHONE,
        messages=[
            {"role": "user", "content": "Pergunta sintética", "timestamp": "2026-09-14T11:00:00+00:00"},
            {"role": "assistant", "content": "Resposta anterior", "timestamp": "2026-09-14T11:00:01+00:00"},
        ],
        current_flow="duvidas",
        flow_data={"nested": {"choices": ["synthetic"]}},
        status="active",
        last_activity=clock.now() - timedelta(minutes=5),
    )


def make_agent(module, clock, client, clinic_info=None):
    return module.ClaudeToolAgent(
        client=client,
        clinic_info=deepcopy(CLINIC_INFO) if clinic_info is None else clinic_info,
        clock=clock.now,
    )


def test_agent_normal_text_returns_independent_complete_context(agent_module, clock, snapshot):
    client = ScriptedClaude()
    client.respond_with_text("Resposta sintética")
    agent = make_agent(agent_module, clock, client)
    original = deepcopy(snapshot)

    result = agent.prepare_result("Qual o horário?", PHONE, snapshot)

    assert isinstance(result, domain.AgentResult)
    assert result.intent is domain.AgentIntent.SAVE_CONTEXT
    assert result.text == "Resposta sintética"
    assert result.messages == original.messages + [
        {"role": "user", "content": "Qual o horário?", "timestamp": "2026-09-14T12:00:00+00:00"},
        {"role": "assistant", "content": "Resposta sintética", "timestamp": "2026-09-14T12:00:00+00:00"},
    ]
    assert result.current_flow == "duvidas"
    assert result.flow_data == {"nested": {"choices": ["synthetic"]}}
    assert snapshot == original
    result.messages[0]["content"] = "changed result"
    result.flow_data["nested"]["choices"].append("changed result")
    assert snapshot == original
    assert all(set(item) == {"role", "content"} for item in client.calls[0]["messages"])
    assert f"?origem=teste&tel={PHONE}" in client.calls[0]["system"][0]["text"]
    assert f"/receita/?tel={PHONE}" in client.calls[0]["system"][0]["text"]


def test_agent_accepts_explicit_empty_clinic_info_without_file_loading(agent_module, clock, snapshot):
    client = ScriptedClaude()
    client.respond_with_text("Resposta")
    agent = make_agent(agent_module, clock, client, clinic_info={})
    assert agent.prepare_result("Olá", PHONE, snapshot).text == "Resposta"
    assert agent.clinic_info == {}


def test_agent_text_alone_does_not_authorize_a_transition(agent_module, clock, snapshot):
    client = ScriptedClaude()
    client.respond_with_text("Vou transferir você para Beatriz. Até logo!")
    result = make_agent(agent_module, clock, client).prepare_result("Olá", PHONE, snapshot)
    assert result.intent is domain.AgentIntent.SAVE_CONTEXT


def test_agent_farewell_closes_before_claude_without_resaving_context(agent_module, clock, snapshot):
    snapshot.messages[-1]["content"] = "Posso ajudar com mais alguma coisa?"
    original = deepcopy(snapshot)
    client = ScriptedClaude()

    result = make_agent(agent_module, clock, client).prepare_result("Não, obrigado", PHONE, snapshot)

    assert result.intent is domain.AgentIntent.CLOSE_CONTEXT
    assert result.text == "Foi um prazer atender você! Até logo!"
    assert result.messages == []
    assert result.current_flow is None
    assert result.flow_data == {}
    assert snapshot == original
    assert client.calls == []


def test_agent_farewell_keeps_existing_recognition_scope(agent_module, clock, snapshot):
    client = ScriptedClaude()
    client.respond_with_text("Disponha")
    result = make_agent(agent_module, clock, client).prepare_result("Obrigado", PHONE, snapshot)
    assert result.intent is domain.AgentIntent.SAVE_CONTEXT
    assert len(client.calls) == 1


@pytest.mark.parametrize("closed", [False, True])
def test_agent_human_intent_is_terminal_and_uses_injected_clock(agent_module, clock, snapshot, closed):
    if closed:
        clock.advance(timedelta(hours=12))
    original = deepcopy(snapshot)
    client = ScriptedClaude()
    client.respond_with_tool("request_human_assistance")

    result = make_agent(agent_module, clock, client).prepare_result("Preciso da secretária", PHONE, snapshot)

    assert result.intent is domain.AgentIntent.PAUSE_FOR_SECRETARY
    assert "Beatriz" in result.text or "nossa secretária" in result.text
    assert "equipe" not in result.text.lower()
    assert ("fora do horário" in result.text) is closed
    assert result.messages == []
    assert result.current_flow is None
    assert result.flow_data == {}
    assert snapshot == original
    assert len(client.calls) == 1


@pytest.mark.parametrize("tool_name,intent", [
    ("request_human_assistance", domain.AgentIntent.PAUSE_FOR_SECRETARY),
    ("end_conversation", domain.AgentIntent.CLOSE_CONTEXT),
])
def test_agent_mixed_text_and_tool_preserves_transition(agent_module, clock, snapshot, tool_name, intent):
    client = ScriptedClaude()
    client.respond(
        TextBlock(type="text", text="Texto antes da tool"),
        ToolUseBlock(type="tool_use", id="tool_terminal", name=tool_name, input={}),
    )
    result = make_agent(agent_module, clock, client).prepare_result("Solicitação", PHONE, snapshot)
    assert result.intent is intent
    assert result.text != "Texto antes da tool"
    assert result.messages == []
    assert result.flow_data == {}
    assert len(client.calls) == 1


@pytest.mark.parametrize("tool_name,intent", [
    ("get_clinic_info", None),
    ("request_human_assistance", domain.AgentIntent.PAUSE_FOR_SECRETARY),
    ("end_conversation", domain.AgentIntent.CLOSE_CONTEXT),
])
def test_agent_tool_outcomes_are_typed_and_have_no_effects(agent_module, clock, tool_name, intent):
    client = ScriptedClaude()
    outcome = make_agent(agent_module, clock, client)._execute_tool(tool_name, {}, PHONE)
    assert isinstance(outcome, domain.ToolOutcome)
    assert outcome.intent is intent
    assert isinstance(outcome.content, str) and outcome.content
    if tool_name == "get_clinic_info":
        assert "Clínica sintética" in outcome.content
        assert "Endereço sintético" in outcome.content
        assert "25/12/2026" in outcome.content
    assert client.calls == []


def test_agent_tool_loop_accumulates_protocol_and_preserves_personalized_links(agent_module, clock, snapshot):
    client = ScriptedClaude()
    for number in range(1, 4):
        client.respond_with_tool("get_clinic_info", tool_id=f"tool_{number}")
    client.respond_with_text("Informações confirmadas")
    original = deepcopy(snapshot)

    result = make_agent(agent_module, clock, client).prepare_result("Consulte os dados", PHONE, snapshot)

    assert result.intent is domain.AgentIntent.SAVE_CONTEXT
    assert result.text == "Informações confirmadas"
    assert len(client.calls) == 4  # Initial response plus three tool continuations.
    for number, call in enumerate(client.calls):
        assert call["system"] == client.calls[0]["system"]
        assert call["tools"] == client.calls[0]["tools"]
        assert len(call["messages"]) == 3 + number * 2
        tool_results = [block for entry in call["messages"] if isinstance(entry["content"], list)
                        for block in entry["content"] if block["type"] == "tool_result"]
        assert [block["tool_use_id"] for block in tool_results] == [f"tool_{i}" for i in range(1, number + 1)]
        assert all("25/12/2026" in block["content"] for block in tool_results)
        json.dumps(call["messages"])
    assert len(result.messages) == 4
    assert snapshot == original


def test_agent_fourth_tool_round_raises_without_executing_it(agent_module, clock, snapshot):
    client = ScriptedClaude()
    for number in range(4):
        client.respond_with_tool("get_clinic_info", tool_id=f"tool_{number}")
    agent = make_agent(agent_module, clock, client)
    original = deepcopy(snapshot)

    with pytest.raises(domain.AgentResponseInvalid) as caught:
        agent.prepare_result("Consulte os dados", PHONE, snapshot)

    assert caught.value.reason_code is domain.FailureReason.TOOL_ITERATION_LIMIT
    assert len(client.calls) == 4
    assert snapshot == original


def test_agent_multiple_info_tools_are_answered_before_continuation(agent_module, clock, snapshot):
    client = ScriptedClaude()
    client.respond(
        TextBlock(type="text", text="Vou consultar"),
        ToolUseBlock(type="tool_use", id="tool_a", name="get_clinic_info", input={}),
        ToolUseBlock(type="tool_use", id="tool_b", name="get_clinic_info", input={}),
    )
    client.respond_with_text("Pronto")
    result = make_agent(agent_module, clock, client).prepare_result("Consulte", PHONE, snapshot)
    assert result.text == "Pronto"
    assert [block["tool_use_id"] for block in client.calls[1]["messages"][-1]["content"]] == ["tool_a", "tool_b"]


def test_agent_conflicting_terminal_intents_fail_closed(agent_module, clock, snapshot):
    client = ScriptedClaude()
    client.respond(
        ToolUseBlock(type="tool_use", id="tool_a", name="request_human_assistance", input={}),
        ToolUseBlock(type="tool_use", id="tool_b", name="end_conversation", input={}),
    )
    with pytest.raises(domain.AgentResponseInvalid):
        make_agent(agent_module, clock, client).prepare_result("Solicitação", PHONE, snapshot)
    assert len(client.calls) == 1


def test_agent_multiple_text_blocks_form_the_complete_answer(agent_module, clock, snapshot):
    client = ScriptedClaude()
    client.respond(TextBlock(type="text", text="Primeira parte"), TextBlock(type="text", text="Segunda parte"))
    result = make_agent(agent_module, clock, client).prepare_result("Pergunta", PHONE, snapshot)
    assert result.text == "Primeira parte\nSegunda parte"
    assert result.messages[-1]["content"] == result.text


@pytest.mark.parametrize("response", [None, SimpleNamespace(content=[]), SimpleNamespace(content=[SimpleNamespace(type="unknown")])])
def test_agent_invalid_response_is_typed_not_patient_apology(agent_module, clock, snapshot, response):
    client = ScriptedClaude()
    client.responses.append(response)
    with pytest.raises(domain.AgentResponseInvalid):
        make_agent(agent_module, clock, client).prepare_result("Pergunta", PHONE, snapshot)


def test_agent_claude_exception_is_sanitized_without_patient_output(agent_module, clock, snapshot, caplog):
    client = ScriptedClaude()
    sentinel = "synthetic-private-provider-error"
    client.responses.append(RuntimeError(sentinel))
    original = deepcopy(snapshot)
    caplog.set_level(logging.DEBUG, logger="app.ai_agent")

    with pytest.raises(domain.AgentUnavailable) as caught:
        make_agent(agent_module, clock, client).prepare_result("Pergunta", PHONE, snapshot)

    assert caught.value.reason_code is domain.FailureReason.AGENT_UNAVAILABLE
    assert sentinel not in str(caught.value)
    assert sentinel not in "".join(traceback.format_exception(caught.value))
    assert sentinel not in caplog.text
    assert snapshot == original


def test_agent_authority_exception_propagates_unchanged(agent_module, clock, snapshot):
    client = ScriptedClaude()
    failure = domain.ContactLeaseLost(domain.FailureReason.CONTACT_LEASE_LOST)
    client.responses.append(failure)
    with pytest.raises(domain.ContactLeaseLost) as caught:
        make_agent(agent_module, clock, client).prepare_result("Pergunta", PHONE, snapshot)
    assert caught.value is failure


@pytest.mark.parametrize("tool_name,tool_input", [
    ("synthetic-unknown-tool", {}),
    ("get_clinic_info", {"synthetic-private-input": "invalid"}),
    ("get_clinic_info", []),
])
def test_agent_invalid_tool_is_typed_and_sanitized(agent_module, clock, tool_name, tool_input, caplog):
    caplog.set_level(logging.DEBUG, logger="app.ai_agent")
    with pytest.raises(domain.AgentToolUnavailable):
        make_agent(agent_module, clock, ScriptedClaude())._execute_tool(tool_name, tool_input, PHONE)
    assert "synthetic-unknown-tool" not in caplog.text
    assert "synthetic-private-input" not in caplog.text


def test_agent_tool_dependency_failure_is_sanitized(agent_module, clock, caplog):
    info = deepcopy(CLINIC_INFO)
    info["dias_fechados"] = ["synthetic-private-invalid-date"]
    caplog.set_level(logging.DEBUG, logger="app.ai_agent")
    with pytest.raises(domain.AgentToolUnavailable) as caught:
        make_agent(agent_module, clock, ScriptedClaude(), info)._execute_tool("get_clinic_info", {}, PHONE)
    assert "synthetic-private-invalid-date" not in str(caught.value)
    assert "synthetic-private-invalid-date" not in "".join(traceback.format_exception(caught.value))
    assert "synthetic-private-invalid-date" not in caplog.text


@pytest.mark.parametrize("phone", ["", "123", "(51) 99999-0000", "5551999990000@s.whatsapp.net"])
def test_agent_rejects_noncanonical_identity_before_claude(agent_module, clock, snapshot, phone):
    client = ScriptedClaude()
    with pytest.raises(domain.InvalidCanonicalContact):
        make_agent(agent_module, clock, client).prepare_result("Pergunta", phone, snapshot)
    assert client.calls == []


def test_agent_rejects_snapshot_from_another_contact(agent_module, clock, snapshot):
    client = ScriptedClaude()
    with pytest.raises(domain.InvalidCanonicalContact):
        make_agent(agent_module, clock, client).prepare_result("Pergunta", OTHER_PHONE, snapshot)
    assert client.calls == []


def test_agent_model_cannot_mutate_the_input_snapshot(agent_module, clock, snapshot):
    snapshot.messages[0]["content"] = [{"type": "text", "text": "nested synthetic text"}]
    original = deepcopy(snapshot)
    client = ScriptedClaude()
    client.respond_with_text("Resposta")
    client.on_create = lambda request: request["messages"][0]["content"][0].update(text="changed by client")
    result = make_agent(agent_module, clock, client).prepare_result("Pergunta", PHONE, snapshot)
    assert snapshot == original
    assert result.messages[0] == original.messages[0]
