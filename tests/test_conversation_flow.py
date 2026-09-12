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
