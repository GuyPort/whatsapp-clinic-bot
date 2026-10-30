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
    from app.ai_agent import ClaudeToolAgent, format_for_whatsapp  # noqa: E402
from app.models import ConversationContext  # noqa: E402


PHONE = "5551999999999"


@pytest.mark.parametrize("entrada, saida", [
    ("Voltam em **01/12/2026** (terça-feira)", "Voltam em *01/12/2026* (terça-feira)"),
    ("Custa **R$ 350,00** e a domiciliar **R$ 500,00**", "Custa *R$ 350,00* e a domiciliar *R$ 500,00*"),
    ("**a Dra. Rose está de férias durante todo o mês de novembro**",
     "*a Dra. Rose está de férias durante todo o mês de novembro*"),
    ("Negrito do WhatsApp *já certo* fica igual", "Negrito do WhatsApp *já certo* fica igual"),
    ("Sem formatação nenhuma 😊", "Sem formatação nenhuma 😊"),
    ("Sobrou um ** solto", "Sobrou um  solto"),
    ("**começa numa linha\ntermina na outra**", "começa numa linha\ntermina na outra"),
    ("👉 https://web-production-39323.up.railway.app/agendar/?tel=5551999999999",
     "👉 https://web-production-39323.up.railway.app/agendar/?tel=5551999999999"),
    ("", ""),
    (None, None),
])
def test_format_for_whatsapp(entrada, saida):
    assert format_for_whatsapp(entrada) == saida


def test_process_message_sends_and_stores_whatsapp_bold():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ConversationContext.__table__.create(engine)
    agent = ClaudeToolAgent()
    agent.client = MagicMock()
    agent.client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Os atendimentos voltam em **01/12/2026**!")]
    )

    with sessionmaker(bind=engine)() as db:
        resposta = agent.process_message("Quando a doutora volta?", PHONE, db)
        historico = db.query(ConversationContext).filter_by(phone=PHONE).one().messages

    assert resposta == "Os atendimentos voltam em *01/12/2026*!"
    assert historico[-1]["content"] == resposta
    engine.dispose()
