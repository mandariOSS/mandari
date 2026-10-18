# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI-Anbieter für das Bürgerportal (Zusammenfassung, KI-Assistent, KI-Verortung).

Es gibt genau einen Weg: ``get_insight_provider()`` liest die zentrale KI-Konfiguration
(``apps.common.ki_anbieter.endpunkt_fuer_insight``, Issue #950) und liefert einen OpenAI-kompatiblen Anbieter
für den geprüften Endpunkt, sonst ``NichtEingerichtet`` (``is_available()`` falsch, kein Aufruf).
"""

from .base import AbstractAIProvider, ChatMessage, ChatResponse
from .openai_kompatibel import KiAnbieterError, OpenAIKompatiblerProvider


class NichtEingerichtet(AbstractAIProvider):
    """Kein freigegebener Endpunkt eingerichtet: nicht verfügbar, jeder Aufruf scheitert ohne Anfrage."""

    def is_available(self) -> bool:
        return False

    @property
    def model_name(self) -> str:
        return ""

    def chat_completion(
        self,
        messages: list[ChatMessage],
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ) -> ChatResponse:
        raise KiAnbieterError("KI ist im Bürgerportal nicht eingerichtet.")


def get_insight_provider() -> AbstractAIProvider:
    """Anbieter für das Bürgerportal aus der KI-Konfiguration; bei jedem Aufruf neu aufgelöst und geprüft."""
    from apps.common.ki_anbieter import endpunkt_fuer_insight

    endpunkt = endpunkt_fuer_insight()
    if endpunkt is None:
        return NichtEingerichtet()
    return OpenAIKompatiblerProvider(endpunkt)


__all__ = [
    "AbstractAIProvider",
    "ChatMessage",
    "ChatResponse",
    "KiAnbieterError",
    "NichtEingerichtet",
    "OpenAIKompatiblerProvider",
    "get_insight_provider",
]
