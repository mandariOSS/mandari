# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI-Assistent in Insight: Antworten aus den strukturierten Ratsdaten (Issue #899).

Ablauf je Frage:

1. Systemprompt mit heutigem Datum, Wochentag und Kommune (``prompts.build_chat_system_prompt``).
2. Bis zu ``INSIGHT_CHAT_MAX_TOOL_ROUNDS`` Runden mit Werkzeugen (``chat_tools``): Das Modell fragt Sitzungen,
   Tagesordnungen, Vorgänge, Gremien, Personen und Dokumentausschnitte der gewählten Kommune ab.
3. Antwort mit Links auf die Insight-Seiten; die verlinkten Einträge werden zu Quellen-Kacheln.

Scheitert eine Werkzeugrunde, antwortet die Schlussrunde mit den bis dahin geholten Ergebnissen; lehnt der Anbieter
Werkzeuge ab, antwortet das Modell ohne sie (``OpenAIKompatiblerProvider.chat_with_tools``).

Anbieter ist der KI-Endpunkt des Bürgerportals aus der zentralen KI-Konfiguration (Issue #950): Die View übergibt
den Endpunkt, für den die Einwilligung gilt; ohne Angabe löst ``get_insight_provider`` ihn auf. Anbieter,
Bürgerportal-Modell und Ausweichmodell stehen im Admin, die Adresse ist gegen ``KI_ERLAUBTE_HOSTS`` geprüft. Ohne
Einrichtung gibt es keine Anfrage. Die Tagesobergrenze aller Antworten zusammen prüft die View
(``insight_core.views.chat``).

Vorher bekam das Modell fünf Suchtreffer mit je bis zu 4.000 Token Volltext. Jetzt gibt es Volltext nur
abschnittsweise auf Anforderung. Der Verbrauch (Eingabe/Ausgabe) wird je Antwort protokolliert.

Einstellungen: ``INSIGHT_CHAT_MAX_TOOL_ROUNDS`` und ``INSIGHT_CHAT_TIME_LIMIT_SECONDS``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.utils import timezone

from insight_ai.providers import OpenAIKompatiblerProvider, get_insight_provider
from insight_ai.providers.base import ToolChatProvider, ToolChatResponse
from insight_ai.services.chat_tools import TOOLS, context_for, run_tool, select_sources
from insight_ai.services.prompts import build_chat_system_prompt

if TYPE_CHECKING:
    from apps.common.ki_anbieter import KiEndpunkt

logger = logging.getLogger(__name__)

# Token budget constants
MAX_HISTORY_TOKENS = 8000
MAX_RESPONSE_TOKENS = 3000
#: Werkzeugaufrufe je Runde; weitere beantwortet der Dienst mit einem Hinweis
MAX_CALLS_PER_ROUND = 5
#: So viel Zeit bleibt mindestens für die abschließende Antwort ohne Werkzeuge (Sekunden)
FINAL_ANSWER_RESERVE_SECONDS = 20.0
#: Kürzestes Zeitlimit je Antwort (Sekunden): genug für mindestens eine Werkzeugrunde und die Antwort
MIN_TIME_LIMIT_SECONDS = 45

# Approximate chars per token for German text
CHARS_PER_TOKEN = 3

NO_ANSWER = "Ich konnte dazu gerade keine Antwort erstellen. Bitte formulieren Sie Ihre Frage etwas anders."


def _estimate_tokens(text: str) -> int:
    """Rough token estimate for German text."""
    return len(text) // CHARS_PER_TOKEN


def _build_history_messages(history: list[dict[str, Any]], max_tokens: int) -> list[dict[str, str]]:
    """
    Verlauf als Nachrichten, älteste zuerst gekürzt (höchstens die letzten drei Wechsel).

    Nur Nachrichten von Nutzer und Assistent mit Text; alles andere aus dem Browser wird verworfen.
    """
    if not history:
        return []
    recent = [m for m in history[-6:] if isinstance(m, dict)]
    total = sum(_estimate_tokens(str(m.get("content", ""))) for m in recent)
    while total > max_tokens and recent:
        removed = recent.pop(0)
        total -= _estimate_tokens(str(removed.get("content", "")))
    return [
        {"role": str(m["role"]), "content": str(m["content"])}
        for m in recent
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m.get("content")
    ]


@dataclass
class Usage:
    """Verbrauch einer Antwort über alle Runden."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    rounds: int = 0
    models: list[str] = field(default_factory=list)

    def add(self, response: ToolChatResponse) -> None:
        self.prompt_tokens += response.input_tokens
        self.completion_tokens += response.output_tokens
        self.rounds += 1
        if response.model not in self.models:
            self.models.append(response.model)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatAbbruchError(ValueError):
    """
    Die Antwort scheiterte, nachdem schon Modellaufrufe gelaufen waren; ``usage`` ist ihr Verbrauch.

    Die View bucht ihn trotzdem (Nutzungsgrenze und Tagesobergrenze), damit gescheiterte Antworten nicht beliebig oft
    auf Kosten des Betreibers wiederholt werden können.
    """

    def __init__(self, meldung: str, usage: Usage) -> None:
        super().__init__(meldung)
        self.usage = usage


def _setting_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(getattr(settings, name, default)))
    except (TypeError, ValueError):
        return default


def process_chat_message(
    message: str,
    history: list[dict[str, Any]],
    body_id: str | None,
    *,
    now: datetime | None = None,
    endpunkt: KiEndpunkt | None = None,
) -> dict[str, Any]:
    """
    Eine Frage beantworten: Werkzeugrunden über die Ratsdaten der Kommune, dann die Antwort.

    Args:
        message: Frage der Nutzerin bzw. des Nutzers (bereits gefiltert)
        history: Verlauf ``[{role, content}, …]`` aus dem Browser
        body_id: Kennung der gewählten Kommune (oder ``None``)
        now: Zeitpunkt der Frage (Standard: jetzt; Tests setzen ihn fest)
        endpunkt: Der Endpunkt, für den die Einwilligung geprüft wurde (View); die Anfrage geht genau dorthin.
            Ohne Angabe wird die KI-Konfiguration neu aufgelöst.

    Returns:
        ``{"response", "sources", "tokens_used", "prompt_tokens", "completion_tokens", "rounds", "tool_calls"}``

    Raises:
        ValueError: Der KI-Anbieter ist nicht konfiguriert oder antwortet nicht.
    """
    provider = OpenAIKompatiblerProvider(endpunkt) if endpunkt is not None else get_insight_provider()
    if not isinstance(provider, ToolChatProvider) or not provider.is_available():
        raise ValueError("KI-Assistent ist nicht eingerichtet.")

    started = time.monotonic()
    now = now or timezone.now()
    max_rounds = _setting_int("INSIGHT_CHAT_MAX_TOOL_ROUNDS", 4, 1)
    time_limit = float(_setting_int("INSIGHT_CHAT_TIME_LIMIT_SECONDS", 90, MIN_TIME_LIMIT_SECONDS))
    deadline = started + time_limit

    ctx = context_for(body_id, now=now) if body_id else None
    tools = TOOLS if ctx is not None else None

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_chat_system_prompt(now, ctx.body_name if ctx else None)}
    ]
    messages.extend(_build_history_messages(history, MAX_HISTORY_TOKENS))
    messages.append({"role": "user", "content": message})

    usage = Usage()
    answer: str | None = None
    for _round in range(max_rounds if tools else 0):
        remaining = deadline - time.monotonic()
        if remaining < FINAL_ANSWER_RESERVE_SECONDS * 1.5:
            break
        try:
            response = provider.chat_with_tools(
                messages,
                tools=tools,
                max_tokens=MAX_RESPONSE_TOKENS,
                temperature=0.3,
                timeout=remaining - FINAL_ANSWER_RESERVE_SECONDS,
            )
        except ValueError:
            # Scheitert eine Werkzeugrunde (Zeitlimit, Fehler des Anbieters), antwortet die Schlussrunde mit dem,
            # was bis dahin vorliegt, statt die Frage ganz abzulehnen
            logger.warning("KI-Assistent: Werkzeugrunde %s gescheitert, weiter mit der Schlussrunde", _round + 1)
            break
        usage.add(response)
        if not response.tool_calls or ctx is None:
            if response.content.strip():
                answer = response.content
            break
        messages.append(response.message)
        for index, call in enumerate(response.tool_calls):
            if index < MAX_CALLS_PER_ROUND:
                result = run_tool(call.name, call.arguments, ctx)
            else:
                result = '{"fehler":"Zu viele Werkzeugaufrufe in einer Runde; bitte gezielter fragen."}'
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    if answer is None:
        # Abschließende Antwort ohne weitere Werkzeuge (Runden oder Zeit erschöpft, ohne Kommune, leere Antwort)
        try:
            response = provider.chat_with_tools(
                messages,
                tools=tools,
                tool_choice="none",
                max_tokens=MAX_RESPONSE_TOKENS,
                temperature=0.3,
                timeout=max(FINAL_ANSWER_RESERVE_SECONDS, deadline - time.monotonic()),
            )
        except ValueError as exc:
            if usage.rounds:
                raise ChatAbbruchError(str(exc), usage) from exc
            raise
        usage.add(response)
        answer = response.content.strip() or NO_ANSWER

    sources = select_sources(ctx, answer) if ctx is not None else []
    tool_calls = list(ctx.calls) if ctx is not None else []
    logger.info(
        "KI-Assistent: Antwort mit %s Eingabe- und %s Ausgabe-Token in %s Runden (Werkzeuge: %s, Modelle: %s, %.1f s)",
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.rounds,
        ",".join(tool_calls) or "-",
        ",".join(usage.models),
        time.monotonic() - started,
        extra={
            "chat_prompt_tokens": usage.prompt_tokens,
            "chat_completion_tokens": usage.completion_tokens,
            "chat_rounds": usage.rounds,
            "chat_tool_calls": len(tool_calls),
        },
    )
    return {
        "response": answer,
        "sources": sources,
        "tokens_used": usage.total_tokens,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "rounds": usage.rounds,
        "tool_calls": tool_calls,
    }
