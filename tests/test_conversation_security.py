"""Authenticated ingress tests; only synthetic headers, identities and stores."""
import asyncio
import hashlib
import json
import logging

import pytest

from app.conversation_state import DependencyName
from tests.fakes import WebhookRequest, webhook_payload


@pytest.mark.parametrize("method,path", [("GET", "/test/chat"), ("POST", "/test/chat"), ("POST", "/test/reset")])
@pytest.mark.parametrize("auth", [None, ("synthetic-admin", "wrong-password")])
def test_test_chat_reset_auth_precedes_state_agent_and_body(admin_client, admin_runtime, method, path, auth):
    rt = admin_runtime
    before = rt.store.snapshot()
    response = admin_client.request(method, path, auth=auth, content="{malformed")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"
    assert rt.readiness_calls == rt.lease_calls == rt.session_calls == rt.legacy_sessions == 0
    assert rt.agent.calls == [] and rt.store.snapshot() == before


@pytest.mark.parametrize("route", ["create", "extend", "unpause", "test_chat", "reset"])
@pytest.mark.parametrize("dependency", list(DependencyName))
def test_dashboard_test_chat_reset_readiness_precedes_content_lease_sql(admin_runtime, main_module, route, dependency):
    from tests.test_conversation_flow import ADMIN_PHONE
    from fastapi import HTTPException
    rt = admin_runtime
    rt.dependencies[dependency] = False
    request = WebhookRequest(main_module.app, json_error=AssertionError("body read before readiness"))
    if route == "create":
        call = main_module.pause_contact(request, admin="synthetic")
    elif route == "extend":
        call = main_module.extend_pause(ADMIN_PHONE, request, admin="synthetic")
    elif route == "unpause":
        call = main_module.unpause_contact(ADMIN_PHONE, request=request, admin="synthetic")
    elif route == "test_chat":
        call = main_module.test_chat_send(request, admin="synthetic")
    else:
        call = main_module.test_chat_reset(request=request, admin="synthetic")
    try:
        response = asyncio.run(call)
        status = response.status_code
    except HTTPException as exc:
        status = exc.status_code
    assert status == 503
    assert request.json_calls == rt.lease_calls == rt.session_calls == rt.legacy_sessions == 0
    assert rt.agent.calls == rt.outbound_broker.calls == rt.transport.calls == []


@pytest.mark.parametrize("action", ["create", "extend", "unpause", "test_chat", "reset"])
@pytest.mark.parametrize("failure", ["lease", "database", "commit", "lease_loss"])
def test_dashboard_simulator_reset_failure_is_closed_and_sanitized(admin_client, admin_runtime, session_factory, caplog, action, failure):
    from tests.test_conversation_flow import ADMIN_PHONE, SIMULATOR_PHONE, _admin_request
    from app.models import ConversationContext, PausedContact, Appointment
    rt = admin_runtime
    phone = SIMULATOR_PHONE if action in ("test_chat", "reset") else ADMIN_PHONE
    rt.seed_contact(phone, paused_hours=None if action == "test_chat" else 2, appointment=True)
    sentinel = "PRIVATE_EXCEPTION message-text token=synthetic"
    def fail():
        raise RuntimeError(sentinel)
    if failure == "lease":
        rt.store.fail_next_atomic("acquire")
    elif failure == "database":
        rt.session_factory = fail
    elif failure == "commit":
        rt.persistent_session_hooks["commit_entered"] = fail
    else:
        from app.conversation_redis import contact_keys
        rt.persistent_session_hooks["flush"] = lambda: rt.store.client.values.pop(contact_keys(phone).lease, None)
    with caplog.at_level(logging.INFO):
        response = (admin_client.post("/test/chat", json={"message": "synthetic content"}) if action == "test_chat"
            else admin_client.post("/test/reset") if action == "reset" else _admin_request(admin_client, action))
        logging.getLogger("unaffected_control").info("unaffected_control")
    assert response.status_code == 503
    application_logs = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("app."))
    assert sentinel not in response.text + application_logs
    assert phone not in application_logs
    assert "synthetic content" not in application_logs
    assert "unaffected_control" in caplog.text
    assert rt.outbound_broker.calls == rt.transport.calls == []
    with session_factory() as db:
        assert db.get(ConversationContext, phone) is not None
        assert (db.get(PausedContact, phone) is not None) is (action != "test_chat")
        assert db.query(Appointment).filter_by(patient_phone=phone).count() == 1


@pytest.mark.parametrize("dependency", list(DependencyName))
def test_scheduler_readiness_closed_before_scan_or_lock(scheduler_module, admin_runtime, dependency):
    rt = admin_runtime
    rt.dependencies[dependency] = False
    asyncio.run(scheduler_module.check_inactive_contexts(rt))
    assert rt.session_calls == rt.lease_calls == 0


def test_scheduler_missing_runtime_is_closed(scheduler_module, admin_runtime):
    asyncio.run(scheduler_module.check_inactive_contexts())
    assert admin_runtime.session_calls == admin_runtime.lease_calls == 0


def test_test_chat_page_accepts_synthetic_auth_without_reading_state(admin_client, admin_runtime):
    response = admin_client.get("/test/chat")
    assert response.status_code == 200
    assert admin_runtime.lease_calls == admin_runtime.session_calls == 0
    assert admin_runtime.agent.calls == []


@pytest.mark.parametrize("path", ["/api/paused-contacts", "/api/paused-contacts/5551999990011/extend", "/test/chat"])
@pytest.mark.parametrize("body", ["{malformed", "[]", "null"])
def test_dashboard_test_chat_bad_json_is_400_before_lease(admin_client, admin_runtime, path, body):
    response = admin_client.request("PUT" if path.endswith("extend") else "POST", path,
        content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert admin_runtime.lease_calls == admin_runtime.session_calls == 0


@pytest.mark.parametrize("failure", ["missing", "readiness_exception"])
@pytest.mark.parametrize("method,path", [("POST", "/api/paused-contacts"),
    ("PUT", "/api/paused-contacts/5551999990011/extend"), ("DELETE", "/api/paused-contacts/5551999990011"),
    ("POST", "/test/chat"), ("POST", "/test/reset")])
def test_dashboard_simulator_reset_missing_or_failed_readiness_is_503(admin_client, main_module, admin_runtime, monkeypatch, failure, method, path):
    def fail():
        raise RuntimeError("synthetic readiness failure")
    if failure == "missing":
        monkeypatch.delattr(main_module.app.state, "conversation_runtime")
    else:
        monkeypatch.setattr(admin_runtime, "readiness_status", fail)
    response = admin_client.request(method, path, content="{malformed")
    assert response.status_code == 503
    assert admin_runtime.lease_calls == admin_runtime.session_calls == 0
    assert admin_runtime.agent.calls == []


@pytest.mark.parametrize("failure", ["database", "readiness", "lease_loss"])
def test_scheduler_failure_is_sanitized_and_preserves_context(scheduler_module, admin_runtime, session_factory, caplog, monkeypatch, failure,
                                                            scheduler_application_log_records):
    from tests.test_conversation_flow import ADMIN_PHONE
    from app.models import ConversationContext
    from app.conversation_redis import contact_keys
    rt = admin_runtime
    rt.seed_contact(ADMIN_PHONE, age_minutes=61)
    sentinel = "PRIVATE_EXCEPTION patient=synthetic token=synthetic"
    def fail():
        raise RuntimeError(sentinel)
    if failure == "database":
        monkeypatch.setattr(rt, "session_factory", fail)
    elif failure == "readiness":
        monkeypatch.setattr(rt, "readiness_status", fail)
    else:
        rt.persistent_session_hooks["flush"] = lambda: rt.store.client.values.pop(contact_keys(ADMIN_PHONE).lease, None)
    with caplog.at_level(logging.INFO):
        asyncio.run(scheduler_module.check_inactive_contexts(rt))
        logging.getLogger("unaffected_control").info("unaffected_control")
    application_records = scheduler_application_log_records()
    assert any(record.name == scheduler_module.logger.name for record in application_records), "scheduler logger is outside the observed privacy set"
    application_logs = "\n".join(record.getMessage() for record in application_records)
    assert sentinel not in application_logs and ADMIN_PHONE not in application_logs
    assert "unaffected_control" in caplog.text
    with session_factory() as db:
        assert db.get(ConversationContext, ADMIN_PHONE) is not None


def test_scheduler_privacy_capture_observes_injected_sentinel_and_excludes_library_logs(
        scheduler_module, scheduler_application_log_records, caplog, monkeypatch):
    sentinel = "PRIVATE_EXCEPTION patient=synthetic token=synthetic"
    original_warning = scheduler_module.logger.warning
    def leaking_warning(*args, **kwargs):
        original_warning(sentinel)
    monkeypatch.setattr(scheduler_module.logger, "warning", leaking_warning)
    with caplog.at_level(logging.INFO):
        asyncio.run(scheduler_module.check_inactive_contexts())
        logging.getLogger("app.synthetic_privacy_control").info("application_control")
        logging.getLogger("httpx").info("httpx_out_of_scope")
        logging.getLogger(scheduler_module.logger.name + ".unrelated").info("other_scheduler_out_of_scope")
        logging.getLogger("unaffected_control").info("unaffected_control")
    application_records = scheduler_application_log_records()
    assert any(record.name == scheduler_module.logger.name and record.getMessage() == sentinel
               for record in application_records)
    assert any(record.name == "app.synthetic_privacy_control" for record in application_records)
    assert all(record.name not in ("httpx", scheduler_module.logger.name + ".unrelated", "unaffected_control")
               for record in application_records)
    assert "httpx_out_of_scope" in caplog.text
    assert "other_scheduler_out_of_scope" in caplog.text
    assert "unaffected_control" in caplog.text


def call_webhook(main, payload=None, **kwargs):
    request = WebhookRequest(main.app, payload, **kwargs)
    return asyncio.run(main.whatsapp_webhook(request)), request


@pytest.mark.parametrize("signature", [None, "wrong", "", "synthetic-webhook-secret-extra", "assinatura-ç"])
def test_webhook_invalid_signature_never_reads_body_or_probes(main_module, ingress_runtime, signature):
    response, request = call_webhook(main_module, signature=signature, json_error=AssertionError("body read"))
    assert response.status_code == 401
    assert json.loads(response.body) == {"status": "unauthorized"}
    assert request.json_calls == ingress_runtime.readiness_calls == ingress_runtime.lease_calls == 0
    assert ingress_runtime.session_calls == 0


def test_webhook_missing_secret_precedes_signature_and_body(main_module, ingress_runtime, monkeypatch):
    monkeypatch.setattr(main_module.settings, "webhook_secret", None)
    response, request = call_webhook(main_module, signature="wrong", json_error=AssertionError("body read"))
    assert response.status_code == 503
    assert json.loads(response.body) == {"status": "temporarily_unavailable"}
    assert request.json_calls == ingress_runtime.readiness_calls == ingress_runtime.lease_calls == 0


@pytest.mark.parametrize("dependency", list(DependencyName))
def test_webhook_readiness_precedes_json_lease_and_sql(main_module, ingress_runtime, dependency):
    ingress_runtime.dependencies[dependency] = False
    before = ingress_runtime.store.snapshot()
    response, request = call_webhook(main_module, json_error=AssertionError("body read"))
    assert response.status_code == 503
    assert request.json_calls == ingress_runtime.lease_calls == ingress_runtime.session_calls == 0
    assert ingress_runtime.readiness_calls == 1
    assert ingress_runtime.store.snapshot() == before


def test_webhook_unconfigured_runtime_is_closed_before_json(main_module):
    response, request = call_webhook(main_module, json_error=AssertionError("body read"))
    assert response.status_code == 503
    assert request.json_calls == 0


def test_webhook_signature_uses_constant_time_comparison(main_module, ingress_runtime, monkeypatch):
    original = main_module.secrets.compare_digest
    comparisons = []
    def compare(left, right):
        comparisons.append(True)
        return original(left, right)
    monkeypatch.setattr(main_module.secrets, "compare_digest", compare)
    response, request = call_webhook(main_module, {"event": "irrelevant"})
    assert response.status_code == 200
    assert request.json_calls == 1
    assert comparisons == [True]


@pytest.mark.parametrize("jid,fields", [
    ("1234567890123@g.us", {}), ("1234567890123@newsletter", {}),
    ("1234567890123@lid", {}),
    ("1234567890123@lid", {"senderPn": "1234567890123@g.us"}),
    ("1234567890123@unknown", {}),
])
def test_webhook_rejects_raw_group_newsletter_lid_before_normalization(main_module, ingress_runtime, monkeypatch, jid, fields):
    def forbidden(raw):
        pytest.fail("numeric normalization reached")
    monkeypatch.setattr(main_module, "normalize_phone", forbidden)
    before = ingress_runtime.store.snapshot()
    response, _ = call_webhook(main_module, webhook_payload(jid=jid, text="/pausar", from_me=True, key_fields=fields))
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ignored"}
    assert ingress_runtime.lease_calls == ingress_runtime.session_calls == 0
    assert ingress_runtime.store.snapshot() == before


@pytest.mark.parametrize("raw", ["", "123", "1" * 16, "0551999990000", None, 5551999990000,
    "prefix5551999990000", "5551999990000foo@s.whatsapp.net", "/5551999990000"])
def test_webhook_invalid_identity_has_no_lease_state_or_task(main_module, ingress_runtime, raw):
    response, _ = call_webhook(main_module, webhook_payload(jid=raw))
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ignored"}
    assert ingress_runtime.lease_calls == ingress_runtime.session_calls == 0
    assert ingress_runtime.processing_broker.calls == []


@pytest.mark.parametrize("payload", [None, [], {}, {"event": "other"}, {"event": "messages.upsert", "data": []},
    {"event": "messages.upsert", "data": {"messages": []}}])
def test_webhook_malformed_or_irrelevant_payload_is_ignored(main_module, ingress_runtime, payload):
    response, _ = call_webhook(main_module, payload)
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ignored"}
    assert ingress_runtime.lease_calls == 0


def test_webhook_unparseable_json_is_sanitized_bad_request(main_module, ingress_runtime):
    response, _ = call_webhook(main_module, json_error=ValueError("synthetic-private-parser-error"))
    assert response.status_code == 400
    assert json.loads(response.body) == {"status": "invalid_request"}
    assert ingress_runtime.lease_calls == 0


@pytest.mark.parametrize("from_me", ["true", "false", 1, {}, None])
def test_webhook_origin_requires_boolean_before_command_authority(main_module, ingress_runtime, from_me):
    response, _ = call_webhook(main_module, webhook_payload(text="/pause", from_me=from_me))
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ignored"}
    assert ingress_runtime.lease_calls == 0


def test_webhook_logs_omit_sentinels_and_keep_unrelated_control(main_module, ingress_runtime, caplog):
    caplog.set_level(logging.INFO)
    phone = "5551976543210"
    message_id = "synthetic-private-message-id"
    content = "synthetic-private-body"
    payload = webhook_payload(jid=phone + "@s.whatsapp.net", text=content, message_id=message_id)
    response, _ = call_webhook(main_module, payload)
    assert response.status_code == 200
    response, _ = call_webhook(main_module, payload)
    assert response.status_code == 200
    response, _ = call_webhook(main_module, payload, signature="synthetic-private-signature")
    assert response.status_code == 401
    ingress_runtime.store.fail_next_atomic("acquire")
    response, _ = call_webhook(main_module, payload)
    assert response.status_code == 503
    logging.getLogger("unrelated_control").info("visible-control")
    captured = " ".join(str(record.__dict__) for record in caplog.records)
    for sentinel in (phone, message_id, content, "synthetic-webhook-secret", "synthetic-private-signature",
                     hashlib.sha256(phone.encode()).hexdigest(), hashlib.sha256(message_id.encode()).hexdigest(),
                     str(ingress_runtime.store.config.coordination_epoch)):
        assert sentinel not in captured
    assert "visible-control" in captured
    assert any(record.name == "app.main" for record in caplog.records)


@pytest.mark.parametrize("cleaned", ["invalid", "123", [], {}, ""])
def test_webhook_lid_uses_usable_sender_pn_when_cleaned_is_invalid(main_module, ingress_runtime, cleaned):
    response, _ = call_webhook(main_module, webhook_payload(
        jid="123456789012345@lid", key_fields={"cleanedSenderPn": cleaned, "senderPn": "5551999990000@c.us"}))
    assert response.status_code == 200
    assert len(ingress_runtime.processing_broker.calls) == 1
    assert ingress_runtime.processing_broker.calls[0].phone == "5551999990000"


def test_webhook_http_app_client_has_no_lifespan_or_external_service(app_client, ingress_runtime):
    response = app_client.post("/webhook/whatsapp", json=webhook_payload(),
                               headers={"X-Webhook-Signature": "synthetic-webhook-secret"})
    assert response.status_code == 200
    assert response.json() == {"status": "buffered"}
    assert ingress_runtime.lease_calls == 1


@pytest.mark.parametrize("failure", ["readiness", "session"])
def test_webhook_dependency_exception_is_sanitized(main_module, ingress_runtime, monkeypatch, caplog, failure):
    caplog.set_level(logging.INFO)
    def fail():
        raise RuntimeError("synthetic-private-dependency-exception")
    monkeypatch.setattr(ingress_runtime, "readiness_status" if failure == "readiness" else "session_factory", fail)
    response, request = call_webhook(main_module, webhook_payload())
    assert response.status_code == 503
    assert json.loads(response.body) == {"status": "temporarily_unavailable"}
    assert request.json_calls == (0 if failure == "readiness" else 1)
    assert ingress_runtime.processing_broker.calls == []
    assert "synthetic-private-dependency-exception" not in str([record.__dict__ for record in caplog.records])


def test_webhook_paused_media_and_sql_failure_keep_private_values_out_of_logs(main_module, ingress_runtime, monkeypatch, caplog):
    from sqlalchemy.orm import Session
    caplog.set_level(logging.INFO)
    ingress_runtime.pause()
    response, _ = call_webhook(main_module, webhook_payload(media="imageMessage", message_id="private-media-id"))
    assert response.status_code == 200
    def fail_get(*args, **kwargs):
        raise RuntimeError("private-sql-error-with-contact-and-body")
    monkeypatch.setattr(Session, "get", fail_get)
    response, _ = call_webhook(main_module, webhook_payload(text="private-body", message_id="private-sql-id"))
    assert response.status_code == 503
    captured = str([record.__dict__ for record in caplog.records])
    for sentinel in ("5551999990000", "synthetic-media-url", "private-media-id", "private-sql-id",
                     "private-body", "private-sql-error-with-contact-and-body"):
        assert sentinel not in captured


@pytest.mark.parametrize("initial_state", ["virgin", "closed", "pause_at_expiry"])
@pytest.mark.parametrize("from_me", [False, True])
@pytest.mark.parametrize("message", [
    None, [], {}, {"reactionMessage": {"text": "/pause"}},
    {"conversation": ""}, {"conversation": " \n "},
    {"extendedTextMessage": {"text": ""}}, {"audioMessage": None},
    {"imageMessage": []}, {"unsupportedMessage": {"text": "/pausar"}},
])
def test_webhook_unusable_message_is_ignored_before_any_lease_or_state(
        main_module, ingress_runtime, monkeypatch, session_factory, initial_state, from_me, message):
    from uuid import uuid4
    from sqlalchemy import event
    phone = "5551999990000"
    if initial_state == "closed":
        with ingress_runtime.store.contact_lease(phone) as lease, session_factory() as db:
            ingress_runtime.coordinator.resolve_ingress(db, phone, ingress_runtime.clock.now(), lease)
            ingress_runtime.coordinator.close_context(db, phone, ingress_runtime.clock.now(), lease, str(uuid4()))
    elif initial_state == "pause_at_expiry":
        ref = ingress_runtime.pause()
        ingress_runtime.clock.set(ref.paused_until)
    before = ingress_runtime.store.snapshot()
    calls = []
    def forbidden(*args, **kwargs):
        calls.append("unexpected_boundary")
        raise AssertionError("unusable message entered coordination")
    monkeypatch.setattr(ingress_runtime.store, "contact_lease", forbidden)
    monkeypatch.setattr(ingress_runtime, "session_factory", forbidden)
    monkeypatch.setattr(ingress_runtime.coordinator, "accept_ingress", forbidden)
    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", forbidden)
    payload = webhook_payload(from_me=from_me)
    payload["data"]["messages"]["message"] = message
    try:
        response, _ = call_webhook(main_module, payload)
    finally:
        event.remove(engine, "before_cursor_execute", forbidden)
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ignored"}
    assert calls == []
    assert ingress_runtime.store.snapshot() == before
    assert ingress_runtime.processing_broker.calls == []


def test_webhook_missing_message_is_ignored_before_any_lease(main_module, ingress_runtime):
    payload = webhook_payload()
    del payload["data"]["messages"]["message"]
    response, _ = call_webhook(main_module, payload)
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ignored"}
    assert ingress_runtime.lease_calls == ingress_runtime.session_calls == 0
