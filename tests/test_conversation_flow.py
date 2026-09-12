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

import anthropic
import pytest
from anthropic.types import TextBlock, ToolUseBlock
from sqlalchemy.orm import Session

from app import conversation_state as domain
from app import utils
from app.conversation_redis import RedisConversationStore
from tests.fakes import ForbiddenAgentEffects, ManualClock, ScriptedClaude
from tests.fakes import WebhookRequest, webhook_payload


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
def test_webhook_identity_normalizes_once_to_one_canonical_lease(main_module, ingress_runtime, monkeypatch, jid, fields, nested):
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


@pytest.mark.parametrize("media", [None, "audioMessage", "imageMessage", "videoMessage", "documentMessage", "stickerMessage"])
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
def test_webhook_patient_pause_alias_buffers_fixed_help_only_while_active(main_module, ingress_runtime, session_factory, alias):
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


@pytest.mark.parametrize("alias", ["/pausar", "/pause"])
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


def test_webhook_secretary_pause_retries_same_prepared_operation(main_module, ingress_runtime, monkeypatch, session_factory):
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


def test_webhook_ordinary_origin_from_me_is_ignored_with_terminal_receipt(main_module, ingress_runtime):
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


def test_webhook_pause_expiry_at_exact_deadline_opens_new_generation(main_module, ingress_runtime, session_factory):
    from app.models import PausedContact
    ref = ingress_runtime.pause()
    ingress_runtime.clock.set(ref.paused_until)
    response = webhook(main_module)
    assert response.status_code == 200
    envelope = ingress_runtime.envelopes()[0]
    assert envelope["generation"] != ref.generation
    with session_factory() as db:
        assert db.get(PausedContact, PHONE) is None


@pytest.mark.parametrize("failure", [domain.EnqueueResult.DEFINITIVE_FAILURE, domain.EnqueueResult.AMBIGUOUS])
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
def test_webhook_invalid_retained_receipt_fails_closed_before_sql_or_dispatch(
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
def test_webhook_snapshot_corruption_is_not_clean_absence_before_sql(
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


def test_webhook_virgin_clean_absence_keeps_sql_backed_initialization(main_module, ingress_runtime, session_factory):
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


def test_webhook_replay_after_dispatch_deadline_is_terminal_without_rebuffer(main_module, ingress_runtime):
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


def test_webhook_without_message_id_accepts_without_replay_guarantee(main_module, ingress_runtime):
    assert webhook(main_module, message_id=None).status_code == 200
    assert webhook(main_module, message_id=None).status_code == 200
    assert len(ingress_runtime.envelopes()) == 2


@pytest.mark.parametrize("operation", ["acquire", "initialize", "finalize_ingress_once"])
def test_webhook_coordination_failure_is_503_without_legacy_fallback(main_module, ingress_runtime, operation):
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


def test_webhook_staged_replay_uses_processing_deadline_not_old_dispatch_deadline(main_module, ingress_runtime):
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
                ingress_runtime.store.complete_batch(command, claim.attempt, ingress_runtime.clock.now(), lease)
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
