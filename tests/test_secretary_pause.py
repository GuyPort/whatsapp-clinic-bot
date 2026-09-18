import asyncio
import importlib
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

with patch.dict(os.environ, {
    "DATABASE_URL": "sqlite:///:memory:",
    "ANTHROPIC_API_KEY": "test-key",
    "REDIS_URL": "redis://127.0.0.1:0/0",
}):
    from app import main  # noqa: E402
from app.models import ConversationContext, PausedContact  # noqa: E402


PHONE = "5551999999999"


@pytest.fixture
def database(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ConversationContext.__table__.create(engine)
    PausedContact.__table__.create(engine)
    session_factory = sessionmaker(bind=engine)

    @contextmanager
    def get_test_db():
        with session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    monkeypatch.setattr(main, "get_db", get_test_db)
    yield session_factory
    engine.dispose()


def webhook_payload(message, *, from_me=True):
    return {
        "event": "messages.upsert",
        "data": {
            "messages": {
                "key": {
                    "remoteJid": f"{PHONE}@s.whatsapp.net",
                    "fromMe": from_me,
                },
                "message": message,
            }
        },
    }


class JsonRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


def test_secretary_command_renews_existing_pause_for_24_hours(database):
    with database() as db:
        db.add(PausedContact(
            phone=PHONE,
            paused_until=datetime.utcnow() + timedelta(hours=1),
            reason="secretary_dashboard_pause",
        ))
        db.commit()

    before = datetime.utcnow()
    result = asyncio.run(main.whatsapp_webhook(
        JsonRequest(webhook_payload({"conversation": " /pausar "}))
    ))

    with database() as db:
        pause = db.get(PausedContact, PHONE)
        assert pause is not None
        assert before + timedelta(hours=23, minutes=59) < pause.paused_until
        assert pause.paused_until < datetime.utcnow() + timedelta(hours=24, minutes=1)
        assert pause.reason == "secretary_manual_pause"
    assert result["action"] == "secretary_pause"


def test_secretary_command_uses_patient_number_for_linked_device(database):
    payload = webhook_payload({"conversation": "/pausar"})
    payload["data"]["messages"]["key"].update({
        "remoteJid": "12345@lid",
        "cleanedSenderPn": f"{PHONE}@s.whatsapp.net",
    })

    result = asyncio.run(main.whatsapp_webhook(JsonRequest(payload)))

    with database() as db:
        assert db.get(PausedContact, PHONE) is not None
        assert db.get(PausedContact, "12345@lid") is None
    assert result["action"] == "secretary_pause"


def test_secretary_command_in_message_sent_event_pauses_patient(database):
    payload = webhook_payload({"conversation": "/pausar"})
    payload["event"] = "message.sent"
    payload["data"] = {
        **payload["data"]["messages"],
        "success": True,
    }

    result = asyncio.run(main.whatsapp_webhook(JsonRequest(payload)))

    with database() as db:
        pause = db.get(PausedContact, PHONE)
        assert pause is not None
        assert pause.reason == "secretary_manual_pause"
    assert result["action"] == "secretary_pause"


def test_failed_message_sent_event_does_not_pause_patient(database):
    payload = webhook_payload({"conversation": "/pausar"})
    payload["event"] = "message.sent"
    payload["data"] = {
        **payload["data"]["messages"],
        "success": False,
    }

    result = asyncio.run(main.whatsapp_webhook(JsonRequest(payload)))

    with database() as db:
        assert db.get(PausedContact, PHONE) is None
    assert result["status"] == "ignored"


def test_queued_reply_is_not_sent_after_secretary_pauses(database, monkeypatch):
    with database() as db:
        db.add(PausedContact(
            phone=PHONE,
            paused_until=datetime.utcnow() + timedelta(hours=24),
            reason="secretary_manual_pause",
        ))
        db.commit()

    sent = []
    def fake_send(phone, text, pre_send_check=None):
        if pre_send_check and not pre_send_check():
            return None
        sent.append(text)
        return True

    monkeypatch.setattr(main, "_send_message_sync", fake_send)

    main.send_message_task.run(PHONE, "Resposta antiga do bot")

    assert sent == []


def test_patient_text_is_silent_after_secretary_command(database, monkeypatch):
    asyncio.run(main.whatsapp_webhook(
        JsonRequest(webhook_payload({"conversation": "/pausar"}))
    ))

    processed = []
    queued = []
    monkeypatch.setattr(main.whatsapp_service, "acquire_chat_lock", lambda phone: None)
    monkeypatch.setattr(
        main.ai_agent, "process_message",
        lambda message, phone, db: processed.append(message) or "Resposta do bot",
    )
    monkeypatch.setattr(main.send_message_task, "delay", lambda *args: queued.append(args))

    main.process_message_task.run(PHONE, "Oi, ainda está aí?")

    assert processed == []
    assert queued == []


def test_bot_resumes_on_next_message_after_pause_expires(database, monkeypatch):
    with database() as db:
        db.add(PausedContact(
            phone=PHONE,
            paused_until=datetime.utcnow() - timedelta(minutes=1),
            reason="secretary_manual_pause",
        ))
        db.commit()

    queued = []
    monkeypatch.setattr(main.whatsapp_service, "acquire_chat_lock", lambda phone: None)
    monkeypatch.setattr(main.ai_agent, "process_message", lambda message, phone, db: "Olá novamente")
    monkeypatch.setattr(
        main.send_message_task,
        "delay",
        lambda *args: queued.append(args) or SimpleNamespace(id="queued"),
    )

    main.process_message_task.run(PHONE, "Oi")

    with database() as db:
        assert db.get(PausedContact, PHONE) is None
    assert queued == [(PHONE, "Olá novamente")]


def test_handoff_reply_can_be_sent_during_patient_requested_pause(database, monkeypatch):
    with database() as db:
        db.add(PausedContact(
            phone=PHONE,
            paused_until=datetime.utcnow() + timedelta(hours=24),
            reason="user_requested_human_assistance",
        ))
        db.commit()

    sent = []
    def fake_send(phone, text, pre_send_check=None):
        if pre_send_check and not pre_send_check():
            return None
        sent.append(text)
        return True

    monkeypatch.setattr(main, "_send_message_sync", fake_send)

    main.send_message_task.run(PHONE, "Vou transferir para Beatriz", True)

    assert sent == ["Vou transferir para Beatriz"]


def test_secretary_pause_blocks_handoff_queued_before_command(database, monkeypatch):
    with database() as db:
        db.add(PausedContact(
            phone=PHONE,
            paused_until=datetime.utcnow() + timedelta(hours=24),
            reason="secretary_manual_pause",
        ))
        db.commit()

    sent = []
    monkeypatch.setattr(
        main, "_send_message_sync",
        lambda phone, text, pre_send_check=None: sent.append(text) or True,
    )

    main.send_message_task.run(PHONE, "Vou transferir para Beatriz", True)

    assert sent == []


@pytest.mark.parametrize(
    "reason,hours",
    [("secretary_manual_pause", 24), ("secretary_dashboard_pause", 48)],
)
def test_in_flight_handoff_preserves_manual_pause(database, reason, hours):
    until = datetime.utcnow() + timedelta(hours=hours)
    with database() as db:
        db.add(PausedContact(
            phone=PHONE,
            paused_until=until,
            reason=reason,
        ))
        db.commit()

    with database() as db:
        main.ai_agent._handle_request_human_assistance({}, db, PHONE)

    with database() as db:
        pause = db.get(PausedContact, PHONE)
        assert pause.reason == reason
        assert pause.paused_until == until


def test_patient_handoff_reply_is_enqueued_as_handoff(database, monkeypatch):
    queued = []
    monkeypatch.setattr(main.whatsapp_service, "acquire_chat_lock", lambda phone: None)
    monkeypatch.setattr(
        main.ai_agent,
        "process_message",
        lambda message, phone, db: main.ai_agent._handle_request_human_assistance({}, db, phone),
    )
    monkeypatch.setattr(
        main.send_message_task,
        "delay",
        lambda *args: queued.append(args) or SimpleNamespace(id="queued"),
    )

    main.process_message_task.run(PHONE, "Quero falar com Beatriz")

    with database() as db:
        pause = db.get(PausedContact, PHONE)
        assert pause.reason == "user_requested_human_assistance"
    assert len(queued) == 1
    assert queued[0][0] == PHONE
    assert queued[0][2] is True


def test_unrelated_reply_does_not_inherit_another_handoff_pause(database, monkeypatch):
    queued = []
    monkeypatch.setattr(main.whatsapp_service, "acquire_chat_lock", lambda phone: None)

    def generate_ordinary_reply(message, phone, db):
        db.add(PausedContact(
            phone=phone,
            paused_until=datetime.utcnow() + timedelta(hours=24),
            reason="user_requested_human_assistance",
        ))
        db.commit()
        return "Resposta antiga"

    monkeypatch.setattr(main.ai_agent, "process_message", generate_ordinary_reply)
    monkeypatch.setattr(
        main.send_message_task,
        "delay",
        lambda *args: queued.append(args) or SimpleNamespace(id="queued"),
    )

    main.process_message_task.run(PHONE, "Dúvida anterior")

    assert queued == [(PHONE, "Resposta antiga")]


def test_handoff_tool_returns_its_notice_without_another_ai_reply(database, monkeypatch):
    extra_ai_calls = []

    def extra_ai_response(**kwargs):
        extra_ai_calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(
            type="text", text="Outra resposta automática"
        )])

    monkeypatch.setattr(
        main.ai_agent,
        "client",
        SimpleNamespace(messages=SimpleNamespace(create=extra_ai_response)),
    )
    tool_response = SimpleNamespace(content=[SimpleNamespace(
        type="tool_use",
        name="request_human_assistance",
        input={},
        id="handoff",
    )])

    with database() as db:
        notice = main.ai_agent._process_claude_response(tool_response, [], db, PHONE)

    assert notice.startswith("Vou transferir você para nossa secretária Beatriz")
    assert extra_ai_calls == []


def test_ai_handoff_is_enqueued_as_the_only_notice(database, monkeypatch):
    tool_response = SimpleNamespace(content=[SimpleNamespace(
        type="tool_use",
        name="request_human_assistance",
        input={},
        id="handoff",
    )])
    ai_calls = []

    def fake_ai_response(**kwargs):
        ai_calls.append(kwargs)
        return tool_response

    queued = []
    monkeypatch.setattr(main.whatsapp_service, "acquire_chat_lock", lambda phone: None)
    monkeypatch.setattr(
        main.ai_agent,
        "client",
        SimpleNamespace(messages=SimpleNamespace(create=fake_ai_response)),
    )
    monkeypatch.setattr(
        main.send_message_task,
        "delay",
        lambda *args: queued.append(args) or SimpleNamespace(id="queued"),
    )

    main.process_message_task.run(PHONE, "Quero falar com Beatriz")

    assert len(ai_calls) == 1
    assert len(queued) == 1
    assert queued[0][0] == PHONE
    assert queued[0][1].startswith("Vou transferir você para nossa secretária Beatriz")
    assert queued[0][2] is True


def test_handoff_failure_does_not_expose_database_error_to_patient():
    class FailingDb:
        def query(self, model):
            raise RuntimeError("database-private-details")

        def rollback(self):
            pass

        info = {}

    result = main.ai_agent._handle_request_human_assistance({}, FailingDb(), PHONE)

    assert "database-private-details" not in result


def test_media_without_text_does_not_enqueue_reply_during_pause(database, monkeypatch):
    with database() as db:
        db.add(PausedContact(
            phone=PHONE,
            paused_until=datetime.utcnow() + timedelta(hours=24),
            reason="secretary_manual_pause",
        ))
        db.commit()

    queued = []
    monkeypatch.setattr(main.send_message_task, "delay", lambda *args: queued.append(args))

    asyncio.run(main.whatsapp_webhook(
        JsonRequest(webhook_payload({"imageMessage": {}}, from_me=False))
    ))

    assert queued == []


def test_linked_device_media_is_silent_during_pause(database, monkeypatch):
    with database() as db:
        db.add(PausedContact(
            phone=PHONE,
            paused_until=datetime.utcnow() + timedelta(hours=24),
            reason="secretary_manual_pause",
        ))
        db.commit()

    payload = webhook_payload({"imageMessage": {}}, from_me=False)
    payload["data"]["messages"]["key"].update({
        "remoteJid": "12345@lid",
        "cleanedSenderPn": f"{PHONE}@s.whatsapp.net",
    })
    queued = []
    monkeypatch.setattr(main.send_message_task, "delay", lambda *args: queued.append(args))

    result = asyncio.run(main.whatsapp_webhook(JsonRequest(payload)))

    assert result == {"status": "ignored", "reason": "contact paused"}
    assert queued == []


def test_secretary_command_does_not_claim_success_when_pause_cannot_be_saved(monkeypatch):
    class FailingDb:
        def query(self, model):
            raise RuntimeError("database unavailable")

        def rollback(self):
            pass

    @contextmanager
    def failing_db():
        yield FailingDb()

    monkeypatch.setattr(main, "get_db", failing_db)

    with pytest.raises(HTTPException) as error:
        asyncio.run(main.whatsapp_webhook(
            JsonRequest(webhook_payload({"conversation": "/pausar"}))
        ))

    assert error.value.status_code == 500


def test_send_rechecks_pause_after_waiting_for_rate_limit(database, monkeypatch):
    whatsapp_module = importlib.import_module("app.whatsapp_service")
    state = {"paused": False}
    sent = []

    class PausingLock:
        def acquire(self, blocking):
            state["paused"] = True
            return True

        def owned(self):
            return True

        def release(self):
            pass

    async def fake_provider_send(phone, message):
        sent.append(message)
        return True

    monkeypatch.setattr(whatsapp_module, "Lock", lambda *args, **kwargs: PausingLock())
    monkeypatch.setattr(main.whatsapp_service, "redis_client", object())
    monkeypatch.setattr(main.whatsapp_service, "_send_message_internal", fake_provider_send)

    result = asyncio.run(main.whatsapp_service.send_message(
        PHONE, "Resposta antiga", pre_send_check=lambda: not state["paused"]
    ))

    assert result is None
    assert sent == []


def test_send_task_rechecks_database_at_provider_boundary(database, monkeypatch):
    sent = []

    async def provider_send(phone, message, pre_send_check=None):
        with database() as db:
            db.add(PausedContact(
                phone=phone,
                paused_until=datetime.utcnow() + timedelta(hours=24),
                reason="secretary_manual_pause",
            ))
            db.commit()
        if pre_send_check is None or pre_send_check():
            sent.append(message)
            return True
        return None

    monkeypatch.setattr(main.whatsapp_service, "send_message", provider_send)

    main.send_message_task.run(PHONE, "Resposta antiga")

    assert sent == []


def test_send_task_never_calls_provider_when_pause_state_is_unknown(monkeypatch):
    class FailingDb:
        def query(self, model):
            raise RuntimeError("database unavailable")

    @contextmanager
    def failing_db():
        yield FailingDb()

    sent = []
    monkeypatch.setattr(main, "get_db", failing_db)
    monkeypatch.setattr(
        main, "_send_message_sync",
        lambda *args, **kwargs: sent.append(args) or True,
    )

    with pytest.raises(Exception):
        main.send_message_task.run(PHONE, "Resposta antiga")

    assert sent == []
