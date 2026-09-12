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
    (domain.AgentIntent.SAVE_CONTEXT, domain.OutboundKind.NORMAL),
    (domain.AgentIntent.PAUSE_FOR_SECRETARY, domain.OutboundKind.TRANSFER_CONFIRMATION),
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
        assert not any(item.body.get("disposition") == "PROCESSED" for item in rt.store.read_details(rt.active_lease))
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


def test_process_batch_retry_after_result_ready_reuses_result_and_explicit_ids(task_api, processing_runtime):
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


def test_process_batch_sql_commit_then_lost_finalization_never_repeats_dml(task_api, processing_runtime):
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
        with pytest.raises(domain.ConversationMutationPending):
            rt.store.complete_batch(caught.value.command, claim.attempt, rt.clock.now(), lease)


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


def test_task_wrapper_processing_retry_preserves_staging_ids_and_celery_retry(main_module, processing_runtime, task_api, monkeypatch, caplog):
    from celery.exceptions import Retry
    from tests.fakes import RetryTask
    rt, task = processing_runtime, RetryTask()
    monkeypatch.setattr(main_module.app.state, "conversation_runtime", rt, raising=False)
    command = rt.buffer()
    rt.store.fail_next_atomic("prepare_mutation")
    with caplog.at_level(logging.INFO):
        logging.getLogger("unrelated_control").info("visible_control")
        with pytest.raises(Retry):
            main_module.process_message_task(task, command.to_payload())
    assert len(task.calls) == 1
    retry = domain.ProcessingCommand.from_payload(task.calls[0]["args"][0])
    assert retry.processing_id and retry.operation_id and retry.staging_id == command.batch_id
    assert task.calls[0]["kwargs"] == {}
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


def test_process_batch_ack_loss_after_claim_returns_explicit_resume_command(task_api, processing_runtime):
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


def test_process_batch_agent_failure_can_retry_only_same_staging_after_claim_expiry(task_api, processing_runtime):
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
def test_process_batch_dependency_failure_before_agent_has_no_patient_response(task_api, processing_runtime, fault, monkeypatch):
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


def test_sender_runs_async_transport_inside_live_lease_and_fresh_session(task_api, processing_runtime):
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
                ingress_runtime.store.record_outbound_attempt(command, claim.attempt, ingress_runtime.clock.now(), lease)
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
