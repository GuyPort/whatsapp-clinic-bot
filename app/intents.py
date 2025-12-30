from dataclasses import dataclass
from typing import Optional, Tuple
import logging

from anthropic import Anthropic

logger = logging.getLogger(__name__)


@dataclass
class IntentResult:
    label: str
    confidence: float


class IntentClassifier:
    """Camada reutilizável de interpretação de intenções com fallback heurístico + LLM."""

    def __init__(self, client: Anthropic):
        self.client = client

        self._positive_keywords = {
            "sim", "pode", "confirma", "confirmar", "claro", "ok", "okay",
            "perfeito", "isso", "certo", "exato", "vamos", "agendar",
            "marcar", "beleza", "aceito", "tá bom", "ta bom", "show",
            "positivo", "concordo", "fechado", "fechou", "com certeza"
        }
        self._negative_keywords = {
            "não", "nao", "nunca", "jamais", "mudar", "alterar", "trocar",
            "outro", "outra", "diferente", "modificar", "cancelar",
            "desistir", "prefiro outra", "melhor não", "melhor nao",
            "não quero", "nao quero"
        }
        self._human_keywords = {
            "secretária", "secretaria", "atendente", "humano", "pessoa",
            "falar com alguém", "falar com alguem", "ser atendido por humano"
        }

    def classify_confirmation(self, message: str) -> IntentResult:
        """Classifica confirmações em positive/negative/unclear."""
        message_lower = (message or "").lower().strip()
        if not message_lower:
            return IntentResult("unclear", 0.0)

        if any(keyword in message_lower for keyword in self._positive_keywords):
            return IntentResult("positive", 0.9)

        if any(keyword in message_lower for keyword in self._negative_keywords):
            return IntentResult("negative", 0.9)

        return self._llm_confirmation(message_lower)

    def detect_insurance_change(self, message: str) -> bool:
        """Detecta intenção de alterar convênio."""
        message_lower = (message or "").lower()
        keywords = [
            "trocar convênio", "trocar convenio", "mudar convênio", "mudar convenio",
            "alterar convênio", "alterar convenio", "quero particular", "prefiro particular",
            "quero cabergs", "prefiro cabergs", "quero ipe", "prefiro ipe",
            "é particular", "eh particular", "será particular", "sera particular",
            "vou particular", "mudar para particular", "trocar para particular",
            "mudar para cabergs", "trocar para cabergs", "mudar para ipe", "trocar para ipe",
            "convênio errado", "convenio errado", "convênio está errado", "convenio esta errado"
        ]
        if any(keyword in message_lower for keyword in keywords):
            return True
        return self._llm_boolean_check(message, "O paciente está tentando alterar o convênio informado?")

    def detect_human_request(self, message: str) -> bool:
        """Detecta pedido explícito de atendimento humano."""
        message_lower = (message or "").lower()
        if any(keyword in message_lower for keyword in self._human_keywords):
            return True
        return self._llm_boolean_check(message, "O paciente está pedindo para falar com um humano/atendente?")

    def _llm_confirmation(self, message: str) -> IntentResult:
        prompt = (
            "Classifique a intenção do paciente quanto a uma proposta de agendamento.\n"
            "Responda com uma das opções: positive, negative ou unclear.\n"
            f"Mensagem: \"{message}\"\n"
            "Resposta (somente a palavra):"
        )
        try:
            result = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=10,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}]
            )
            if result.content:
                label = result.content[0].text.strip().lower()
                if label in {"positive", "negative", "unclear"}:
                    confidence = 0.6 if label == "unclear" else 0.75
                    return IntentResult(label, confidence)
        except Exception as exc:
            logger.warning(f"Falha no classificador LLM de confirmação: {exc}")

        return IntentResult("unclear", 0.0)

    def _llm_boolean_check(self, message: str, question: str) -> bool:
        prompt = (
            f"{question}\n"
            "Responda apenas SIM ou NÃO.\n"
            f"Mensagem: \"{message}\""
        )
        try:
            result = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=5,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}]
            )
            if result.content:
                answer = result.content[0].text.strip().lower()
                return answer.startswith("sim")
        except Exception as exc:
            logger.warning(f"Falha no classificador booleano LLM: {exc}")
        return False

