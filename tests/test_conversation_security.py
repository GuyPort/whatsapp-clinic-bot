"""Authenticated ingress tests; only synthetic headers, identities and stores."""
import asyncio
import hashlib
import json
import logging

import pytest


@pytest.mark.parametrize("failed", [None, "secret", "sql", "redis", "epoch", "broker"])
def test_ready_endpoint_exact_shape_and_health_never_probes(app_client, ingress_runtime, failed):
    from app.conversation_state import DependencyName
    if failed:
        ingress_runtime.dependencies[DependencyName(failed)] = False
    calls = ingress_runtime.readiness_calls
    assert app_client.get("/health").status_code == 200
    assert ingress_runtime.readiness_calls == calls
    response = app_client.get("/ready")
    assert response.status_code == (503 if failed else 200)
    assert response.json() == {"status": "not_ready" if failed else "ready", "dependencies": {
        name: "not_ready" if name == failed else "ready" for name in ("secret", "sql", "redis", "epoch", "broker")}}
    assert ingress_runtime.lease_calls == ingress_runtime.session_calls == 0


@pytest.mark.parametrize("failure", ["missing", "exception", "empty"])
def test_ready_public_failure_hides_values_and_partial_reports(main_module, app_client, ingress_runtime, monkeypatch, failure, caplog):
    from app.conversation_state import ReadinessReport
    if failure == "missing":
        monkeypatch.delattr(main_module.app.state, "conversation_runtime")
    else:
        def check():
            if failure == "exception":
                raise RuntimeError("private-token redis://private.invalid")
            return ReadinessReport(())
        monkeypatch.setattr(ingress_runtime, "readiness_status", check)
    response = app_client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "dependencies": {
        name: "not_ready" for name in ("secret", "sql", "redis", "epoch", "broker")}}
    assert "private-token" not in caplog.text + response.text


def epoch_cli():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parents[1] / "scripts" / "rotate_conversation_epoch.py"
    assert path.exists(), "guarded epoch CLI missing"
    spec = importlib.util.spec_from_file_location("synthetic_epoch_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("query", ["socket_timeout=60", "socket_connect_timeout=60", "retry_on_timeout=true",
    "retry=private-token", "retry_on_error=TimeoutError", "SOCKET_TIMEOUT=60", "Socket_Timeout=60",
    "socket_timeout=2&socket_timeout=60", "%73ocket_timeout=60", "%2573ocket_timeout=60",
    "socket%5ftimeout=60", "db=0&retry_on_timeout=true", "db=0%26socket_timeout%3D60",
    "db=0;socket_timeout=60", "db=0&db=1", "ssl_connection_timeout=60", "health_check_interval=999"])
def test_ready_redis_url_options_cannot_override_timeout_or_retry(query, monkeypatch, caplog):
    from app import conversation_recovery as api
    assert hasattr(api, "bounded_redis_client"), "central bounded Redis construction missing"
    def factory(*args, **kwargs):
        pytest.fail("unsafe URL reached client construction")
    with pytest.raises(ValueError, match="invalid Redis configuration") as raised:
        api.bounded_redis_client("rediss://synthetic:private-token@synthetic.invalid/0?" + query, client_factory=factory)
    assert query not in str(raised.value)
    assert "private-token" not in str(raised.value) + caplog.text


@pytest.mark.parametrize("override", ["socket_timeout", "socket_connect_timeout", "retry_on_timeout", "retry", "retry_on_error"])
def test_ready_redis_effective_options_are_validated_after_factory(override):
    from types import SimpleNamespace
    from redis.retry import Retry
    from redis.backoff import NoBackoff
    from app import conversation_recovery as api
    assert hasattr(api, "bounded_redis_client"), "central bounded Redis construction missing"
    def factory(url, **options):
        options[override] = {"socket_timeout": 60, "socket_connect_timeout": None, "retry_on_timeout": True,
                             "retry": Retry(NoBackoff(), 1), "retry_on_error": [TimeoutError]}[override]
        return SimpleNamespace(connection_pool=SimpleNamespace(connection_kwargs=options))
    with pytest.raises(ValueError, match="invalid Redis configuration"):
        api.bounded_redis_client("redis://synthetic.invalid/0", client_factory=factory)


@pytest.mark.parametrize("scheme", ["redis", "rediss"])
def test_ready_redis_safe_credentials_are_not_exposed_and_effective_retry_is_zero(scheme, monkeypatch, caplog):
    import socket
    from app import conversation_recovery as api
    assert hasattr(api, "bounded_redis_client"), "central bounded Redis construction missing"
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    client = api.bounded_redis_client(scheme + "://synthetic:private-token@synthetic.invalid/0?db=1")
    options = client.connection_pool.connection_kwargs
    assert options["socket_timeout"] == options["socket_connect_timeout"] == 2
    assert options["retry_on_timeout"] is False
    assert options["retry_on_error"] == []
    assert options["retry"]._retries == 0
    assert options["db"] == 1
    assert "private-token" not in caplog.text
    client.close()


def test_epoch_cli_rejects_unsafe_redis_url_with_fixed_class_before_probe(monkeypatch, capsys):
    from app import conversation_recovery as api
    cli = epoch_cli()
    monkeypatch.setenv("REDIS_URL", "redis://synthetic:private-token@synthetic.invalid/0?socket_timeout=60")
    monkeypatch.setenv("CONVERSATION_COORDINATION_EPOCH", "00000000-0000-4000-8000-000000000002")
    touched = []
    monkeypatch.setattr(api, "bounded_sql_dependencies", lambda url: touched.append(True) or (_ for _ in ()).throw(RuntimeError("private-token")))
    code = cli.main(["--expected-current-epoch", "00000000-0000-4000-8000-000000000001",
                     "--new-epoch", "00000000-0000-4000-8000-000000000002", "--confirm-quiescent"])
    assert code == 2
    assert touched == []
    assert capsys.readouterr() == ("epoch_rotation_rejected\n", "")


def test_epoch_cli_import_and_rejected_args_never_construct_dependencies(monkeypatch, capsys):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert not name.startswith("app"), "CLI imported application before validation"
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    cli = epoch_cli()
    assert cli.main(["--private-token"]) == 2
    assert capsys.readouterr() == ("epoch_rotation_rejected\n", "")


@pytest.mark.parametrize("fault", ["owner", "cas_race", "acl", "acl_missing", "persistence", "same", "invalid"])
def test_epoch_rotation_rejects_faults_before_any_write_and_invalidates_owner(fault):
    from dataclasses import replace
    from uuid import UUID
    from app.simple_config import settings
    from app.conversation_state import ConversationConfig, ConversationDomainError
    from app.conversation_redis import EpochStore, GLOBAL_EPOCH_KEY
    from tests.fakes import InMemoryConversationStore
    store = InMemoryConversationStore(ConversationConfig.from_settings(settings))
    old = store.config.coordination_epoch
    new = UUID("00000000-0000-4000-8000-000000000002")
    epoch = EpochStore(store.client, replace(store.config, coordination_epoch=new))
    with store.contact_lease("5551999990000") as lease:
        if fault == "owner":
            assert epoch.rotate(old, new) == new
            with pytest.raises(ConversationDomainError):
                lease.assert_owned()
            return
        if fault == "cas_race":
            store.client.before_atomic = lambda: store.client.values.update({GLOBAL_EPOCH_KEY: str(new)})
        elif fault == "acl":
            store.client.denied_commands.add(("SET", GLOBAL_EPOCH_KEY))
        elif fault == "acl_missing":
            store.client.acl_check_available = False
        elif fault == "persistence":
            store.client.persistence["aof_last_write_status"] = "err"
        before = store.global_epoch_writes
        with pytest.raises(ConversationDomainError):
            epoch.rotate(old, old if fault == "same" else "private-token" if fault == "invalid" else new)
        assert store.global_epoch_writes == before


def test_recovery_mutation_pages_only_expired_ids_and_terminal_index_is_empty(admin_runtime):
    from uuid import uuid4
    from datetime import timedelta
    from app import conversation_state as domain
    from app.conversation_redis import MUTATION_INDEX_KEY
    rt = admin_runtime
    phone = "5551999990000"
    with rt.store.contact_lease(phone) as lease, rt.session_factory() as db:
        rt.coordinator.resolve_ingress(db, phone, rt.clock.now(), lease)
        operation = str(uuid4())
        target = domain.MutationTarget("PAUSE_MANUAL", "synthetic", "synthetic", domain.ConversationCycle.PAUSED)
        rt.store.prepare_mutation(phone, target.kind, target.fingerprint, lease, operation, rt.clock.now(), target=target)
        assert all(phone not in member and operation not in member and str(rt.store.config.coordination_epoch) not in member
                   for member in rt.store.client.sets[MUTATION_INDEX_KEY])
        assert rt.store.recoverable_mutations(rt.clock.now()).mutations == ()
        rt.clock.advance(timedelta(seconds=600))
        # The lease has expired; recovery must acquire a new owner below.
    page = rt.store.recoverable_mutations(rt.clock.now())
    assert page.mutations == ((phone, operation),)
    with rt.store.contact_lease(phone) as lease:
        assert rt.store.recover_mutation(phone, operation, rt.clock.now(), lease) == "aborted"
    assert rt.store.client.sets.get(MUTATION_INDEX_KEY, set()) == set()
    assert rt.store.recoverable_mutations(rt.clock.now()).mutations == ()
    assert phone not in repr(page) and operation not in repr(page)


@pytest.mark.parametrize("fault,code,output", [(None, 0, "succeeded"), ("confirmation", 2, "rejected"),
    ("invalid", 2, "rejected"), ("same", 2, "rejected"), ("config", 2, "rejected"),
    ("cas", 3, "failed"), ("dependency", 3, "failed"), ("attestation", 3, "failed")])
def test_epoch_rotation_cli_is_injected_guarded_cas_and_sanitized(fault, code, output, capsys):
    from dataclasses import replace
    from uuid import UUID
    from app.simple_config import settings
    from app.conversation_state import ConversationConfig
    from tests.fakes import InMemoryConversationStore
    from app import conversation_redis
    cli = epoch_cli()
    old = UUID("00000000-0000-4000-8000-000000000001")
    new = UUID("00000000-0000-4000-8000-000000000002")
    config = ConversationConfig.from_settings(settings)
    store = InMemoryConversationStore(config)
    assert hasattr(conversation_redis, "EpochStore"), "epoch CAS store missing"
    epoch = conversation_redis.EpochStore(store.client, replace(config, coordination_epoch=new))
    args = ["--expected-current-epoch", str(old), "--new-epoch", str(new), "--confirm-quiescent"]
    configured = new
    if fault == "confirmation":
        args.pop()
    elif fault == "invalid":
        args[3] = "private-token"
    elif fault == "same":
        args[3] = str(old)
    elif fault == "config":
        configured = old
    elif fault == "cas":
        store.client.values[conversation_redis.GLOBAL_EPOCH_KEY] = str(new)
    elif fault == "attestation":
        store.client.memory["maxmemory_policy"] = "allkeys-lru"
    def dependencies():
        if fault == "dependency":
            raise RuntimeError("private-token redis://private.invalid")
        return True
    result = cli.main(args, configured_epoch=configured, epoch_store=epoch, dependency_probe=dependencies)
    assert result == code
    assert capsys.readouterr() == (f"epoch_rotation_{output}\n", "")
    assert store.global_epoch_writes == (1 if code == 0 else 0)
    if code == 0:
        assert epoch.read() == new
        assert cli.main(args, configured_epoch=new, epoch_store=epoch, dependency_probe=lambda: True) == 3
        assert store.global_epoch_writes == 1

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
