"""Task 2 contracts; all coordination runs against deterministic script semantics."""

from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Thread
from uuid import UUID
import json

import pytest

from app.conversation_state import (
    ConfigurationIssue, ContactLeaseLost, ContactLockUnavailable, ConversationGenerationUnavailable,
    ConversationStateUnavailable, ReadinessUnavailable,
)

PHONE = "5551999990000"
OTHER = "5551888880000"

from tests.test_conversation_state import transition_env, seed_context


def make_store():
    from tests.fakes import InMemoryConversationStore
    from tests.test_conversation_state import _valid_environment
    from app.conversation_state import ConversationConfig
    from app.simple_config import Settings
    return InMemoryConversationStore(ConversationConfig.from_settings(Settings(_valid_environment())))


@pytest.mark.parametrize("offset,winner", [(-1, False), (0, True), (1, True)])
def test_claim_takeover_exact_deadline_rejects_old_token(offset, winner):
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    domain, store = batch_api(), make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        old = store.claim_or_resume_batch(command, store.clock.now(), lease)
    store.clock.set(old.attempt.claim_deadline + timedelta(microseconds=offset))
    with store.contact_lease(PHONE) as lease:
        claim = store.claim_or_resume_batch(command, store.clock.now(), lease)
        assert claim.outcome.value == ("CLAIMED" if winner else "DUPLICATE")
        if winner:
            result = domain.AgentResult("synthetic result", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
            with pytest.raises(domain.ConversationMutationPending):
                store.stage_agent_result(command, old.attempt, result, store.clock.now(), lease)
            store.stage_agent_result(command, claim.attempt, result, store.clock.now(), lease)


@pytest.mark.parametrize("kind", ["processing", "staging", "batch", "dedupe", "staging_index"])
def test_staged_missing_detail_or_membership_quarantines_before_new_claim(kind):
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    batch_api()
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        store.claim_or_resume_batch(command, store.clock.now(), lease)
        if kind == "staging_index":
            store.corrupt_contact(PHONE, "staging")
        else:
            store.delete_detail(PHONE, batch_details(store, lease, kind)[0].entry)
        with pytest.raises(ConversationGenerationUnavailable):
            store.claim_or_resume_batch(command, store.clock.now(), lease)
        assert store.is_quarantined(PHONE)
        assert "synthetic text" not in str(store.contact_snapshot(PHONE))


@pytest.mark.parametrize("operation", ["finalize_ingress_once", "claim_or_resume_batch", "stage_agent_result", "exhaust_batch"])
def test_batch_atomic_fault_at_each_write_preserves_all_details_and_manifest(operation):
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    domain = batch_api()
    def prepared():
        store = make_store()
        context = store.contact_lease(PHONE)
        lease = context.__enter__()
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = attempt = None
        if operation != "finalize_ingress_once":
            command = batch_command(store, lease, append_batch(store, lease))
        if operation == "stage_agent_result":
            attempt = store.claim_or_resume_batch(command, store.clock.now(), lease).attempt
        if operation == "exhaust_batch":
            deadline = store.dispatch(command, lease).dispatch_deadline
            from app.conversation_redis import contact_keys
            store.client.expiry[contact_keys(PHONE).lease] = (deadline + timedelta(seconds=60)).timestamp()
            store.clock.set(deadline)
        def invoke():
            if operation == "finalize_ingress_once":
                return append_batch(store, lease)
            if operation == "claim_or_resume_batch":
                return store.claim_or_resume_batch(command, store.clock.now(), lease)
            if operation == "stage_agent_result":
                result = domain.AgentResult("synthetic output", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
                return store.stage_agent_result(command, attempt, result, store.clock.now(), lease)
            return store.exhaust_batch(command, store.clock.now(), lease)
        return store, context, invoke
    store, context, invoke = prepared()
    try:
        invoke()
        count = store.client.write_counts[operation]
        assert count >= 4
    finally:
        context.__exit__(None, None, None)
    for index in range(count):
        store, context, invoke = prepared()
        try:
            before = store.snapshot()
            store.client.fail_write_at = (operation, index)
            with pytest.raises(ConversationStateUnavailable):
                invoke()
            assert store.snapshot() == before
            invoke()  # intact state remains retryable
        finally:
            context.__exit__(None, None, None)


def test_batch_two_consumers_observe_only_one_live_claim():
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    batch_api()
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
    barrier, outcomes = Barrier(2), []
    def consume():
        barrier.wait(timeout=2)
        try:
            with store.contact_lease(PHONE) as lease:
                outcomes.append(store.claim_or_resume_batch(command, store.clock.now(), lease).outcome.value)
        except ContactLockUnavailable:
            outcomes.append("LOCKED")
    threads = [Thread(target=consume), Thread(target=consume)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert outcomes.count("CLAIMED") == 1
    assert set(outcomes) <= {"CLAIMED", "DUPLICATE", "LOCKED"}


@pytest.mark.parametrize("boundary", ["prepare_mutation", "enter_committing"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_batch_mutation_processing_deadline_aborts_before_sql_commit(transition_env, boundary, offset):
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    from app.conversation_redis import contact_keys
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, clock.now(), lease)
        result = domain.AgentResult("synthetic result", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(command, claim.attempt, result, clock.now(), lease)
        due = claim.attempt.processing_deadline + timedelta(microseconds=offset)
        store.client.expiry[contact_keys(PHONE).lease] = (due + timedelta(seconds=60)).timestamp()
        store.client.before_operation[boundary] = lambda: clock.set(due)
        if offset == -1:
            coordinator.apply_agent_result(db, PHONE, result, claim.attempt.processing_id,
                                           claim.attempt.operation_id, clock.now(), lease)
            assert db.events.count("commit_entered") == 1
            assert store.inspect_mutation(PHONE, claim.attempt.operation_id, lease).phase is domain.MutationPhase.COMMITTED
            return
        with pytest.raises(domain.ConversationMutationPending):
            coordinator.apply_agent_result(db, PHONE, result, claim.attempt.processing_id,
                                           claim.attempt.operation_id, clock.now(), lease)
        assert "commit_entered" not in db.events
        if boundary == "prepare_mutation":
            assert "flush" not in db.events
        else:
            assert "rollback" in db.events
            assert store.inspect_mutation(PHONE, claim.attempt.operation_id, lease).phase is domain.MutationPhase.ABORTED
        assert store.dispatch(command, lease).phase is domain.DispatchPhase.EXHAUSTED
        assert batch_details(store, lease, "dedupe")[0].body["disposition"] == "FAILED"


def test_batch_committing_death_quarantines_content_without_repeating_mutation(transition_env):
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, clock.now(), lease)
        result = domain.AgentResult("synthetic output", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(command, claim.attempt, result, clock.now(), lease)
        store.fail_next_atomic("finalize_committed")
        with pytest.raises(domain.ConversationMutationAmbiguous):
            coordinator.apply_agent_result(db, PHONE, result, claim.attempt.processing_id,
                                           claim.attempt.operation_id, clock.now(), lease)
        with pytest.raises(domain.ConversationMutationPending):
            store.claim_or_resume_batch(command, clock.now(), lease)
    clock.set(claim.attempt.processing_deadline)
    with store.contact_lease(PHONE) as lease:
        with pytest.raises(domain.ConversationMutationPending):
            store.exhaust_batch(command, clock.now(), lease)
        assert store.is_quarantined(PHONE)
        assert "synthetic output" not in str(store.contact_snapshot(PHONE))
        assert "synthetic text" not in str(store.contact_snapshot(PHONE))


@pytest.mark.parametrize("intent", ["SAVE_CONTEXT", "PAUSE_FOR_SECRETARY", "CLOSE_CONTEXT"])
def test_batch_complete_atomic_failure_retries_without_losing_sql_receipt(transition_env, intent):
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, clock.now(), lease)
        result = domain.AgentResult("synthetic result", [], None, {}, domain.AgentIntent(intent))
        store.stage_agent_result(command, claim.attempt, result, clock.now(), lease)
        coordinator.apply_agent_result(db, PHONE, result, claim.attempt.processing_id,
                                       claim.attempt.operation_id, clock.now(), lease)
        snapshot = store.snapshot()
        store.fail_next_atomic("complete_batch")
        with pytest.raises(ConversationStateUnavailable):
            store.complete_batch(command, claim.attempt, clock.now(), lease)
        assert store.snapshot() == snapshot
        assert store.inspect_mutation(PHONE, claim.attempt.operation_id, lease).phase is domain.MutationPhase.COMMITTED
        store.complete_batch(command, claim.attempt, clock.now(), lease)
        assert batch_details(store, lease, "dedupe")[0].body["disposition"] == "PROCESSED"


@pytest.mark.parametrize("boundary", ["reserve_enqueue", "finish_enqueue"])
def test_enqueue_atomic_failure_never_calls_broker_without_a_reservation(boundary):
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    from tests.fakes import ScriptedBroker
    domain, store, broker = batch_api(), make_store(), ScriptedBroker()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        before = store.snapshot()
        store.fail_next_atomic(boundary)
        with pytest.raises(ConversationStateUnavailable):
            store.ensure_consumer(broker, command, store.clock.now(), lease)
        assert len(broker.calls) == (0 if boundary == "reserve_enqueue" else 1)
        if boundary == "reserve_enqueue":
            assert store.snapshot() == before
        else:
            dispatch = store.dispatch(command, lease)
            assert dispatch.enqueue_attempt_id is not None
            assert dispatch.phase is domain.DispatchPhase.PENDING
            assert store.ensure_consumer(broker, command, store.clock.now(), lease) is domain.EnsureConsumerResult.NOT_DUE
            assert len(broker.calls) == 1


def test_enqueue_old_owner_completion_cannot_overwrite_successor_reservation():
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    from tests.fakes import ScriptedBroker
    domain, store = batch_api(), make_store()
    broker, successor = ScriptedBroker(), ScriptedBroker()
    with store.contact_lease(PHONE) as old:
        store.initialize_contact(PHONE, old, db_state_present=False)
        command = batch_command(store, old, append_batch(store, old))
        def takeover(_command):
            store.clock.advance(timedelta(seconds=61))
            with store.contact_lease(PHONE) as new:
                store.ensure_consumer(successor, command, store.clock.now(), new)
        broker.on_enqueue = takeover
        with pytest.raises(ContactLeaseLost):
            store.ensure_consumer(broker, command, store.clock.now(), old)
    with store.contact_lease(PHONE) as lease:
        assert store.dispatch(command, lease).phase is domain.DispatchPhase.SCHEDULED
        assert store.dispatch(command, lease).scheduled_at == store.clock.now()


def test_staged_heartbeat_renews_only_owned_claim_and_keeps_original_processing_horizon():
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    batch_api()
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, store.clock.now(), lease)
        for _ in range(29):
            store.clock.advance(timedelta(seconds=20))
            store.renew_lease(lease)
        item = batch_details(store, lease, "processing")[0]
        assert item.body["processing_deadline"] == claim.attempt.processing_deadline.timestamp()
        assert item.body["claim_deadline"] == claim.attempt.processing_deadline.timestamp()
    with store.contact_lease(PHONE) as lease:
        before = batch_details(store, lease, "processing")[0]
        store.renew_lease(lease)
        assert batch_details(store, lease, "processing")[0] == before


def test_staged_other_batch_cannot_call_agent_until_current_batch_is_terminal():
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    domain, store = batch_api(), make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        first = batch_command(store, lease, append_batch(store, lease))
        store.claim_or_resume_batch(first, store.clock.now(), lease)
        second = batch_command(store, lease, append_batch(store, lease, message_id="second-id"))
        before = store.snapshot()
        with pytest.raises(domain.ConversationMutationPending):
            store.claim_or_resume_batch(second, store.clock.now(), lease)
        assert store.snapshot() == before


def test_batch_generation_change_discards_old_result_before_sql(transition_env):
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, clock.now(), lease)
        coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "intervening-pause")
        result = domain.AgentResult("obsolete response", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        with pytest.raises(domain.ConversationMutationPending):
            store.stage_agent_result(command, claim.attempt, result, clock.now(), lease)
        assert store.claim_or_resume_batch(command, clock.now(), lease).outcome is domain.ClaimOutcome.TERMINAL
        assert batch_details(store, lease, "dedupe")[0].body["disposition"] == "FAILED"
        assert "obsolete response" not in str(store.contact_snapshot(PHONE))


@pytest.mark.parametrize("fault", ["epoch_absent", "run_id_mismatch", "fingerprint"])
def test_batch_recovery_coordination_failure_preserves_index_without_broker_effect(fault):
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    from tests.fakes import ScriptedBroker
    domain, store, broker = batch_api(), make_store(), ScriptedBroker()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        if fault == "fingerprint":
            store.corrupt_contact(PHONE, fault)
        else:
            store.inject_fault(fault)
        index_before = {key: set(value) for key, value in store.client.sets.items()}
        with pytest.raises((ReadinessUnavailable, ConversationGenerationUnavailable)):
            store.ensure_consumer(broker, command, store.clock.now(), lease)
        for key, value in index_before.items():
            assert store.client.sets[key] == value
        assert broker.calls == []


def test_batch_result_identity_cannot_be_substituted_before_mutation(transition_env):
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, clock.now(), lease)
        result = domain.AgentResult("accepted", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(command, claim.attempt, result, clock.now(), lease)
        for processing_id, operation_id, response in [("other", claim.attempt.operation_id, result),
                (claim.attempt.processing_id, "other", result),
                (claim.attempt.processing_id, claim.attempt.operation_id, replace(result, text="substituted"))]:
            with pytest.raises(domain.ConversationMutationPending):
                coordinator.apply_agent_result(db, PHONE, response, processing_id, operation_id, clock.now(), lease)
        assert "commit_entered" not in db.events


def test_batch_processing_terminal_expiry_cannot_be_renewed_by_replay():
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    domain, store = batch_api(), make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        receipt = append_batch(store, lease)
        command = batch_command(store, lease, receipt)
        claim = store.claim_or_resume_batch(command, store.clock.now(), lease)
    store.clock.set(claim.attempt.processing_deadline)
    with store.contact_lease(PHONE) as lease:
        replay = store.finalize_ingress_once(PHONE, None, "synthetic-id", command.generation, lease)
        assert replay.disposition is domain.IngressDisposition.DUPLICATE
        assert store.dispatch(command, lease).phase is domain.DispatchPhase.EXHAUSTED
        assert batch_details(store, lease, "staging") == []


def test_staged_recovery_enqueues_abandoned_claim_without_creating_another_batch():
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    from tests.fakes import ScriptedBroker
    domain, store, broker = batch_api(), make_store(), ScriptedBroker()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, store.clock.now(), lease)
    store.clock.set(claim.attempt.claim_deadline)
    with store.contact_lease(PHONE) as lease:
        recovered = store.recoverable_batches().commands[0]
        assert store.ensure_consumer(broker, recovered, store.clock.now(), lease) is domain.EnsureConsumerResult.SCHEDULED
        assert store.dispatch(command, lease).phase is domain.DispatchPhase.STAGED
        assert batch_details(store, lease, "buffer") == []
        assert broker.calls[0].processing_id == claim.attempt.processing_id
        assert store.ensure_consumer(broker, recovered, store.clock.now(), lease) is domain.EnsureConsumerResult.NOT_DUE
        assert len(broker.calls) == 1


def test_claim_processing_deadline_equality_never_returns_staging_content():
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    domain, store = batch_api(), make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        attempt = store.claim_or_resume_batch(command, store.clock.now(), lease).attempt
    store.clock.set(attempt.processing_deadline)
    with store.contact_lease(PHONE) as lease:
        claimed = store.claim_or_resume_batch(command, store.clock.now(), lease)
        assert claimed.outcome is domain.ClaimOutcome.TERMINAL
        assert claimed.envelopes == ()


def test_batch_quarantine_preserves_prior_applied_command_dedupe(transition_env):
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        resolution = coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        receipt = store.finalize_ingress_once(PHONE, None, "command-receipt", resolution.generation, lease,
                                             disposition=domain.IngressDisposition.APPLIED)
        coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, receipt.operation_id)
        coordinator.unpause(db, PHONE, clock.now(), lease, "unpause-for-test")
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, clock.now(), lease)
        result = domain.AgentResult("result", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(command, claim.attempt, result, clock.now(), lease)
        store.fail_next_atomic("finalize_committed")
        with pytest.raises(domain.ConversationMutationAmbiguous):
            coordinator.apply_agent_result(db, PHONE, result, claim.attempt.processing_id, claim.attempt.operation_id, clock.now(), lease)
        store.quarantine_ambiguous_commit(PHONE, claim.attempt.operation_id, lease, clock.now())
        store.resolve_quarantined_mutation(PHONE, claim.attempt.operation_id, store.config.coordination_epoch,
                                          lease, clock.now(), quiescent=True, outcome=domain.MutationPhase.COMMITTED)
        replay = store.finalize_ingress_once(PHONE, None, "command-receipt", resolution.generation, lease,
                                            disposition=domain.IngressDisposition.APPLIED)
        assert replay.disposition is domain.IngressDisposition.DUPLICATE


def test_batch_expired_result_ready_never_accepts_duplicate_result_publish():
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    from app.conversation_redis import contact_keys
    domain, store = batch_api(), make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, store.clock.now(), lease)
        result = domain.AgentResult("winner", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(command, claim.attempt, result, store.clock.now(), lease)
        store.client.expiry[contact_keys(PHONE).lease] = (claim.attempt.processing_deadline + timedelta(seconds=60)).timestamp()
        store.clock.set(claim.attempt.processing_deadline)
        with pytest.raises(domain.ConversationMutationPending):
            store.stage_agent_result(command, claim.attempt, result, store.clock.now(), lease)
        assert store.dispatch(command, lease).phase is domain.DispatchPhase.EXHAUSTED


def test_batch_recovery_dependency_error_exposes_only_enumerated_reason():
    from tests.test_conversation_state import batch_api
    domain, store = batch_api(), make_store()
    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic-sensitive-redis-error")
    store.client.sscan = fail
    with pytest.raises(domain.ConversationStateUnavailable) as caught:
        store.recoverable_batches()
    assert "synthetic-sensitive" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_batch_claim_cannot_publish_mismatching_operation_or_epoch():
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    domain, store = batch_api(), make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, store.clock.now(), lease)
        result = domain.AgentResult("result", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        for field in ("operation_id", "coordination_epoch", "generation", "batch_id"):
            altered = replace(claim.attempt, **{field: "00000000-0000-4000-8000-999999999999"})
            with pytest.raises(domain.ConversationMutationPending):
                store.stage_agent_result(command, altered, result, store.clock.now(), lease)


def test_batch_ack_loss_after_append_staging_and_result_keeps_one_atomic_winner():
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    domain, store = batch_api(), make_store()
    def lost():
        raise RuntimeError("synthetic acknowledgment lost")
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        store.client.after_operation["finalize_ingress_once"] = lost
        with pytest.raises(ConversationStateUnavailable):
            append_batch(store, lease)
        receipt = append_batch(store, lease)
        assert receipt.disposition is domain.IngressDisposition.DUPLICATE
        assert len(batch_details(store, lease, "buffer")[0].body["envelopes"]) == 1
        command = batch_command(store, lease, receipt)
        store.client.after_operation["claim_or_resume_batch"] = lost
        with pytest.raises(ConversationStateUnavailable):
            store.claim_or_resume_batch(command, store.clock.now(), lease)
        assert store.claim_or_resume_batch(command, store.clock.now(), lease).outcome is domain.ClaimOutcome.DUPLICATE
        deadline = store._processing_load(batch_details(store, lease, "processing")[0]).claim_deadline
    store.clock.set(deadline)
    with store.contact_lease(PHONE) as lease:
        claim = store.claim_or_resume_batch(command, store.clock.now(), lease)
        store.client.after_operation["stage_agent_result"] = lost
        result = domain.AgentResult("result", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        with pytest.raises(ConversationStateUnavailable):
            store.stage_agent_result(command, claim.attempt, result, store.clock.now(), lease)
        assert store.claim_or_resume_batch(command, store.clock.now(), lease).result == result


@pytest.mark.parametrize("timeout", [False, True])
def test_batch_quarantine_resolution_preserves_current_batch_receipt(transition_env, timeout):
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, clock.now(), lease)
        result = domain.AgentResult("result", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(command, claim.attempt, result, clock.now(), lease)
        store.fail_next_atomic("finalize_committed")
        with pytest.raises(domain.ConversationMutationAmbiguous):
            coordinator.apply_agent_result(db, PHONE, result, claim.attempt.processing_id, claim.attempt.operation_id, clock.now(), lease)
        if not timeout:
            store.quarantine_ambiguous_commit(PHONE, claim.attempt.operation_id, lease, clock.now())
    if timeout:
        clock.set(claim.attempt.processing_deadline)
    with store.contact_lease(PHONE) as lease:
        if timeout:
            with pytest.raises(domain.ConversationMutationPending):
                store.read_anchor(lease)
        store.resolve_quarantined_mutation(PHONE, claim.attempt.operation_id, store.config.coordination_epoch,
                                          lease, clock.now(), quiescent=True, outcome=domain.MutationPhase.COMMITTED)
        assert store.finalize_ingress_once(PHONE, None, "synthetic-id", command.generation, lease).disposition is domain.IngressDisposition.DUPLICATE


@pytest.mark.parametrize("offset", [0, 1])
def test_batch_committed_without_enqueue_remains_recoverable_past_processing_deadline(transition_env, offset):
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        command = batch_command(store, lease, append_batch(store, lease))
        claim = store.claim_or_resume_batch(command, clock.now(), lease)
        result = domain.AgentResult("bounded result", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(command, claim.attempt, result, clock.now(), lease)
        coordinator.apply_agent_result(db, PHONE, result, claim.attempt.processing_id, claim.attempt.operation_id, clock.now(), lease)
    clock.set(claim.attempt.processing_deadline + timedelta(microseconds=offset))
    with store.contact_lease(PHONE) as lease:
        resumed = store.claim_or_resume_batch(command, clock.now(), lease)
        assert resumed.outcome is domain.ClaimOutcome.APPLYING
        assert resumed.result == result
        store.exhaust_batch(command, clock.now(), lease)
        assert store.dispatch(command, lease).phase is domain.DispatchPhase.STAGED
        assert len(batch_details(store, lease, "staging")) == 1
        assert len(store.recoverable_batches().commands) == 1
        assert db.events.count("commit_entered") == 1
        # Recovery only schedules the existing processing command, never outbound.
        from tests.fakes import ScriptedBroker
        broker = ScriptedBroker()
        assert store.ensure_consumer(broker, store.recoverable_batches().commands[0], clock.now(), lease) is domain.EnsureConsumerResult.SCHEDULED
        assert broker.calls[0].operation_id == claim.attempt.operation_id
        assert store.dispatch(command, lease).phase is domain.DispatchPhase.STAGED


def test_enqueue_untyped_confirmation_fails_closed_and_preserves_reservation():
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    from tests.fakes import ScriptedBroker
    domain, store, broker = batch_api(), make_store(), ScriptedBroker()
    broker.next_result = "CONFIRMED"
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        command = batch_command(store, lease, append_batch(store, lease))
        with pytest.raises(domain.BrokerUnavailable):
            store.ensure_consumer(broker, command, store.clock.now(), lease)
        assert store.dispatch(command, lease).phase is domain.DispatchPhase.PENDING
        assert store.dispatch(command, lease).enqueue_attempt_id is not None


@pytest.mark.parametrize("other_phase", ["RESULT_READY", "CLAIMED"])
def test_terminal_processing_batch_cannot_authorize_another_operation_or_result(transition_env, other_phase):
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        first_command = batch_command(store, lease, append_batch(store, lease))
        first = store.claim_or_resume_batch(first_command, clock.now(), lease).attempt
        first_result = domain.AgentResult("first result", [], None, {"version": 1}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(first_command, first, first_result, clock.now(), lease)
        coordinator.apply_agent_result(db, PHONE, first_result, first.processing_id, first.operation_id, clock.now(), lease)
        store.complete_batch(first_command, first, clock.now(), lease)
        second_command = batch_command(store, lease, append_batch(store, lease, message_id="second-id"))
        second = store.claim_or_resume_batch(second_command, clock.now(), lease).attempt
        second_result = replace(first_result, text="second result", flow_data={"version": 2})
        if other_phase == "RESULT_READY":
            store.stage_agent_result(second_command, second, second_result, clock.now(), lease)
        db.events.clear()
        with pytest.raises(domain.ConversationMutationPending):
            store.validate_agent_application(PHONE, first.processing_id, second.operation_id, second_result, lease)
        with pytest.raises(domain.ConversationMutationPending):
            coordinator.apply_agent_result(db, PHONE, second_result, first.processing_id, second.operation_id, clock.now(), lease)
        assert db.events == []
        # A retry of its own proven terminal operation still returns without SQL.
        coordinator.apply_agent_result(db, PHONE, first_result, first.processing_id, first.operation_id, clock.now(), lease)
        assert db.events == []


def test_terminal_processing_batch_validates_result_fingerprint_before_short_circuit(transition_env):
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        command = batch_command(store, lease, append_batch(store, lease))
        attempt = store.claim_or_resume_batch(command, clock.now(), lease).attempt
        result = domain.AgentResult("winner", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(command, attempt, result, clock.now(), lease)
        coordinator.apply_agent_result(db, PHONE, result, attempt.processing_id, attempt.operation_id, clock.now(), lease)
        store.complete_batch(command, attempt, clock.now(), lease)
        db.events.clear()
        with pytest.raises(domain.ConversationMutationPending):
            store.validate_agent_application(PHONE, attempt.processing_id, attempt.operation_id,
                                              replace(result, text="substitution"), lease)
        assert db.events == []


@pytest.mark.parametrize("timeout", [False, True])
def test_batch_quarantine_keeps_mutation_proof_and_failed_sibling_digests(transition_env, timeout):
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    from app.conversation_redis import contact_keys
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        command = batch_command(store, lease, append_batch(store, lease))
        attempt = store.claim_or_resume_batch(command, clock.now(), lease).attempt
        sibling = append_batch(store, lease, message_id="sibling-replay-id", content="sibling protected text")
        original_receipt = next(item for item in batch_details(store, lease, "dedupe") if item.body["batch_id"] == sibling.batch_id)
        result = domain.AgentResult("ambiguous protected result", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(command, attempt, result, clock.now(), lease)
        store.fail_next_atomic("finalize_committed")
        with pytest.raises(domain.ConversationMutationAmbiguous):
            coordinator.apply_agent_result(db, PHONE, result, attempt.processing_id, attempt.operation_id, clock.now(), lease)
        if not timeout:
            store.quarantine_ambiguous_commit(PHONE, attempt.operation_id, lease, clock.now())
    if timeout:
        clock.set(attempt.processing_deadline)
    with store.contact_lease(PHONE) as lease:
        if timeout:
            with pytest.raises(domain.ConversationMutationPending):
                store.read_anchor(lease)
        proof = store.inspect_mutation(PHONE, attempt.operation_id, lease, operational=True)
        assert proof.phase is domain.MutationPhase.QUARANTINED
        assert proof.operation_id == attempt.operation_id
        retained = store._snapshot(lease, operational=True)[1]
        failed = next(item for item in retained if item.entry.kind == "dedupe" and item.entry.id == original_receipt.entry.id)
        assert failed.body["disposition"] == "FAILED"
        assert failed.terminal is True
        assert failed.entry.expected_until >= original_receipt.entry.expected_until
        assert "sibling protected text" not in str(store.contact_snapshot(PHONE))
        assert "ambiguous protected result" not in str(store.contact_snapshot(PHONE))
        store.resolve_quarantined_mutation(PHONE, attempt.operation_id, store.config.coordination_epoch,
                                          lease, clock.now(), quiescent=True, outcome=domain.MutationPhase.COMMITTED)
        replay = store.finalize_ingress_once(PHONE, None, "sibling-replay-id", command.generation, lease)
        assert replay.disposition is domain.IngressDisposition.DUPLICATE
        assert batch_details(store, lease, "batch") == []
        assert batch_details(store, lease, "buffer") == []


def test_batch_cleanup_retains_committed_proof_until_its_staged_batch_completes(transition_env):
    from tests.test_conversation_state import batch_api, append_batch, batch_command, batch_details
    coordinator, db, store, clock = transition_env
    domain = batch_api()
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        command = batch_command(store, lease, append_batch(store, lease))
        attempt = store.claim_or_resume_batch(command, clock.now(), lease).attempt
        result = domain.AgentResult("confirmed pending enqueue", [], None, {}, domain.AgentIntent.SAVE_CONTEXT)
        store.stage_agent_result(command, attempt, result, clock.now(), lease)
        coordinator.apply_agent_result(db, PHONE, result, attempt.processing_id, attempt.operation_id, clock.now(), lease)
    clock.advance(timedelta(days=8))
    with store.contact_lease(PHONE) as lease:
        # An unrelated ingress invokes cleanup after the ordinary receipt window.
        append_batch(store, lease, message_id="day-eight-id", content="later message")
        committed = store.inspect_mutation(PHONE, attempt.operation_id, lease)
        assert committed is not None
        assert committed.phase is domain.MutationPhase.COMMITTED
        resumed = store.claim_or_resume_batch(command, clock.now(), lease)
        assert resumed.result == result
        assert resumed.attempt.operation_id == attempt.operation_id
        db.events.clear()
        coordinator.apply_agent_result(db, PHONE, result, attempt.processing_id, attempt.operation_id, clock.now(), lease)
        assert db.events == []
        assert store.dispatch(command, lease).phase is domain.DispatchPhase.STAGED
        assert any(item.entry.id == command.batch_id for item in batch_details(store, lease, "staging"))
        # Task 7 owns its actual local enqueue attempt before calling completion.
        store.complete_batch(command, resumed.attempt, clock.now(), lease)
        store.cleanup(lease, store.read_anchor(lease))
        assert store.inspect_mutation(PHONE, attempt.operation_id, lease) is None


@pytest.mark.parametrize("page_size", [1, 2])
@pytest.mark.parametrize("scan_chunk", [1, 100])
def test_batch_recovery_cursor_visits_both_indexes_past_not_due_leading_items(page_size, scan_chunk):
    from tests.test_conversation_state import batch_api, append_batch, batch_command
    from tests.fakes import ScriptedBroker
    from app.conversation_redis import RedisConversationStore
    import base64
    import hashlib
    domain, store, broker = batch_api(), make_store(), ScriptedBroker()
    store.client.sscan_chunk_limit = scan_chunk
    phones = [PHONE, OTHER, "5551777770000", "5551666660000"]
    expected = []
    for position, phone in enumerate(phones):
        with store.contact_lease(phone) as lease:
            store.initialize_contact(phone, lease, db_state_present=False)
            command = batch_command(store, lease, append_batch(store, lease, message_id="cursor-message-id"))
            expected.append(command.batch_id)
            if position < 3:
                store.ensure_consumer(broker, command, store.clock.now(), lease)
            else:
                attempt = store.claim_or_resume_batch(command, store.clock.now(), lease).attempt
    store.clock.set(attempt.claim_deadline)
    before_calls = len(broker.calls)
    cursor, visited, outcomes = None, [], []
    forbidden = [*phones, *(hashlib.sha256(phone.encode()).hexdigest() for phone in phones),
                 str(store.config.coordination_epoch), "cursor-message-id", "synthetic text"]
    for _ in range(10):
        # A new reader on every page proves continuation is explicit, not local state.
        reader = RedisConversationStore(store.client, store.config, store.clock)
        scan_calls = len(store.client.sscan_calls)
        page = reader.recoverable_batches(limit=page_size, cursor=cursor)
        assert len(store.client.sscan_calls) - scan_calls <= 2
        assert len(page.commands) <= page_size
        for command in page.commands:
            visited.append(command.batch_id)
            with store.contact_lease(command.phone) as lease:
                outcomes.append(store.ensure_consumer(broker, command, store.clock.now(), lease))
        cursor = page.next_cursor
        if cursor is None:
            break
        assert isinstance(cursor, str)
        encoded = cursor.rsplit(".", 1)[-1]
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        assert all(value not in cursor and value not in decoded for value in forbidden)
    else:
        pytest.fail("recovery cursor did not finish both indexes")
    assert set(visited) == set(expected)
    assert len(visited) == 4
    if page_size == 1:
        assert visited.index(expected[-1]) <= 1  # Each index gets a turn before a long first scan finishes.
    assert outcomes.count(domain.EnsureConsumerResult.NOT_DUE) == 3
    assert outcomes.count(domain.EnsureConsumerResult.SCHEDULED) == 1
    assert len(broker.calls) == before_calls + 1


@pytest.mark.parametrize("cursor", ["synthetic-sensitive-cursor", "r1.e30", "r1.bnVsbA", "r1.W10"])
def test_batch_recovery_cursor_rejects_malformed_input_with_sanitized_error(cursor):
    from tests.test_conversation_state import batch_api
    domain, store = batch_api(), make_store()
    with pytest.raises(domain.ConversationStateUnavailable) as caught:
        store.recoverable_batches(limit=1, cursor=cursor)
    assert caught.value.reason_code is domain.FailureReason.INVALID_VALUE
    assert cursor not in str(caught.value)


def mutation_for(store, lease, operation_id="pause-1", operational=False):
    return store.inspect_mutation(lease.phone, operation_id, lease, operational=operational)


def test_mutation_flush_lease_cas_commit_and_finalize_order(transition_env):
    from app.conversation_state import MutationPhase, ConversationCycle
    from app.models import ConversationContext, PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        def at_flush():
            assert mutation_for(store, lease).phase is MutationPhase.PREPARED
            assert db.get(ConversationContext, PHONE) is None
            assert db.get(PausedContact, PHONE) is not None
        def proof():
            lease.assert_owned()
            if "flush" in db.events:
                db.events.append("lease_proof")
        wrapped = replace(lease, ownership_guard=proof)
        def at_commit():
            assert mutation_for(store, lease).phase is MutationPhase.COMMITTING
            assert "lease_proof" in db.events
            assert store.read_anchor(lease).cycle is ConversationCycle.MUTATING
        db.hooks["flush"] = at_flush
        db.hooks["commit_entered"] = at_commit
        db.hooks["commit_returned"] = lambda: at_commit()
        coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), wrapped, "pause-1")
        assert mutation_for(store, lease).phase is MutationPhase.COMMITTED
        assert store.read_anchor(lease).cycle is ConversationCycle.PAUSED
        assert db.events.index("flush") < db.events.index("lease_proof") < db.events.index("commit_entered") < db.events.index("commit_returned")


def test_mutation_lease_loss_after_flush_rolls_back_without_commit(transition_env):
    from app.conversation_redis import contact_keys
    from app.conversation_state import MutationPhase
    from app.models import ConversationContext, PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        db.hooks["flush"] = lambda: store.client.values.pop(contact_keys(PHONE).lease)
        with pytest.raises(ContactLeaseLost):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
    assert "commit_entered" not in db.events
    assert "rollback" in db.events
    assert db.get(ConversationContext, PHONE) is not None
    assert db.get(PausedContact, PHONE) is None
    with store.contact_lease(PHONE) as lease:
        assert mutation_for(store, lease).phase is MutationPhase.PREPARED


def test_mutation_precommit_failure_aborts_and_allows_new_operation(transition_env):
    from app.conversation_state import MutationPhase, FailureReason
    from app.models import ConversationContext, PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        def fail():
            raise ConversationStateUnavailable(FailureReason.CONDITION_CHANGED)
        db.hooks["flush"] = fail
        with pytest.raises(ConversationStateUnavailable):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        assert mutation_for(store, lease).phase is MutationPhase.ABORTED
        assert db.get(ConversationContext, PHONE) is not None
        assert db.get(PausedContact, PHONE) is None
        assert "commit_entered" not in db.events
        coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-2")
        assert db.get(ConversationContext, PHONE) is None
        assert db.get(PausedContact, PHONE) is not None


@pytest.mark.parametrize("boundary", ["flush", "commit_entered", "commit_returned"])
def test_mutation_pending_blocks_competing_operation_at_each_sql_barrier(transition_env, boundary):
    from app.conversation_state import ConversationMutationPending, MutationPhase
    from app.models import ConversationContext
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        def competitor():
            phase = mutation_for(store, lease).phase
            assert phase is (MutationPhase.PREPARED if boundary == "flush" else MutationPhase.COMMITTING)
            before = store.contact_snapshot(PHONE)
            with pytest.raises(ConversationMutationPending):
                coordinator.close_context(db, PHONE, clock.now(), lease, "competing")
            assert store.contact_snapshot(PHONE) == before
        db.hooks[boundary] = competitor
        coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        assert db.get(ConversationContext, PHONE) is None
        assert db.events.count("commit_entered") == 1


def test_committing_finalize_failure_is_ambiguous_and_never_repeats_dml(transition_env):
    from app.conversation_state import ConversationMutationAmbiguous, ConversationMutationPending, MutationPhase
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        db.hooks["commit_returned"] = lambda: store.fail_next_atomic("finalize_committed")
        with pytest.raises(ConversationMutationAmbiguous) as caught:
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        assert caught.value.reason_code.value == "redis_finalize_after_commit_failed"
        assert mutation_for(store, lease).phase is MutationPhase.COMMITTING
        assert db.get(PausedContact, PHONE) is not None
        for operation in ("pause-1", "pause-2"):
            with pytest.raises(ConversationMutationPending):
                coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, operation)
        assert db.events.count("commit_entered") == 1


@pytest.mark.parametrize("boundary", ["commit_entered", "commit_returned"])
def test_committing_exception_quarantines_even_when_sql_result_is_unknown(transition_env, boundary):
    from app.conversation_state import ConversationMutationAmbiguous, ConversationMutationPending, MutationPhase
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        def fail():
            raise RuntimeError("synthetic-private-exception")
        db.hooks[boundary] = fail
        with pytest.raises(ConversationMutationAmbiguous) as caught:
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        assert caught.value.reason_code.value == "commit_result_unknown"
        assert "synthetic-private-exception" not in str(caught.value)
        assert (db.get(PausedContact, PHONE) is not None) is (boundary == "commit_returned")
        attempt = mutation_for(store, lease, operational=True)
        assert attempt.phase is MutationPhase.QUARANTINED
        with pytest.raises(ConversationMutationPending):
            coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        with pytest.raises(ConversationMutationPending):
            coordinator.close_context(db, PHONE, clock.now(), lease, "close-2")


def test_quarantine_timeout_is_compact_durable_and_requires_exact_quiescent_resolution(transition_env):
    from app.conversation_state import ConversationMutationAmbiguous, ConversationMutationPending, MutationPhase, ConversationCycle
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        db.hooks["commit_returned"] = lambda: store.fail_next_atomic("finalize_committed")
        with pytest.raises(ConversationMutationAmbiguous):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
    clock.advance(timedelta(seconds=601))
    with store.contact_lease(PHONE) as lease:
        with pytest.raises(ConversationMutationPending):
            coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        attempt = mutation_for(store, lease, operational=True)
        assert attempt.phase is MutationPhase.QUARANTINED
        before = store.contact_snapshot(PHONE)
        for epoch, operation, quiescent in ((UUID(int=99), "pause-1", True), (store.config.coordination_epoch, "wrong", True), (store.config.coordination_epoch, "pause-1", False)):
            with pytest.raises(ConversationStateUnavailable):
                store.resolve_quarantined_mutation(PHONE, operation, epoch, lease, clock.now(), quiescent=quiescent, outcome=MutationPhase.COMMITTED)
        assert store.contact_snapshot(PHONE) == before
    clock.advance(timedelta(days=366))
    with store.contact_lease(PHONE) as lease:
        attempt = mutation_for(store, lease, operational=True)
        assert attempt.phase is MutationPhase.QUARANTINED
        assert attempt.paused_until is not None
        assert attempt.reason == "secretary_manual_pause"
        store.resolve_quarantined_mutation(PHONE, "pause-1", store.config.coordination_epoch, lease, clock.now(), quiescent=True, outcome=MutationPhase.COMMITTED)
        assert store.read_anchor(lease).cycle is ConversationCycle.PAUSED


def test_mutation_operation_reuse_with_different_request_fails_without_sql(transition_env):
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        before = store.contact_snapshot(PHONE)
        with pytest.raises(ConversationStateUnavailable):
            coordinator.pause_for_secretary(db, PHONE, "user_requested_human_assistance", clock.now(), lease, "pause-1")
        assert store.contact_snapshot(PHONE) == before
        assert db.events.count("commit_entered") == 1


def test_mutation_cross_contact_lease_is_rejected_before_sql(transition_env):
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(OTHER) as lease:
        with pytest.raises(ContactLeaseLost):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
    assert db.get(PausedContact, PHONE) is None
    assert "commit_entered" not in db.events


def test_committing_atomic_deadline_cannot_be_crossed_after_python_check(transition_env):
    from app.conversation_state import ConversationMutationPending, MutationPhase
    from app.conversation_redis import contact_keys
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        def expire_processing_only():
            clock.advance(timedelta(seconds=600))
            # A heartbeat kept the contact lease alive while the SQL work stalled.
            store.client.expiry[contact_keys(PHONE).lease] = clock.now().timestamp() + 60
        store.client.before_operation["enter_committing"] = expire_processing_only
        with pytest.raises(ConversationMutationPending):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        assert "commit_entered" not in db.events
        assert db.get(PausedContact, PHONE) is None
        assert mutation_for(store, lease).phase is MutationPhase.ABORTED


def test_committing_new_lease_cannot_claim_previous_commit_success(transition_env):
    from app.conversation_state import ConversationMutationPending, ConversationMutationAmbiguous, MutationPhase
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        db.hooks["commit_returned"] = lambda: store.fail_next_atomic("finalize_committed")
        with pytest.raises(ConversationMutationAmbiguous):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
    with store.contact_lease(PHONE) as lease:
        with pytest.raises(ConversationMutationPending):
            store.finalize_committed(PHONE, "pause-1", lease, clock.now())
        store.preserve_or_abort_prepared(PHONE, "pause-1", lease, clock.now())
        assert mutation_for(store, lease).phase is MutationPhase.COMMITTING


def test_quarantine_late_read_after_bounded_detail_expiry_retains_operational_identity(transition_env):
    from app.conversation_state import ConversationMutationPending, ConversationMutationAmbiguous, MutationPhase
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        db.hooks["commit_returned"] = lambda: store.fail_next_atomic("finalize_committed")
        with pytest.raises(ConversationMutationAmbiguous):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
    clock.advance(timedelta(days=366))
    with store.contact_lease(PHONE) as lease:
        with pytest.raises(ConversationMutationPending):
            coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        assert mutation_for(store, lease, operational=True).phase is MutationPhase.QUARANTINED
        store.resolve_quarantined_mutation(PHONE, "pause-1", store.config.coordination_epoch, lease,
                                           clock.now(), quiescent=True, outcome=MutationPhase.COMMITTED)


@pytest.mark.parametrize("intent", ["PAUSE_FOR_SECRETARY", "CLOSE_CONTEXT"])
def test_mutation_agent_terminal_retry_rejects_different_output(transition_env, intent):
    from app.conversation_state import AgentIntent, AgentResult
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        result = AgentResult("synthetic", [], None, {}, AgentIntent(intent))
        coordinator.apply_agent_result(db, PHONE, result, "p-1", "terminal-1", clock.now(), lease)
        with pytest.raises(ConversationStateUnavailable):
            coordinator.apply_agent_result(db, PHONE, replace(result, text="changed"), "p-1", "terminal-1", clock.now(), lease)
        assert db.events.count("commit_entered") == 1


def test_mutation_rollback_failure_preserves_prepared_fence(transition_env):
    from app.conversation_state import MutationPhase
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        def fail():
            raise RuntimeError("synthetic")
        db.hooks.update(flush=fail, rollback=fail)
        with pytest.raises(ConversationStateUnavailable):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        assert mutation_for(store, lease).phase is MutationPhase.PREPARED
        assert "commit_entered" not in db.events


def test_quarantine_contact_a_does_not_block_contact_b_sql_or_generation(transition_env):
    from app.conversation_state import ConversationMutationAmbiguous
    from app.models import ConversationContext, PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(OTHER) as lease:
        coordinator.resolve_ingress(db, OTHER, clock.now(), lease)
        seed_context(db, OTHER, clock.now())
        before = store.contact_snapshot(OTHER)
    with store.contact_lease(PHONE) as lease:
        def fail():
            raise RuntimeError("synthetic")
        db.hooks["commit_entered"] = fail
        with pytest.raises(ConversationMutationAmbiguous):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
    with store.contact_lease(OTHER) as lease:
        assert store.contact_snapshot(OTHER) == before
        coordinator.pause_for_secretary(db, OTHER, "secretary_manual_pause", clock.now(), lease, "pause-2")
        assert db.get(PausedContact, OTHER) is not None
        assert db.get(ConversationContext, OTHER) is None


def test_committing_cas_ack_loss_before_commit_uses_definitive_local_rollback(transition_env):
    from app.conversation_state import MutationPhase
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        seen = []
        def fail_after_cas():
            assert mutation_for(store, lease).phase is MutationPhase.COMMITTING
            raise RuntimeError("synthetic")
        store.client.after_operation["enter_committing"] = fail_after_cas
        store.client.after_operation["restore_prepared"] = lambda: seen.append(mutation_for(store, lease).phase)
        with pytest.raises(ConversationStateUnavailable):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        assert seen == [MutationPhase.PREPARED]
        assert mutation_for(store, lease).phase is MutationPhase.PREPARED
        assert db.get(PausedContact, PHONE) is None
        assert "commit_entered" not in db.events
        coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        assert db.get(PausedContact, PHONE) is not None
        assert db.events.count("commit_entered") == 1


def test_committing_rollback_without_local_receipt_cannot_release_fence(transition_env):
    from app.conversation_state import MutationPhase, ConversationMutationAmbiguous
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        db.hooks["commit_returned"] = lambda: store.fail_next_atomic("finalize_committed")
        with pytest.raises(ConversationMutationAmbiguous):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        method = getattr(store, "restore_prepared_after_rollback", None)
        assert method is not None, "definitive rollback receipt API is absent"
        with pytest.raises(ConversationStateUnavailable):
            method(PHONE, "pause-1", lease, clock.now(), proof=None)
        assert mutation_for(store, lease).phase is MutationPhase.COMMITTING


def test_mutation_inactivity_refresh_between_read_and_delete_preserves_new_context(transition_env):
    from sqlalchemy import update
    from app.models import ConversationContext
    from app.conversation_state import ConversationCycle
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now() - timedelta(hours=2))
        def concurrent_refresh():
            db.session.execute(update(ConversationContext).where(ConversationContext.phone == PHONE).values(
                last_activity=clock.now().replace(tzinfo=None)))
            db.session.commit()
        db.hooks["execute"] = concurrent_refresh
        assert not coordinator.close_inactive_context(db, PHONE, clock.now() - timedelta(hours=1), clock.now(), lease, "clean-1")
        assert db.get(ConversationContext, PHONE).last_activity == clock.now().replace(tzinfo=None)
        assert store.read_anchor(lease).cycle is ConversationCycle.OPEN
        assert "commit_entered" not in db.events


@pytest.mark.parametrize("timeout", [False, True])
def test_quarantine_preserves_prior_committed_replay_receipt(transition_env, timeout):
    from app.conversation_state import ConversationMutationAmbiguous, ConversationMutationPending, MutationPhase
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        original = coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-a")
        def ambiguous():
            if timeout:
                store.fail_next_atomic("quarantine_mutation")
            raise RuntimeError("synthetic")
        db.hooks["commit_entered"] = ambiguous
        with pytest.raises(ConversationMutationAmbiguous):
            coordinator.unpause(db, PHONE, clock.now(), lease, "unpause-b")
    clock.advance(timedelta(seconds=601 if timeout else 1))
    with store.contact_lease(PHONE) as lease:
        with pytest.raises(ConversationMutationPending):
            coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        previous = store.inspect_mutation(PHONE, "pause-a", lease, operational=True)
        assert previous is not None and previous.phase is MutationPhase.COMMITTED
        store.resolve_quarantined_mutation(PHONE, "unpause-b", store.config.coordination_epoch, lease,
                                           clock.now(), quiescent=True, outcome=MutationPhase.ABORTED)
        commits = db.events.count("commit_entered")
        replay = coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-a")
        assert replay == original
        assert db.get(PausedContact, PHONE).paused_until == original.paused_until.replace(tzinfo=None)
        assert db.events.count("commit_entered") == commits


def test_quarantine_can_resolve_after_preserved_terminal_receipt_horizon(transition_env):
    from app.conversation_state import ConversationMutationAmbiguous, MutationPhase
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-a")
        def fail():
            raise RuntimeError("synthetic")
        db.hooks["commit_entered"] = fail
        with pytest.raises(ConversationMutationAmbiguous):
            coordinator.unpause(db, PHONE, clock.now(), lease, "unpause-b")
    clock.advance(timedelta(days=8))
    with store.contact_lease(PHONE) as lease:
        store.resolve_quarantined_mutation(PHONE, "unpause-b", store.config.coordination_epoch, lease,
                                           clock.now(), quiescent=True, outcome=MutationPhase.ABORTED)
        assert store.inspect_mutation(PHONE, "pause-a", lease) is None


@pytest.mark.parametrize("fault", ["phase", "generation", "target_fingerprint", "request_fingerprint",
                                   "target_cycle", "paused_until", "reason", "terminal", "extra"])
def test_committing_divergent_detail_cannot_bypass_durable_receipt(transition_env, fault):
    from app.conversation_state import ConversationMutationAmbiguous, ConversationDomainError, MutationPhase
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        db.hooks["commit_returned"] = lambda: store.fail_next_atomic("finalize_committed")
        with pytest.raises(ConversationMutationAmbiguous):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        entry = next(item for item in store.read_anchor(lease).manifest if item.kind == "mutation")
        key = store._detail_key(PHONE, entry)
        value = json.loads(store.client.values[key])
        if fault == "phase":
            value["body"]["phase"], value["terminal"] = "COMMITTED", True
        elif fault == "terminal":
            value["terminal"] = True
        else:
            value["body"][fault] = {"generation": str(UUID(int=9)), "target_fingerprint": "changed",
                                     "request_fingerprint": "changed", "target_cycle": "OPEN",
                                     "paused_until": "2026-09-14T12:00:00+00:00",
                                     "reason": "user_requested_human_assistance", "extra": "synthetic-private-note"}[fault]
        store.client.values[key] = json.dumps(value)
        with pytest.raises(ConversationDomainError):
            coordinator.unpause(db, PHONE, clock.now(), lease, "unpause-2")
        assert db.events.count("commit_entered") == 1
        assert db.get(PausedContact, PHONE) is not None
        assert mutation_for(store, lease, operational=True).phase is MutationPhase.QUARANTINED


@pytest.mark.parametrize("intent", ["SAVE_CONTEXT", "PAUSE_FOR_SECRETARY", "CLOSE_CONTEXT"])
def test_quarantine_resolution_rotates_generation_and_rejects_old_outbound(transition_env, intent):
    from app.conversation_state import (AgentResult, AgentIntent, ConversationMutationAmbiguous,
        MutationPhase, OutboundEnvelope, OutboundKind, PauseTransitionRef, ClosureTransitionRef)
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        def fail():
            raise RuntimeError("synthetic")
        db.hooks["commit_returned"] = fail
        with pytest.raises(ConversationMutationAmbiguous):
            coordinator.apply_agent_result(db, PHONE, AgentResult("synthetic", [], None, {}, AgentIntent(intent)),
                                           "p-1", "op-1", clock.now(), lease)
        old = store.inspect_mutation(PHONE, "op-1", lease, operational=True)
        kind = {"SAVE_CONTEXT": OutboundKind.NORMAL, "PAUSE_FOR_SECRETARY": OutboundKind.TRANSFER_CONFIRMATION,
                "CLOSE_CONTEXT": OutboundKind.CLOSURE_CONFIRMATION}[intent]
        envelope = OutboundEnvelope(PHONE, "synthetic", kind, str(old.generation), "p-1", "op-1",
            pause_ref=PauseTransitionRef(str(old.generation), clock.now() + timedelta(hours=24), "user_requested_human_assistance")
                if intent == "PAUSE_FOR_SECRETARY" else None,
            closure_ref=ClosureTransitionRef(str(old.generation), "op-1") if intent == "CLOSE_CONTEXT" else None)
        resolved = store.resolve_quarantined_mutation(PHONE, "op-1", store.config.coordination_epoch, lease,
                                                     clock.now(), quiescent=True, outcome=MutationPhase.COMMITTED)
        assert resolved.generation != old.generation
        assert store.read_anchor(lease).last_generation == resolved.generation
        assert not coordinator.may_send(db, envelope, clock.now(), lease)


@pytest.mark.parametrize("kind", ["pause", "manual", "extend", "unpause", "close", "inactive", "save"])
def test_mutation_identical_prepared_retry_resumes_original_target(transition_env, kind):
    from app.conversation_state import AgentResult, AgentIntent, MutationPhase
    from app.models import PausedContact, ConversationContext
    coordinator, db, store, clock = transition_env
    initial = clock.now()
    cutoff = initial - timedelta(hours=1)
    def action(lease):
        if kind == "pause":
            return coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "resume-1")
        if kind == "manual":
            return coordinator.pause_manual(db, PHONE, 24, "secretary_dashboard_pause", clock.now(), lease, "resume-1")
        if kind == "extend":
            return coordinator.extend_pause(db, PHONE, 2, clock.now(), lease, "resume-1")
        if kind == "unpause":
            return coordinator.unpause(db, PHONE, clock.now(), lease, "resume-1")
        if kind == "close":
            return coordinator.close_context(db, PHONE, clock.now(), lease, "resume-1")
        if kind == "inactive":
            return coordinator.close_inactive_context(db, PHONE, cutoff, clock.now(), lease, "resume-1")
        return coordinator.apply_agent_result(db, PHONE, AgentResult("synthetic", [], None, {}, AgentIntent.SAVE_CONTEXT),
                                               "p-1", "resume-1", clock.now(), lease)
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, initial, lease)
        seed_context(db, PHONE, initial - timedelta(hours=2))
        if kind in ("extend", "unpause"):
            coordinator.pause_manual(db, PHONE, 4, "secretary_dashboard_pause", initial, lease, "initial-pause")
        baseline_commits = db.events.count("commit_entered")
        def interrupted_preparation():
            raise RuntimeError("synthetic")
        store.client.after_operation["prepare_mutation"] = interrupted_preparation
        with pytest.raises(ConversationStateUnavailable):
            action(lease)
        original = store.inspect_mutation(PHONE, "resume-1", lease)
        assert original.phase is MutationPhase.PREPARED
        lineage = store.read_anchor(lease).generation_history
    clock.advance(timedelta(seconds=3))
    with store.contact_lease(PHONE) as lease:
        action(lease)
        completed = store.inspect_mutation(PHONE, "resume-1", lease)
        assert completed.phase is MutationPhase.COMMITTED
        assert completed.generation == original.generation
        assert completed.target_fingerprint == original.target_fingerprint
        assert completed.processing_deadline == original.processing_deadline
        assert store.read_anchor(lease).generation_history == lineage
        assert db.events.count("commit_entered") == baseline_commits + 1
        if kind in ("pause", "manual", "extend"):
            expected = initial + timedelta(hours=6 if kind == "extend" else 24)
            assert db.get(PausedContact, PHONE).paused_until == expected.replace(tzinfo=None)
            assert db.get(PausedContact, PHONE).paused_at == initial.replace(tzinfo=None)
        elif kind in ("close", "inactive"):
            assert db.get(ConversationContext, PHONE) is None
        elif kind == "unpause":
            assert db.get(PausedContact, PHONE) is None
        else:
            assert db.get(ConversationContext, PHONE).last_activity == initial.replace(tzinfo=None)


def test_mutation_changed_prepared_retry_is_rejected_without_dml(transition_env):
    from app.conversation_state import FailureReason, MutationPhase
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        def fail():
            raise RuntimeError("synthetic")
        store.client.after_operation["prepare_mutation"] = fail
        with pytest.raises(ConversationStateUnavailable):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        before = store.contact_snapshot(PHONE)
        with pytest.raises(ConversationStateUnavailable) as caught:
            coordinator.pause_for_secretary(db, PHONE, "user_requested_human_assistance", clock.now(), lease, "pause-1")
        assert caught.value.reason_code is FailureReason.INVALID_VALUE
        assert store.contact_snapshot(PHONE) == before
        assert mutation_for(store, lease).phase is MutationPhase.PREPARED
        assert "commit_entered" not in db.events


def test_mutation_aborted_retry_is_terminal_and_does_not_block_later_operation(transition_env):
    from app.conversation_state import ConversationDomainError, FailureReason, MutationPhase
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        def fail():
            raise ConversationStateUnavailable(FailureReason.CONDITION_CHANGED)
        db.hooks["flush"] = fail
        with pytest.raises(ConversationStateUnavailable):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        assert mutation_for(store, lease).phase is MutationPhase.ABORTED
        coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-2")
        before = store.contact_snapshot(PHONE)
        with pytest.raises(ConversationDomainError) as caught:
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        assert caught.value.reason_code.value == "conversation_mutation_aborted"
        assert store.contact_snapshot(PHONE) == before
        assert db.events.count("commit_entered") == 1


def test_pause_expiry_prepared_retry_uses_original_internal_operation(transition_env):
    from app.conversation_state import ConversationState, MutationPhase
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        ref = coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
    clock.set(ref.paused_until)
    with store.contact_lease(PHONE) as lease:
        def fail():
            raise RuntimeError("synthetic")
        store.client.after_operation["prepare_mutation"] = fail
        with pytest.raises(ConversationStateUnavailable):
            coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        pending = store.read_anchor(lease).mutation_fence
        resumed = coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        assert resumed.state is ConversationState.BOT_ACTIVE
        completed = store.inspect_mutation(PHONE, pending["operation_id"], lease)
        assert completed.phase is MutationPhase.COMMITTED
        assert resumed.generation == pending["generation"]
        assert db.get(PausedContact, PHONE) is None
        assert db.events.count("commit_entered") == 2


@pytest.mark.parametrize("change", ["removed", "extended"])
@pytest.mark.parametrize("elapsed", [0, 601])
@pytest.mark.parametrize("phase", ["PREPARED", "COMMITTING", "QUARANTINED"])
def test_pause_expiry_ingress_reconciles_only_inapplicable_prepared(transition_env, change, elapsed, phase):
    """A changed SQL pause must not strand internal expiry or release uncertain commit."""
    from sqlalchemy import event
    from app.conversation_state import (ConversationMutationAborted, ConversationMutationPending,
                                        ConversationCycle, ConversationState, MutationPhase)
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        ref = coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
    clock.set(ref.paused_until)
    with store.contact_lease(PHONE) as lease:
        def interrupted():
            raise RuntimeError("synthetic")
        store.client.after_operation["prepare_mutation"] = interrupted
        with pytest.raises(ConversationStateUnavailable):
            coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        operation = store.read_anchor(lease).mutation_fence["operation_id"]
        pending = mutation_for(store, lease, operation)
        if phase != "PREPARED":
            store.enter_committing(PHONE, operation, lease, clock.now(), pending.processing_deadline)
        if phase == "QUARANTINED":
            store.quarantine_ambiguous_commit(PHONE, operation, lease, clock.now())
        pause = db.get(PausedContact, PHONE)
        if change == "removed":
            db.session.delete(pause)
        else:
            pause.paused_until = (clock.now() + timedelta(hours=1)).replace(tzinfo=None)
        db.session.commit()
    clock.advance(timedelta(seconds=elapsed))
    db.events.clear()
    statements = []
    def after_sql(_connection, _cursor, statement, _parameters, _context, _many):
        operation = statement.lstrip().split()[0].upper()
        if operation in ("INSERT", "UPDATE", "DELETE"):
            statements.append(operation)
    engine = db.get_bind()
    event.listen(engine, "after_cursor_execute", after_sql)
    try:
        with store.contact_lease(PHONE) as lease:
            error = ConversationMutationAborted if phase == "PREPARED" else ConversationMutationPending
            with pytest.raises(error):
                coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
            attempt = mutation_for(store, lease, operation, operational=phase != "PREPARED")
            if phase == "PREPARED":
                assert attempt.phase is MutationPhase.ABORTED
                anchor = store.read_anchor(lease)
                assert anchor.cycle is ConversationCycle.PAUSED
                assert anchor.mutation_fence is None
                if change == "extended":
                    result = coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
                    assert result.state is ConversationState.SECRETARY_ATTENDANCE
                    assert result.cycle is ConversationCycle.PAUSED
                else:
                    with pytest.raises(ConversationGenerationUnavailable):
                        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
            else:
                expected = MutationPhase.QUARANTINED if phase == "QUARANTINED" or elapsed else MutationPhase.COMMITTING
                assert attempt.phase is expected
            assert attempt.generation == pending.generation
            row = db.get(PausedContact, PHONE)
            assert (row is None) is (change == "removed")
            if row is not None:
                assert row.paused_until == (ref.paused_until + timedelta(hours=1)).replace(tzinfo=None)
            assert statements == []
            assert "flush_entered" not in db.events
            assert "commit_entered" not in db.events
    finally:
        event.remove(engine, "after_cursor_execute", after_sql)


@pytest.mark.parametrize("elapsed", [1, 601])
def test_mutation_changed_prepared_target_terminalizes_before_or_after_deadline(transition_env, elapsed):
    from app.conversation_state import ConversationDomainError, MutationPhase
    from app.models import ConversationContext, PausedContact
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now())
        def interrupted():
            raise RuntimeError("synthetic")
        store.client.after_operation["prepare_mutation"] = interrupted
        with pytest.raises(ConversationStateUnavailable):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        db.get(ConversationContext, PHONE).flow_data = {"changed": True}
        db.session.commit()
    clock.advance(timedelta(seconds=elapsed))
    with store.contact_lease(PHONE) as lease:
        with pytest.raises(ConversationDomainError) as caught:
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        assert caught.value.reason_code.value == "conversation_mutation_aborted"
        assert mutation_for(store, lease).phase is MutationPhase.ABORTED
        assert db.get(PausedContact, PHONE) is None
        assert db.get(ConversationContext, PHONE).flow_data == {"changed": True}
        assert "commit_entered" not in db.events
        coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-2")
        assert db.get(PausedContact, PHONE) is not None


def test_mutation_inapplicable_prepared_inactivity_is_aborted_before_returning_false(transition_env):
    from app.models import ConversationContext
    from app.conversation_state import MutationPhase
    coordinator, db, store, clock = transition_env
    cutoff = clock.now() - timedelta(hours=1)
    with store.contact_lease(PHONE) as lease:
        coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        seed_context(db, PHONE, clock.now() - timedelta(hours=2))
        def interrupted():
            raise RuntimeError("synthetic")
        store.client.after_operation["prepare_mutation"] = interrupted
        with pytest.raises(ConversationStateUnavailable):
            coordinator.close_inactive_context(db, PHONE, cutoff, clock.now(), lease, "clean-1")
        db.get(ConversationContext, PHONE).last_activity = clock.now().replace(tzinfo=None)
        db.session.commit()
        assert not coordinator.close_inactive_context(db, PHONE, cutoff, clock.now(), lease, "clean-1")
        assert store.inspect_mutation(PHONE, "clean-1", lease).phase is MutationPhase.ABORTED
        assert db.get(ConversationContext, PHONE) is not None
        assert "commit_entered" not in db.events
        coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-2")


def test_committing_cannot_be_aborted_by_prepared_reconciliation(transition_env):
    from app.conversation_state import ConversationMutationAmbiguous, ConversationMutationPending, MutationPhase
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        db.hooks["commit_returned"] = lambda: store.fail_next_atomic("finalize_committed")
        with pytest.raises(ConversationMutationAmbiguous):
            coordinator.pause_for_secretary(db, PHONE, "secretary_manual_pause", clock.now(), lease, "pause-1")
        current = mutation_for(store, lease)
        method = getattr(store, "abort_prepared", None)
        assert method is not None, "same-operation reconciliation API is absent"
        before = store.contact_snapshot(PHONE)
        with pytest.raises(ConversationMutationPending):
            method(PHONE, "pause-1", lease, clock.now(), request_fingerprint=current.request_fingerprint)
        assert mutation_for(store, lease).phase is MutationPhase.COMMITTING
        assert store.contact_snapshot(PHONE) == before
        assert db.events.count("commit_entered") == 1


@pytest.mark.parametrize("kind,completed_dml", [("pause", 0), ("save", 0), ("reset", 0),
                                               ("pause", 1), ("reset", 1), ("reset", 2), ("reset", 3)])
def test_mutation_lease_loss_blocks_each_subsequent_dml_and_flush(transition_env, kind, completed_dml):
    from sqlalchemy import event
    from app.models import ConversationContext, PausedContact, Appointment
    from app.conversation_state import AgentResult, AgentIntent
    from app.conversation_redis import contact_keys
    coordinator, db, store, clock = transition_env
    phone = coordinator.TEST_PHONE if kind == "reset" else PHONE
    with store.contact_lease(phone) as lease:
        coordinator.resolve_ingress(db, phone, clock.now(), lease)
        if kind != "save":
            seed_context(db, phone, clock.now())
        if kind == "reset":
            coordinator.pause_manual(db, phone, 24, "secretary_dashboard_pause", clock.now(), lease, "initial-pause")
            db.add(Appointment(patient_name="synthetic", patient_phone=phone, patient_birth_date="01/01/2000",
                               appointment_date="20260914", appointment_time="12:00",
                               created_at=clock.now().replace(tzinfo=None), updated_at=clock.now().replace(tzinfo=None)))
            db.session.commit()
        db.events.clear()
        statements = []
        def lose_lease():
            store.client.values.pop(contact_keys(phone).lease, None)
        def after_sql(_connection, _cursor, statement, _parameters, _context, _many):
            operation = statement.lstrip().split()[0].upper()
            if operation in ("INSERT", "UPDATE", "DELETE"):
                statements.append(operation)
                if len(statements) == completed_dml:
                    lose_lease()
        engine = db.get_bind()
        event.listen(engine, "after_cursor_execute", after_sql)
        if completed_dml == 0:
            store.client.after_operation["prepare_mutation"] = lose_lease
        try:
            with pytest.raises(ContactLeaseLost):
                if kind == "pause":
                    coordinator.pause_for_secretary(db, phone, "secretary_manual_pause", clock.now(), lease, "op-1")
                elif kind == "save":
                    coordinator.apply_agent_result(db, phone, AgentResult("synthetic", [], None, {}, AgentIntent.SAVE_CONTEXT),
                                                   "p-1", "op-1", clock.now(), lease)
                else:
                    coordinator.reset_test_state(db, phone, clock.now(), lease, "op-1")
        finally:
            event.remove(engine, "after_cursor_execute", after_sql)
        assert len(statements) == completed_dml
        assert "flush_entered" not in db.events
        assert "commit_entered" not in db.events
        assert (db.get(ConversationContext, phone) is not None) is (kind != "save")
        assert (db.get(PausedContact, phone) is not None) is (kind == "reset")
        if kind == "reset":
            assert db.query(Appointment).filter_by(patient_phone=phone).count() == 1


def test_mutation_lease_loss_after_orm_staging_blocks_flush(transition_env):
    from app.models import ConversationContext
    from app.conversation_state import AgentResult, AgentIntent
    from app.conversation_redis import contact_keys
    coordinator, db, store, clock = transition_env
    with store.contact_lease(PHONE) as lease:
        db.hooks["add_returned"] = lambda: store.client.values.pop(contact_keys(PHONE).lease, None)
        with pytest.raises(ContactLeaseLost):
            coordinator.apply_agent_result(db, PHONE, AgentResult("synthetic", [], None, {}, AgentIntent.SAVE_CONTEXT),
                                           "p-1", "op-1", clock.now(), lease)
        assert "flush_entered" not in db.events
        assert "commit_entered" not in db.events
        assert db.get(ConversationContext, PHONE) is None


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("reason", ["secretary_manual_pause", "user_requested_human_assistance"])
def test_quarantine_committed_pause_replay_returns_fresh_typed_reference_without_sql(transition_env, timeout, reason):
    from app.conversation_state import (ConversationMutationAmbiguous, ConversationMutationPending,
        MutationPhase, PauseTransitionRef, OutboundEnvelope, OutboundKind)
    from app.models import PausedContact
    coordinator, db, store, clock = transition_env
    original = []
    expected_deadline = clock.now() + timedelta(hours=24)
    with store.contact_lease(PHONE) as lease:
        def commit_returned():
            attempt = mutation_for(store, lease)
            original.append(PauseTransitionRef(str(attempt.generation), attempt.paused_until, attempt.reason))
            if timeout:
                store.fail_next_atomic("finalize_committed")
            else:
                raise RuntimeError("synthetic")
        db.hooks["commit_returned"] = commit_returned
        with pytest.raises(ConversationMutationAmbiguous):
            coordinator.pause_for_secretary(db, PHONE, reason, clock.now(), lease, "pause-1")
    clock.advance(timedelta(seconds=601 if timeout else 1))
    with store.contact_lease(PHONE) as lease:
        with pytest.raises(ConversationMutationPending):
            coordinator.resolve_ingress(db, PHONE, clock.now(), lease)
        resolved = store.resolve_quarantined_mutation(PHONE, "pause-1", store.config.coordination_epoch, lease,
                                                     clock.now(), quiescent=True, outcome=MutationPhase.COMMITTED)
        replay = coordinator.pause_for_secretary(db, PHONE, reason, clock.now(), lease, "pause-1")
        assert replay.paused_until == expected_deadline
        assert replay.reason == reason
        assert replay.generation == str(resolved.generation) != original[0].generation
        assert db.get(PausedContact, PHONE).paused_until == expected_deadline.replace(tzinfo=None)
        assert db.events.count("commit_entered") == 1
        old_outbound = OutboundEnvelope(PHONE, "synthetic", OutboundKind.TRANSFER_CONFIRMATION,
                                       original[0].generation, "p-1", "pause-1", pause_ref=original[0])
        assert not coordinator.may_send(db, old_outbound, clock.now(), lease)


def detail(store, kind="batch", flags=("dispatch",), terminal=False):
    from app.conversation_state import ContactDetail, ManifestEntry
    return ContactDetail(ManifestEntry(kind, "item-1", 1,
                         store.clock.now() + timedelta(seconds=120), flags),
                         {"phase": "DONE" if terminal else "PENDING"}, terminal)


@pytest.mark.parametrize("fault", ["epoch_absent", "epoch_mismatch", "run_id_mismatch", "noeviction_invalid", "persistence_invalid"])
def test_readiness_fails_closed_for_each_coordination_fault(fault):
    """Catches accepting an untrusted Redis incarnation or configuration."""
    store = make_store()
    assert store.readiness().ready
    store.inject_fault(fault)
    result = store.readiness()
    assert result.ready is False
    assert result.components["redis"] != "ready"


def test_store_never_initializes_epoch_implicitly():
    """Catches self-healing a missing global fence during initialization."""
    store = make_store()
    store.delete_global_epoch()
    with pytest.raises(ReadinessUnavailable):
        store.initialize_contact("5551999990000")
    assert store.global_epoch_writes == 0


def test_anchor_initialization_requires_explicit_empty_database_evidence():
    """Catches silently reconstructing lost Redis state over existing SQL state."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        for evidence in (None, True):
            with pytest.raises(ConversationGenerationUnavailable):
                store.initialize_contact(PHONE, lease, db_state_present=evidence)
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        assert anchor.contact_revision == 0
        assert isinstance(anchor.last_generation, UUID)
        assert store.read_anchor(lease) == anchor


@pytest.mark.parametrize("kind,flags", [("batch", ("dispatch",)), ("buffer", ()), ("staging", ("staging",)), ("processing", ()), ("mutation", ()), ("dedupe", ())])
def test_manifest_missing_detail_quarantines_only_affected_contact(kind, flags):
    """Catches missing referenced state being silently recreated or poisoning peers."""
    store = make_store()
    with store.contact_lease(PHONE) as lease, store.contact_lease(OTHER) as other:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        peer = store.initialize_contact(OTHER, other, db_state_present=False)
        item = detail(store, kind, flags)
        store.compare_and_set(lease, anchor, (item,))
        store.delete_detail(PHONE, item.entry)
        with pytest.raises(ConversationGenerationUnavailable):
            store.read_anchor(lease)
        assert store.is_quarantined(PHONE)
        assert store.read_anchor(other) == peer


@pytest.mark.parametrize("fault", ["dispatch", "staging", "fingerprint", "manifest", "generation"])
def test_manifest_divergence_quarantines_contact(fault):
    """Catches accepting missing indices, malformed anchors, or generation divergence."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        store.compare_and_set(lease, anchor, (detail(store, flags=("dispatch", "staging")),))
        store.corrupt_contact(PHONE, fault)
        with pytest.raises(ConversationGenerationUnavailable):
            store.read_anchor(lease)
        assert store.is_quarantined(PHONE)


def test_anchor_stale_revision_and_fingerprint_never_write():
    """Catches stale callers overwriting newer complete state."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        initial = store.initialize_contact(PHONE, lease, db_state_present=False)
        current = store.compare_and_set(lease, initial, (detail(store),))
        for stale in (initial, replace(current, manifest_fingerprint="bad")):
            before = store.snapshot()
            with pytest.raises(ConversationStateUnavailable):
                store.compare_and_set(lease, stale, ())
            assert store.snapshot() == before
        assert store.read_anchor(lease) == current


def test_anchor_generation_cannot_reuse_an_earlier_uuid():
    """Catches an old generation becoming valid again after a transition."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        old = store.initialize_contact(PHONE, lease, db_state_present=False)
        current = store.compare_and_set(lease, old, (), generation=UUID(int=2))
        with pytest.raises(ConversationGenerationUnavailable):
            store.compare_and_set(lease, current, (), generation=old.last_generation)
        assert store.is_quarantined(PHONE)


def test_manifest_atomic_failure_preserves_all_keys_indices_revision():
    """Catches partial detail/index/anchor writes on a failed CAS."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        before = store.snapshot()
        store.fail_next_atomic("cas")
        with pytest.raises(ConversationStateUnavailable):
            store.compare_and_set(lease, anchor, (detail(store),))
        unchanged = store.snapshot() == before
        assert unchanged


def test_manifest_cleanup_removes_terminal_details_only_at_horizon():
    """Catches early removal and expired terminal details being treated as loss."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        item = detail(store, terminal=True)
        anchor = store.compare_and_set(lease, anchor, (item,))
        assert store.cleanup(lease, anchor) == anchor
        for _ in range(6):
            store.clock.advance(timedelta(seconds=20))
            store.renew_lease(lease)
        cleaned = store.cleanup(lease, store.read_anchor(lease))
        assert cleaned.manifest == ()
        assert cleaned.contact_revision > anchor.contact_revision


def test_lease_expired_owner_cannot_release_or_mutate_successor():
    """Catches stale-owner release deleting a successor's lease or allowing writes."""
    store = make_store()
    with store.contact_lease(PHONE) as first:
        anchor = store.initialize_contact(PHONE, first, db_state_present=False)
        store.clock.advance(timedelta(seconds=61))
        with store.contact_lease(PHONE) as second:
            store.release_lease(first)
            store.assert_owned(second)
            with pytest.raises(ContactLeaseLost):
                store.compare_and_set(first, anchor, ())
            assert store.read_anchor(second) == anchor


def test_lease_two_owners_barrier_loser_changes_no_state():
    """Catches SET without NX allowing simultaneous owners to mutate a contact."""
    store = make_store()
    with store.contact_lease(PHONE) as initial_lease:
        store.initialize_contact(PHONE, initial_lease, db_state_present=False)
    start, held = Barrier(2), Barrier(2)
    outcomes = []
    def contender():
        start.wait(timeout=2)
        try:
            with store.contact_lease(PHONE) as lease:
                anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
                held.wait(timeout=2)
                outcomes.append(("won", anchor.contact_revision))
        except ContactLockUnavailable:
            before = store.contact_snapshot(PHONE)
            held.wait(timeout=2)
            outcomes.append(("lost", before == store.contact_snapshot(PHONE)))
    threads = [Thread(target=contender) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert sorted(outcomes) == [("lost", True), ("won", 0)]


def test_lease_heartbeat_renews_at_one_third_ttl_and_stops():
    """Catches delayed renewal, leaked heartbeat workers, and swallowed lease loss."""
    from tests.fakes import ControlledWait
    store = make_store()
    waiter = ControlledWait(store.clock)
    store.heartbeat_wait = waiter
    with store.contact_lease(PHONE) as lease:
        waiter.tick(19)
        assert store.lease_remaining(PHONE) == 41
        waiter.tick(1)
        store.assert_owned(lease)
        assert store.lease_remaining(PHONE) == 60
        store.fail_next_atomic("renew")
        waiter.tick(20)
        with pytest.raises(ContactLeaseLost):
            lease.assert_owned()
    assert waiter.stopped


def test_lease_claim_renewal_is_atomic_and_bounded_by_processing_deadline():
    """Catches renewal extending a claim past its processing horizon or stale manifest."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        item = detail(store, "processing", ())
        body = {"phase": "CLAIMED", "claim_deadline": store.clock.now().timestamp() + 10,
                "processing_deadline": store.clock.now().timestamp() + 30}
        anchor = store.compare_and_set(lease, anchor, (replace(item, body=body),))
        store.clock.advance(timedelta(seconds=20))
        store.renew_lease(lease)
        current = store.read_anchor(lease)
        renewed = store.read_details(lease)[0]
        assert renewed.body["claim_deadline"] == body["processing_deadline"]
        assert renewed.entry.version == 2
        assert current.contact_revision == anchor.contact_revision + 1
        assert current.manifest_fingerprint != anchor.manifest_fingerprint


def test_epoch_change_between_readiness_and_cas_blocks_mutation():
    """Catches relying on stale readiness after the global epoch changes."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        store.before_atomic = lambda: store.inject_fault("epoch_mismatch")
        with pytest.raises(ReadinessUnavailable):
            store.compare_and_set(lease, anchor, ())
        assert store.contact_snapshot(PHONE)["revision"] == anchor.contact_revision


def test_anchor_missing_with_surviving_details_quarantines_contact():
    """Catches an absent anchor being treated as a clean contact despite remnants."""
    from app.conversation_redis import contact_keys
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        store.compare_and_set(lease, anchor, (detail(store),))
        store.client.values.pop(contact_keys(PHONE).anchor)
        with pytest.raises(ConversationGenerationUnavailable):
            store.read_anchor(lease)
        assert store.is_quarantined(PHONE)


def test_anchor_initialization_rejects_orphan_global_index_membership():
    """Catches initialization after partial loss leaves only a global index remnant."""
    from app.conversation_redis import contact_keys
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        store.compare_and_set(lease, anchor, (detail(store),))
        keys = contact_keys(PHONE)
        for key in list(store.client.values):
            if key.startswith(keys.anchor.rsplit(":", 1)[0]) and key != keys.lease:
                store.client.values.pop(key)
        with pytest.raises(ConversationGenerationUnavailable):
            store.initialize_contact(PHONE, lease, db_state_present=False)
        assert store.is_quarantined(PHONE)


def test_manifest_update_rejects_regressive_or_unchanged_detail_version():
    """Catches changing a detail while reusing the old manifest version fence."""
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        item = detail(store)
        anchor = store.compare_and_set(lease, anchor, (item,))
        with pytest.raises(ConversationGenerationUnavailable):
            store.compare_and_set(lease, anchor, (replace(item, body={"phase": "STAGED"}),))
        assert store.is_quarantined(PHONE)


def test_lease_invalid_configuration_fails_with_domain_reason_before_acquire():
    """Catches invalid TTL arithmetic escaping instead of readiness failing closed."""
    store = make_store()
    store.config = replace(store.config, contact_lease_ttl_seconds=None,
                           issues=(ConfigurationIssue.INVALID_CONTACT_LEASE_TTL,))
    with pytest.raises(ReadinessUnavailable):
        with store.contact_lease(PHONE):
            pytest.fail("invalid configuration acquired a lease")


@pytest.mark.parametrize("quarantine", [False, True])
def test_manifest_acl_denial_of_later_write_keeps_entire_state_unchanged(quarantine):
    """Catches a denied later SADD leaving earlier SET/DEL writes committed."""
    from app.conversation_redis import DISPATCH_INDEX_KEY, QUARANTINE_INDEX_KEY
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        target = QUARANTINE_INDEX_KEY if quarantine else DISPATCH_INDEX_KEY
        store.client.denied_commands.add(("SADD", target))
        if quarantine:
            store.corrupt_contact(PHONE, "manifest")
        before = store.snapshot()
        with pytest.raises(ConversationStateUnavailable):
            if quarantine:
                store.read_anchor(lease)
            else:
                store.compare_and_set(lease, anchor, (detail(store),))
        unchanged = store.snapshot() == before
        assert unchanged


@pytest.mark.parametrize("target", ["anchor", "control"])
def test_anchor_regressive_revision_quarantines_only_that_contact(target):
    """Catches accepting a rolled-back anchor revision after a completed CAS."""
    from app.conversation_redis import RedisConversationStore, contact_keys
    store = make_store()
    with store.contact_lease(PHONE) as lease, store.contact_lease(OTHER) as peer_lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        peer = store.initialize_contact(OTHER, peer_lease, db_state_present=False)
        # Include an empty manifest: checking detail versions alone cannot fence it.
        updated = store.compare_and_set(lease, anchor, ())
        assert updated.contact_revision == 1
        key = contact_keys(PHONE).anchor if target == "anchor" else contact_keys(PHONE).generation
        raw = store.client.values[key]
        if not raw.startswith("{"):
            pytest.fail("generation control does not retain a durable revision fence")
        corrupt = json.loads(raw)
        corrupt["contact_revision"] = 0
        store.client.values[key] = json.dumps(corrupt)
        assert store.read_anchor(peer_lease) == peer
    # A fresh store/lease must detect the regression without process-local memory.
    restarted = RedisConversationStore(store.client, store.config, store.clock)
    with restarted.contact_lease(PHONE) as lease:
        with pytest.raises(ConversationGenerationUnavailable):
            restarted.read_anchor(lease)
        assert store.is_quarantined(PHONE)
    with store.contact_lease(OTHER) as peer_lease:
        assert store.read_anchor(peer_lease) == peer


@pytest.mark.parametrize("ttl", [-1, True, 2 ** 63])
def test_manifest_invalid_late_write_argument_preserves_all_state(ttl):
    """Catches invalid TTL in a later operation failing after a prior SET."""
    from app.conversation_redis import contact_keys
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        keys = contact_keys(PHONE)
        before = store.snapshot()
        with pytest.raises(ConversationStateUnavailable):
            store._atomic(PHONE, "cas", [store._lease_check(lease)], [
                {"op": "SET", "key": keys.buffer_prefix + "item-1", "value": "synthetic"},
                {"op": "PEXPIRE", "key": keys.lease, "ttl": ttl},
            ])
        unchanged = store.snapshot() == before
        assert unchanged


def test_readiness_missing_acl_preflight_capability_blocks_acquire_without_writes():
    """Catches mutating on Redis that cannot authorize the entire script write batch."""
    store = make_store()
    store.client.acl_check_available = False
    before = store.snapshot()
    with pytest.raises(ConversationStateUnavailable):
        with store.contact_lease(PHONE):
            pytest.fail("missing ACL preflight capability acquired a lease")
    unchanged = store.snapshot() == before
    assert unchanged


@pytest.mark.parametrize("remnant", ["generation", "detail", "index"])
def test_lease_renewal_after_partial_anchor_loss_quarantines_and_loses_ownership(remnant):
    """Catches missing-anchor renewal prolonging permission to mutate lost state."""
    from app.conversation_redis import contact_keys
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        anchor = store.initialize_contact(PHONE, lease, db_state_present=False)
        item = detail(store)
        store.compare_and_set(lease, anchor, (item,))
        keys = contact_keys(PHONE)
        for key in list(store.client.values):
            if key.startswith(keys.anchor.rsplit(":", 1)[0]) and key != keys.lease:
                if remnant == "generation" and key == keys.generation:
                    continue
                if remnant == "detail" and key == store._detail_key(PHONE, item.entry):
                    continue
                store.client.values.pop(key)
        if remnant != "index":
            store.client.sets.clear()
        with pytest.raises(ContactLeaseLost):
            store.renew_lease(lease)
        assert store.is_quarantined(PHONE)
        with pytest.raises(ContactLeaseLost):
            lease.assert_owned()


@pytest.mark.parametrize("field,value", [("last_generation", 3), ("last_generation", {}),
                                         ("generation_history", [3]),
                                         ("generation_history", {"invalid": 1})])
def test_anchor_corrupt_uuid_json_types_quarantine_with_domain_reason(field, value):
    """Catches UUID parser AttributeError escaping without quarantining corrupt state."""
    from app.conversation_redis import contact_keys
    from app.conversation_state import FailureReason
    store = make_store()
    with store.contact_lease(PHONE) as lease:
        store.initialize_contact(PHONE, lease, db_state_present=False)
        key = contact_keys(PHONE).anchor
        anchor = json.loads(store.client.values[key])
        anchor[field] = value
        store.client.values[key] = json.dumps(anchor)
        with pytest.raises(ConversationGenerationUnavailable) as error:
            store.read_anchor(lease)
        assert error.value.reason_code is FailureReason.GENERATION_UNAVAILABLE
        assert store.is_quarantined(PHONE)
