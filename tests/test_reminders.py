from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from app.models import Appointment, AppointmentStatus
from app.utils import (
    get_brazil_timezone,
    parse_appointment_datetime,
    format_pre_appointment_reminder,
)
from app import scheduler


def test_parse_appointment_datetime_returns_timezone_aware():
    tz = get_brazil_timezone()
    date_str = "20250210"
    time_str = "14:00"

    result = parse_appointment_datetime(date_str, time_str)

    assert result is not None
    assert result.tzinfo == tz
    assert result.strftime("%Y-%m-%d %H:%M") == "2025-02-10 14:00"


def test_format_pre_appointment_reminder_includes_core_information():
    tz = get_brazil_timezone()
    appointment_dt = tz.localize(datetime(2025, 1, 15, 16, 0))

    message = format_pre_appointment_reminder(
        "Ana Maria",
        appointment_dt,
        clinic_info={"endereco": "Rua Exemplo, 123"}
    )

    assert "Ana Maria" in message
    assert "15/01/2025" in message
    assert "16:00" in message
    assert "Rua Exemplo, 123" in message
    assert "não responda" in message.lower()


@pytest.mark.asyncio
async def test_send_appointment_reminders_respects_22h_window(monkeypatch):
    tz = get_brazil_timezone()
    base_now = tz.localize(datetime(2025, 3, 1, 9, 0))

    inside_dt = base_now + timedelta(hours=23)
    inside = Appointment(
        patient_name="Carlos Souza",
        patient_phone="555199999999",
        patient_birth_date="10/10/1980",
        appointment_date=inside_dt.strftime("%Y%m%d"),
        appointment_time=inside_dt.strftime("%H:%M"),
        consultation_type="clinica_geral",
        insurance_plan="Particular",
        status=AppointmentStatus.AGENDADA,
    )
    inside.id = 1

    outside_dt = base_now + timedelta(hours=19)
    outside = Appointment(
        patient_name="Joana Dias",
        patient_phone="555188888888",
        patient_birth_date="05/05/1975",
        appointment_date=outside_dt.strftime("%Y%m%d"),
        appointment_time=outside_dt.strftime("%H:%M"),
        consultation_type="clinica_geral",
        insurance_plan="Particular",
        status=AppointmentStatus.AGENDADA,
    )
    outside.id = 2

    class DummyQuery:
        def __init__(self, items):
            self._items = items

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return list(self._items)

    class DummySession:
        def __init__(self, items):
            self._items = items
            self.commits = 0

        def query(self, model):
            assert model is Appointment
            return DummyQuery(self._items)

        def add(self, _):
            pass

        def commit(self):
            self.commits += 1

    session = DummySession([inside, outside])

    @contextmanager
    def fake_get_db():
        yield session

    sent_messages = []

    async def fake_send_message(phone, message):
        sent_messages.append((phone, message))
        return True

    monkeypatch.setattr(scheduler, "get_db", fake_get_db)
    monkeypatch.setattr(scheduler, "now_brazil", lambda: base_now)
    monkeypatch.setattr(scheduler.whatsapp_service, "send_message", fake_send_message)

    await scheduler.send_appointment_reminders()

    assert len(sent_messages) == 1
    phone, message = sent_messages[0]
    assert phone == "555199999999"
    assert "Carlos Souza" in message
    assert "02/03/2025" in message
    assert inside.reminder_sent_at is not None
    assert session.commits >= 1

    assert outside.reminder_sent_at is None

