"""Authenticated ingress tests; only synthetic headers, identities and stores."""
import asyncio
import hashlib
import json
import logging

import pytest

from app.conversation_state import DependencyName
from tests.fakes import WebhookRequest, webhook_payload


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
