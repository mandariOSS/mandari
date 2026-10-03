# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bereiche der Insight-Navigation (Issue #783).

Die Seitenleiste zeigt sieben feste Bereiche (Übersicht, Sitzungen, Vorgänge, Gremien, Personen,
Karte, KI-Assistent) und zwei bedingte (Beschlüsse, Ratsfragen). Jede Seite gehört zu genau einem
Bereich; der aktive Bereich wird hervorgehoben und trägt ``aria-current="page"``. Seiten ohne
eigenen Eintrag (Dokumente, Nachbarschaft) zählen zum Bereich, in dem sie fachlich aufgehen.
"""

from __future__ import annotations

#: URL-Name (Namespace ``insight_core:insight``) → Bereich der Navigation
NAV_AREAS: dict[str, str] = {
    "portal_home": "uebersicht",
    "portal_entry": "uebersicht",
    "portal_entry_path": "uebersicht",
    "meeting_list": "sitzungen",
    "meeting_detail": "sitzungen",
    "meeting_calendar": "sitzungen",
    "meeting_year_plan": "sitzungen",
    "paper_list": "vorgaenge",
    "paper_detail": "vorgaenge",
    "decision_list": "beschluesse",
    "decision_detail": "beschluesse",
    "organization_list": "gremien",
    "organization_detail": "gremien",
    "person_list": "personen",
    "person_detail": "personen",
    "question_portal": "ratsfragen",
    "question_detail": "ratsfragen",
    "question_start": "ratsfragen",
    "question_submitted": "ratsfragen",
    "ask_question": "ratsfragen",
    "map": "karte",
    "neighborhood": "karte",
    "chat": "ki",
    "search": "suche",
    "file_list": "suche",
    "saved": "gespeichert",
    "notifications": "benachrichtigungen",
}


def nav_area(url_name: str | None) -> str:
    """Bereich der Navigation für einen URL-Namen; leer, wenn die Seite keinem Bereich angehört."""
    return NAV_AREAS.get(url_name or "", "")
