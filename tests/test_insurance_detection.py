import pytest
from unittest.mock import Mock, patch

from app.ai_agent import ClaudeToolAgent


class _DummyTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _DummyResponse:
    def __init__(self, text: str):
        self.content = [_DummyTextBlock(text)]


@pytest.fixture()
def agent_with_mock_client():
    with patch("app.ai_agent.Anthropic") as mock_anthropic:
        mock_client = Mock()
        mock_client.messages.create.return_value = _DummyResponse("{}")
        mock_anthropic.return_value = mock_client

        agent = ClaudeToolAgent()
        return agent, mock_client


def test_detect_insurance_prioritizes_affirmative_plan(agent_with_mock_client):
    agent, mock_client = agent_with_mock_client
    mock_client.messages.create.return_value = _DummyResponse(
        '{"insurance_plan":"CABERGS","confidence":"high","justification":"Paciente afirmou CABERGS."}'
    )

    result = agent._detect_insurance_in_message("não tenho IPE, só CABERGS", context=None)

    assert result == "CABERGS"


def test_detect_insurance_defaults_to_particular_when_claude_returns_null(agent_with_mock_client):
    agent, mock_client = agent_with_mock_client
    mock_client.messages.create.return_value = _DummyResponse(
        '{"insurance_plan": null, "confidence":"medium","justification":"Paciente negou possuir convênio."}'
    )

    result = agent._detect_insurance_in_message("não tenho convênio", context=None)

    assert result == "Particular"


def test_detect_insurance_fallback_regex_when_claude_unavailable(agent_with_mock_client):
    agent, mock_client = agent_with_mock_client
    mock_client.messages.create.return_value = _DummyResponse("Resposta inválida")

    result = agent._detect_insurance_in_message("Meu convênio é CABERGS", context=None)

    assert result == "CABERGS"

