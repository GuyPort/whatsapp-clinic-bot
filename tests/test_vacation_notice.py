import os
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

with patch.dict(os.environ, {
    "DATABASE_URL": "sqlite:///:memory:",
    "ANTHROPIC_API_KEY": "test-key",
    "REDIS_URL": "redis://127.0.0.1:0/0",
}):
    from app.ai_agent import ClaudeToolAgent  # noqa: E402


FERIAS = {"inicio": "01/11/2026", "fim": "30/11/2026", "retorno": "01/12/2026"}
PHONE = "5551999999999"


def _agent_with(ferias):
    agent = ClaudeToolAgent()
    agent.clinic_info = {**agent.clinic_info}
    agent.clinic_info.pop("ferias", None)
    if ferias is not None:
        agent.clinic_info["ferias"] = ferias
    agent.system_prompt = agent._create_system_prompt()
    return agent


@pytest.fixture
def agent():
    return _agent_with(FERIAS)


def test_real_config_has_november_vacation():
    agent = ClaudeToolAgent()

    assert agent._get_vacation(date(2026, 11, 15)) == {
        "inicio": date(2026, 11, 1),
        "fim": date(2026, 11, 30),
        "retorno": date(2026, 12, 1),
    }


def test_notice_before_vacation_starts(agent):
    notice = agent._format_vacation_notice(date(2026, 10, 30))

    assert "[AVISO DE FÉRIAS" in notice
    assert "Hoje é sexta-feira, 30/10/2026." in notice
    assert "De 01/11/2026 a 30/11/2026 a Dra. Rose está de férias" in notice
    assert "voltam em 01/12/2026 (terça-feira)" in notice


def test_notice_on_first_and_last_vacation_day(agent):
    assert "Hoje é domingo, 01/11/2026." in agent._format_vacation_notice(date(2026, 11, 1))
    assert "Hoje é segunda-feira, 30/11/2026." in agent._format_vacation_notice(date(2026, 11, 30))


def test_notice_disappears_after_vacation(agent):
    assert agent._format_vacation_notice(date(2026, 12, 1)) == ""
    assert agent._get_vacation(date(2026, 12, 1)) is None


@pytest.mark.parametrize("dia, aparece", [
    (date(2026, 10, 30), True),
    (date(2026, 10, 31), True),
    (date(2026, 11, 1), True),
    (date(2026, 11, 2), True),
    (date(2026, 11, 29), True),
    (date(2026, 11, 30), True),
    (date(2026, 12, 1), False),
    (date(2026, 12, 2), False),
    (date(2027, 11, 15), False),
])
def test_notice_window_day_by_day(agent, dia, aparece):
    assert bool(agent._format_vacation_notice(dia)) is aparece


@pytest.mark.parametrize("instante_utc, aparece", [
    # Servidor em UTC: 21:00 de Brasília já é o dia seguinte em UTC
    (datetime(2026, 12, 1, 2, 59, 59, tzinfo=timezone.utc), True),   # 30/11 23:59:59 BRT
    (datetime(2026, 12, 1, 3, 0, 0, tzinfo=timezone.utc), False),    # 01/12 00:00:00 BRT
    (datetime(2026, 12, 1, 0, 30, tzinfo=timezone.utc), True),       # 30/11 21:30 BRT
    (datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc), True),       # 30/10 23:00 BRT
])
def test_notice_uses_brazil_clock_not_server_clock(agent, instante_utc, aparece):
    class RelogioFixo(datetime):
        @classmethod
        def now(cls, tz=None):
            return instante_utc.astimezone(tz)

    with patch("app.utils.datetime", RelogioFixo):
        prompt = agent._get_system_prompt_for(PHONE)
        info = agent._handle_get_clinic_info({}, None, PHONE)

    assert ("[AVISO DE FÉRIAS" in prompt) is aparece
    assert ("🧳 Férias" in info) is aparece


def test_no_vacation_configured():
    agent = _agent_with(None)

    assert agent._format_vacation_notice(date(2026, 11, 15)) == ""


@pytest.mark.parametrize("ferias", [
    {"inicio": "2026-11-01", "fim": "30/11/2026", "retorno": "01/12/2026"},
    {"inicio": "01/11/2026", "fim": "30/11/2026"},
    {"inicio": "30/11/2026", "fim": "01/11/2026", "retorno": "01/12/2026"},
    {"inicio": "01/11/2026", "fim": "30/11/2026", "retorno": "30/11/2026"},
])
def test_invalid_vacation_config_is_ignored(ferias):
    agent = _agent_with(ferias)

    assert agent._format_vacation_notice(date(2026, 11, 15)) == ""


def test_system_prompt_for_uses_current_date(agent):
    with patch("app.ai_agent.now_brazil", return_value=datetime(2026, 11, 10, 15, 0)):
        prompt = agent._get_system_prompt_for(PHONE)

    assert prompt.startswith(agent.system_prompt.split("__LINK_AGENDAR__")[0])
    assert f"tel={PHONE}" in prompt
    assert "__LINK_" not in prompt
    assert prompt.endswith(agent._format_vacation_notice(date(2026, 11, 10)))

    with patch("app.ai_agent.now_brazil", return_value=datetime(2026, 12, 1, 9, 0)):
        prompt = agent._get_system_prompt_for(PHONE)

    assert "FÉRIAS" not in prompt


def test_tool_follow_up_keeps_notice_and_links(agent):
    tool_call = SimpleNamespace(type="tool_use", name="get_clinic_info", input={}, id="toolu_1")
    resposta_final = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])
    agent.client = MagicMock()
    agent.client.messages.create.return_value = resposta_final

    with patch("app.ai_agent.now_brazil", return_value=datetime(2026, 11, 10, 15, 0)):
        texto = agent._process_claude_response(
            SimpleNamespace(content=[tool_call]), [{"role": "user", "content": "oi"}], None, PHONE
        )

    system = agent.client.messages.create.call_args.kwargs["system"][0]["text"]
    assert texto == "ok"
    assert "[AVISO DE FÉRIAS" in system
    assert f"tel={PHONE}" in system
    assert "__LINK_" not in system


def test_clinic_info_tool_mentions_vacation(agent):
    with patch("app.ai_agent.now_brazil", return_value=datetime(2026, 11, 10, 15, 0)):
        info = agent._handle_get_clinic_info({}, None, PHONE)

    assert "🧳 Férias: Dra. Rose de férias de 01/11/2026 a 30/11/2026" in info
    assert "Atendimentos voltam em 01/12/2026." in info

    with patch("app.ai_agent.now_brazil", return_value=datetime(2026, 12, 1, 9, 0)):
        info = agent._handle_get_clinic_info({}, None, PHONE)

    assert "Férias" not in info
