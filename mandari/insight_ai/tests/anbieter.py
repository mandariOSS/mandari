# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ersatz für den KI-Anbieter in Tests: folgt einem Drehbuch statt die Schnittstelle aufzurufen (Issue #899)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from insight_ai.providers.base import ToolCall, ToolChatResponse

#: Ein Schritt bekommt die Nachrichten der Runde und gibt Werkzeugaufrufe ``[(name, argumente)]`` oder Text zurück;
#: ein fester Text steht für eine Antwort ohne Werkzeuge, eine Ausnahme für eine gescheiterte Runde
Schritt = Callable[[list[dict[str, Any]]], list[tuple[str, dict[str, Any]]] | str] | str | Exception


def zeichen(wert: Any) -> int:
    return len(json.dumps(wert, ensure_ascii=False)) if wert else 0


class SkriptAnbieter:
    """
    Wie ``OpenAICompatibleProvider.chat_with_tools``, aber ohne Netz: Jede Runde führt den nächsten Schritt aus. Token
    zählt er wie die bisherige Schätzung des Chat-Dienstes (drei Zeichen je Token) über alles, was an die
    Schnittstelle ginge – Nachrichten und Beschreibung der Werkzeuge.
    """

    def __init__(self, schritte: list[Schritt], *, verfuegbar: bool = True) -> None:
        self.schritte = list(schritte)
        self.verfuegbar = verfuegbar
        self.aufrufe: list[dict[str, Any]] = []

    def is_available(self) -> bool:
        return self.verfuegbar

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        model: str | None = None,
        max_tokens: int = 1500,
        temperature: float = 0.3,
        timeout: float = 120.0,
    ) -> ToolChatResponse:
        self.aufrufe.append(
            {
                "messages": json.loads(json.dumps(messages)),
                "tools": tools,
                "tool_choice": tool_choice,
                "model": model,
                "timeout": timeout,
            }
        )
        eingabe = (zeichen(messages) + zeichen(tools)) // 3
        schritt: Schritt = self.schritte.pop(0) if self.schritte else "Ende des Drehbuchs."
        if isinstance(schritt, Exception):
            raise schritt
        ergebnis = schritt if isinstance(schritt, str) else schritt(messages)
        if isinstance(ergebnis, str) or tool_choice == "none":
            text = ergebnis if isinstance(ergebnis, str) else "Antwort ohne Werkzeuge."
            ausgabe = max(1, len(text) // 3)
            return ToolChatResponse(
                content=text,
                model=model or "hauptmodell",
                input_tokens=eingabe,
                output_tokens=ausgabe,
                total_tokens=eingabe + ausgabe,
                message={"role": "assistant", "content": text},
            )
        nummer = len(self.aufrufe)
        calls = [
            ToolCall(id=f"call_{nummer}_{i}", name=name, arguments=json.dumps(args, ensure_ascii=False))
            for i, (name, args) in enumerate(ergebnis)
        ]
        ausgabe = max(1, sum(len(c.name) + len(c.arguments) for c in calls) // 3)
        return ToolChatResponse(
            content="",
            model=model or "hauptmodell",
            input_tokens=eingabe,
            output_tokens=ausgabe,
            total_tokens=eingabe + ausgabe,
            tool_calls=calls,
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                    for c in calls
                ],
            },
        )


# --- Bausteine für Drehbücher --------------------------------------------------------------------------------


def heute(messages: list[dict[str, Any]]) -> date:
    """Das Datum, das der Systemprompt nennt (wie ein Modell es lesen würde)."""
    treffer = re.search(r"HEUTE: \w+, (\d{2}\.\d{2}\.\d{4})", str(messages[0]["content"]))
    assert treffer, "Systemprompt nennt kein Datum"
    return datetime.strptime(treffer.group(1), "%d.%m.%Y").date()


def diese_woche(messages: list[dict[str, Any]]) -> tuple[str, str]:
    treffer = re.search(r"Montag, (\d{2}\.\d{2}\.\d{4}), bis Sonntag, (\d{2}\.\d{2}\.\d{4})", messages[0]["content"])
    assert treffer, "Systemprompt nennt die Woche nicht"
    von, bis = (datetime.strptime(wert, "%d.%m.%Y").date().isoformat() for wert in treffer.groups())
    return von, bis


def morgen(messages: list[dict[str, Any]]) -> str:
    return (heute(messages) + timedelta(days=1)).isoformat()


def letztes_ergebnis(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Ergebnis des letzten Werkzeugaufrufs."""
    letzte = next(m for m in reversed(messages) if m.get("role") == "tool")
    ergebnis = json.loads(letzte["content"])
    assert isinstance(ergebnis, dict)
    return ergebnis


def werkzeuge(*aufrufe: tuple[str, dict[str, Any]]) -> Schritt:
    return lambda _messages: list(aufrufe)


def antwort_sitzungen(messages: list[dict[str, Any]]) -> str:
    sitzungen = letztes_ergebnis(messages).get("sitzungen", [])
    if not sitzungen:
        return "Für diesen Zeitraum sind keine Sitzungen eingetragen."
    zeilen = []
    for zeile in sitzungen:
        beginn, titel, _ort, link, *rest = zeile.split(" | ")
        zeilen.append(f"- [{titel} am {beginn}]({link}){' (abgesagt)' if rest else ''}")
    return "Diese Sitzungen sind eingetragen:\n" + "\n".join(zeilen)


def tagesordnung_der_ersten_sitzung(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    return [("sitzung", {"id": letztes_ergebnis(messages)["sitzungen"][0].split(" | ")[3]})]


def antwort_tagesordnung(messages: list[dict[str, Any]]) -> str:
    ergebnis = letztes_ergebnis(messages)
    sitzung = ergebnis["tagesordnungen"][0] if "tagesordnungen" in ergebnis else ergebnis
    zeilen = [
        "- " + re.sub(r"Vorlage (\S+) (/insight/\S+)", r"[\1](\2)", top) for top in sitzung.get("tagesordnung", [])
    ]
    kopf = f"Tagesordnung der Sitzung [{sitzung['titel']} am {sitzung['beginn']}]({sitzung['link']}):"
    return kopf + "\n" + "\n".join(zeilen)


def ersten_vorgang_laden(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    return [("vorgang", {"id": letztes_ergebnis(messages)["vorgaenge"][0]["link"]})]


def antwort_stand(messages: list[dict[str, Any]]) -> str:
    ergebnis = letztes_ergebnis(messages)
    vorgang = ergebnis.get("vorgang", ergebnis)
    return f"Stand der Vorlage [{vorgang['az']} – {vorgang['titel']}]({vorgang['link']}): {vorgang['stand']}"
