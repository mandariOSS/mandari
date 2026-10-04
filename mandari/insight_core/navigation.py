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


#: Bereich → (Name, URL-Name der Übersichtsseite) für die Brotkrumen der Kopfzeile (Stufe 2)
AREA_PAGES: dict[str, tuple[str, str]] = {
    "sitzungen": ("Sitzungen", "meeting_list"),
    "vorgaenge": ("Vorgänge", "paper_list"),
    "beschluesse": ("Beschlüsse", "decision_list"),
    "gremien": ("Gremien", "organization_list"),
    "personen": ("Personen", "person_list"),
    "ratsfragen": ("Ratsfragen", "question_portal"),
    "karte": ("Karte", "map"),
    "ki": ("KI-Assistent", "chat"),
    "suche": ("Suche", "search"),
    "gespeichert": ("Gespeichert", "saved"),
    "benachrichtigungen": ("Benachrichtigungen", "notifications"),
}


def breadcrumb_area(url_name: str | None) -> dict[str, str | bool]:
    """Bereich als zweite Brotkrume („Beispielstadt › Vorgänge › …“); leer auf der Übersicht und außerhalb der Bereiche.

    ``current`` ist wahr, wenn die Seite selbst die Übersicht des Bereichs ist (dann ohne Link, mit aria-current).
    """
    area = nav_area(url_name)
    if area not in AREA_PAGES:
        return {}
    label, list_name = AREA_PAGES[area]
    return {"label": label, "url_name": f"insight_core:insight:{list_name}", "current": url_name == list_name}
