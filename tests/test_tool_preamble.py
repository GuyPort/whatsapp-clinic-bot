import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

with patch.dict(os.environ, {
    "DATABASE_URL": "sqlite:///:memory:",
    "ANTHROPIC_API_KEY": "test-key",
    "REDIS_URL": "redis://127.0.0.1:0/0",
}):
    from app.ai_agent import ClaudeToolAgent  # noqa: E402
from app.models import ConversationContext, PausedContact  # noqa: E402


PHONE = "5551999999999"


def texto(t):
    return SimpleNamespace(type="text", text=t)


def tool(nome, tool_id="toolu_1"):
    return SimpleNamespace(type="tool_use", name=nome, input={}, id=tool_id)


def resposta(*blocos):
    return SimpleNamespace(content=list(blocos))


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ConversationContext.__table__.create(engine)
    PausedContact.__table__.create(engine)
    # Mesma configuração de app/database.py
    with sessionmaker(autocommit=False, autoflush=False, bind=engine)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def agent():
    instance = ClaudeToolAgent()
    instance.client = MagicMock()
    return instance


def test_handoff_runs_when_model_writes_before_the_tool(agent, db):
    agent.client.messages.create.return_value = resposta(
        texto("Vou te transferir para a Beatriz, nossa secretária! 😊"),
        tool("request_human_assistance"),
    )

    enviado = agent.process_message("Preciso remarcar minha consulta", PHONE, db)

    assert db.query(PausedContact).filter_by(phone=PHONE).one().reason == "user_requested_human_assistance"
    assert db.info.get("handoff_created_phone") == PHONE
    assert "Beatriz" in enviado and "(51) 99954-6355" in enviado
    assert agent.client.messages.create.call_count == 1


def test_handoff_still_runs_when_tool_comes_first(agent, db):
    agent.client.messages.create.return_value = resposta(tool("request_human_assistance"))

    agent.process_message("Quero cancelar", PHONE, db)

    assert db.query(PausedContact).filter_by(phone=PHONE).count() == 1


def test_clinic_info_runs_when_model_writes_before_the_tool(agent, db):
    agent.client.messages.create.side_effect = [
        resposta(texto("Vou buscar as informações para você 😊"), tool("get_clinic_info")),
        resposta(texto("Atendemos terça, quarta e sexta à tarde.")),
    ]

    enviado = agent.process_message("Me passa as informações da clínica", PHONE, db)

    assert enviado == "Atendemos terça, quarta e sexta à tarde."
    segunda = agent.client.messages.create.call_args_list[1].kwargs["messages"]
    assert segunda[-1]["content"][0] == {
        "type": "tool_result", "tool_use_id": "toolu_1", "content": segunda[-1]["content"][0]["content"],
    }
    assert "Consultório Dra. Rose" in segunda[-1]["content"][0]["content"]


def test_handoff_inside_follow_up_after_preamble(agent, db):
    agent.client.messages.create.side_effect = [
        resposta(tool("get_clinic_info")),
        resposta(texto("Vou te passar para a Beatriz!"), tool("request_human_assistance", "toolu_2")),
    ]

    enviado = agent.process_message("Quero remarcar, quais dias vocês atendem?", PHONE, db)

    assert db.query(PausedContact).filter_by(phone=PHONE).count() == 1
    assert "Beatriz" in enviado


def test_one_tool_per_reply_is_requested(agent, db):
    agent.client.messages.create.return_value = resposta(texto("Oi!"))

    agent.process_message("Oi", PHONE, db)

    assert agent.client.messages.create.call_args.kwargs["tool_choice"] == {
        "type": "auto", "disable_parallel_tool_use": True,
    }


def test_end_conversation_keeps_the_answer_written_with_it(agent, db):
    db.add(ConversationContext(phone=PHONE, messages=[], status="active"))
    db.commit()
    agent.client.messages.create.return_value = resposta(
        texto("O endereço é Rua Dr. Edmundo Lauffer, 299, sala 405. Até logo!"),
        tool("end_conversation"),
    )

    enviado = agent._process_claude_response(
        agent.client.messages.create.return_value, [{"role": "user", "content": "Qual o endereço? Só isso"}], db, PHONE
    )

    assert enviado == "O endereço é Rua Dr. Edmundo Lauffer, 299, sala 405. Até logo!"
    assert db.query(ConversationContext).filter_by(phone=PHONE).count() == 0


def test_end_conversation_without_text_uses_farewell(agent, db):
    enviado = agent._process_claude_response(resposta(tool("end_conversation")), [], db, PHONE)

    assert enviado == "Foi um prazer atender você! Até logo!"


def test_plain_text_reply_runs_no_tool(agent, db):
    agent.client.messages.create.return_value = resposta(texto("A consulta particular custa R$ 350,00."))

    enviado = agent.process_message("Quanto custa?", PHONE, db)

    assert enviado == "A consulta particular custa R$ 350,00."
    assert db.query(PausedContact).count() == 0
    assert agent.client.messages.create.call_count == 1
