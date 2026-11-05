# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Claude API (Anthropic) für die Zusammenfassungen im Bürgerportal: Einzelaufruf und Message Batches.

Nur für Zusammenfassungen öffentlicher Dokumente; KI-Assistent und KI-Verortung nutzen weiter den
OpenAI-kompatiblen Endpunkt (``openai_kompatibel.py``). Zugang, Modell und Budget kommen aus der zentralen
KI-Konfiguration (``apps.common.ki_anbieter.claude_fuer_zusammenfassung``); der Host steht dort gegen
``KI_ERLAUBTE_HOSTS`` geprüft, Schlüssel und Adresse gehen ausdrücklich an das SDK.

- **Modell:** Standard ``claude-haiku-5-5``. Es denkt adaptiv; ``EFFORT`` hält das für Zusammenfassungen knapp.
  Sampling-Parameter (``temperature``) nimmt das Modell nicht an, sie gehen nicht mit.
- **Antwort:** nur Textblöcke; Denkblöcke zählen nie als Antwort. Abgelehnt (``refusal``) oder abgeschnitten
  (``max_tokens``) ergibt ``KiAnbieterError``: Eine halbe Zusammenfassung wird nicht gespeichert.
- **Kosten:** Jede Antwort wird verbucht (``insight_ai.kosten``), auch abgelehnte und abgeschnittene. Ist das
  Monatsbudget erschöpft, gibt es keinen Einzelaufruf mehr (``BudgetErschoepftError``).
- **Fehlermeldungen** nennen nur Art und HTTP-Status, nie Inhalt, Adresse oder Schlüssel.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from .. import kosten
from .base import STANDARD_MAX_AUSGABE, AbstractAIProvider, ChatMessage, ChatResponse
from .openai_kompatibel import KiAnbieterError

if TYPE_CHECKING:
    from apps.common.ki_anbieter import ClaudeEndpunkt

logger = logging.getLogger(__name__)

#: Denktiefe für Zusammenfassungen: knapp (Kosten, Dauer); die Länge regelt der Prompt
EFFORT: Final = "low"
#: Zeitgrenze eines Einzelaufrufs in Sekunden
TIMEOUT: Final = 180.0


class BudgetErschoepftError(KiAnbieterError):
    """Das Monatsbudget der Zusammenfassungen ist ausgeschöpft: kein Aufruf."""


class ClaudeApiError(KiAnbieterError):
    """Fehler der Claude API mit HTTP-Status (``None``: kein Status, etwa Zeitüberschreitung)."""

    def __init__(self, meldung: str, status_code: int | None = None) -> None:
        super().__init__(meldung)
        self.status_code = status_code


@dataclass(frozen=True)
class BatchErgebnis:
    """Ergebnis einer Anfrage aus einem Message Batch."""

    custom_id: str
    #: ``succeeded``, ``errored``, ``canceled`` oder ``expired``
    art: str
    #: Zusammenfassung; leer, wenn keine brauchbare Antwort vorliegt (dann ``fehler``)
    text: str
    fehler: str = ""
    modell: str = ""
    #: ``usage`` der Antwort (nur bei ``succeeded``), Grundlage der Kosten
    usage: Any = None


def antwort_text(nachricht: Any) -> str:
    """
    Text einer Antwort der Claude API (nur Textblöcke). Abgelehnt, abgeschnitten oder leer: ``KiAnbieterError``.
    """
    grund = getattr(nachricht, "stop_reason", None)
    if grund == "refusal":
        raise KiAnbieterError("Claude API hat die Anfrage abgelehnt (refusal)")
    if grund == "max_tokens":
        raise KiAnbieterError("Antwort der Claude API abgeschnitten (max_tokens)")
    teile = [
        str(getattr(block, "text", "") or "")
        for block in (getattr(nachricht, "content", None) or [])
        if getattr(block, "type", "") == "text"
    ]
    text = "".join(teile).strip()
    if not text:
        raise KiAnbieterError("Antwort der Claude API ohne Text")
    return text


def _fehler(fehler: Exception) -> ClaudeApiError:
    """Feste Meldung zu einer Ausnahme des SDK (nur Art und Status, nie deren Text)."""
    import anthropic

    if isinstance(fehler, anthropic.APIStatusError):
        status = getattr(fehler, "status_code", None)
        return ClaudeApiError(
            f"Claude API meldet HTTP {status if status is not None else '?'}",
            status if isinstance(status, int) else None,
        )
    if isinstance(fehler, anthropic.APITimeoutError):
        return ClaudeApiError("Claude API: Zeitüberschreitung")
    if isinstance(fehler, anthropic.APIConnectionError):
        return ClaudeApiError("Claude API nicht erreichbar")
    return ClaudeApiError(f"Claude API: {type(fehler).__name__}")


class ClaudeZusammenfassung(AbstractAIProvider):
    """Zusammenfassungen über die Claude API; ``client`` nur für Tests (Ersatz ohne Netz)."""

    def __init__(self, endpunkt: ClaudeEndpunkt, *, client: Any = None) -> None:
        self.endpunkt = endpunkt
        self._client = client

    def __repr__(self) -> str:
        return f"ClaudeZusammenfassung({self.endpunkt!r})"

    @property
    def client(self) -> Any:
        if self._client is None:
            from apps.common.ki_anbieter import claude_client

            self._client = claude_client(self.endpunkt, timeout=TIMEOUT)
        return self._client

    def is_available(self) -> bool:
        return bool(self.endpunkt.api_key and self.endpunkt.modell)

    @property
    def model_name(self) -> str:
        return self.endpunkt.modell

    @property
    def max_output_tokens(self) -> int:
        return self.endpunkt.max_output_tokens or STANDARD_MAX_AUSGABE

    @property
    def zusammenfassung_hinweis(self) -> str:
        return self.endpunkt.zusammenfassung_hinweis

    def budget_rest(self) -> Any:
        return kosten.budget_rest(self.endpunkt)

    def parameter(self, messages: list[ChatMessage], max_tokens: int) -> dict[str, Any]:
        """Anfrage an die Messages API: Systemnachrichten als ``system``, sonst die Nachrichten in Reihenfolge."""
        system = "\n\n".join(msg.content for msg in messages if msg.role == "system" and msg.content)
        params: dict[str, Any] = {
            "model": self.endpunkt.modell,
            "max_tokens": max(1, int(max_tokens)),
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages if msg.role != "system"],
            "output_config": {"effort": EFFORT},
        }
        if system:
            params["system"] = system
        return params

    def chat_completion(
        self,
        messages: list[ChatMessage],
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ) -> ChatResponse:
        """
        Einzelaufruf (Klick auf „Zusammenfassen“). ``temperature`` wird nicht gesendet (vom Modell nicht angenommen).
        """
        if self.budget_rest() <= 0:
            raise BudgetErschoepftError("Monatsbudget der Zusammenfassungen ausgeschöpft")
        params = self.parameter(messages, max_tokens)
        try:
            antwort = self.client.messages.create(**params)
        except Exception as fehler:
            raise _fehler(fehler) from fehler
        modell = str(getattr(antwort, "model", "") or self.endpunkt.modell)
        usage = getattr(antwort, "usage", None)
        betrag = kosten.verbuchen(art="sofort", modell=modell, usage=usage)
        eingabe = int(getattr(usage, "input_tokens", 0) or 0)
        ausgabe = int(getattr(usage, "output_tokens", 0) or 0)
        logger.info(
            "KI-Aufruf: anbieter=anthropic host=%s modell=%s eingabe=%d ausgabe=%d kosten_usd=%s",
            self.endpunkt.host,
            modell,
            eingabe,
            ausgabe,
            betrag,
        )
        text = antwort_text(antwort)
        return ChatResponse(
            content=text, model=modell, input_tokens=eingabe, output_tokens=ausgabe, total_tokens=eingabe + ausgabe
        )

    # --- Message Batches -----------------------------------------------------------------------------------------

    def batch_einreichen(self, anfragen: list[tuple[str, list[ChatMessage], int]]) -> str:
        """Reicht ``(custom_id, Nachrichten, max_tokens)`` als einen Message Batch ein; Rückgabe: Batch-Kennung."""
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        requests = [
            Request(
                custom_id=custom_id,
                params=cast(MessageCreateParamsNonStreaming, self.parameter(nachrichten, grenze)),
            )
            for custom_id, nachrichten, grenze in anfragen
        ]
        try:
            batch = self.client.messages.batches.create(requests=requests)
        except Exception as fehler:
            raise _fehler(fehler) from fehler
        return str(batch.id)

    def batch_abbrechen(self, batch_id: str) -> None:
        """Bricht einen Batch ab (noch nicht bearbeitete Anfragen werden nicht berechnet)."""
        try:
            self.client.messages.batches.cancel(batch_id)
        except Exception as fehler:
            raise _fehler(fehler) from fehler

    def batch_stand(self, batch_id: str) -> str:
        """``processing_status`` des Batches (``in_progress``, ``canceling`` oder ``ended``)."""
        try:
            return str(self.client.messages.batches.retrieve(batch_id).processing_status)
        except Exception as fehler:
            raise _fehler(fehler) from fehler

    def batch_ergebnisse(self, batch_id: str) -> Iterator[BatchErgebnis]:
        """Ergebnisse eines beendeten Batches in beliebiger Reihenfolge (zuordnen nur über ``custom_id``)."""
        try:
            eintraege = self.client.messages.batches.results(batch_id)
            for eintrag in eintraege:
                yield self._ergebnis(eintrag)
        except KiAnbieterError:
            raise
        except Exception as fehler:
            raise _fehler(fehler) from fehler

    @staticmethod
    def _ergebnis(eintrag: Any) -> BatchErgebnis:
        custom_id = str(getattr(eintrag, "custom_id", "") or "")
        ergebnis = getattr(eintrag, "result", None)
        art = str(getattr(ergebnis, "type", "") or "")
        if art != "succeeded":
            fehler = getattr(ergebnis, "error", None)
            typ = getattr(getattr(fehler, "error", None), "type", None) or getattr(fehler, "type", None) or art
            return BatchErgebnis(custom_id=custom_id, art=art, text="", fehler=str(typ))
        nachricht = getattr(ergebnis, "message", None)
        modell = str(getattr(nachricht, "model", "") or "")
        usage = getattr(nachricht, "usage", None)
        try:
            text, fehler_text = antwort_text(nachricht), ""
        except KiAnbieterError as fehler:
            text, fehler_text = "", str(fehler)
        return BatchErgebnis(custom_id=custom_id, art=art, text=text, fehler=fehler_text, modell=modell, usage=usage)
