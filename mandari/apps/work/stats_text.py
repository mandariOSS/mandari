# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kennzahlen der Work-Übersichten als Satz statt als Zählerkacheln (Issue #851, Stufe 0).

Jede Funktion bekommt die Zahlen, die die Seite vorher als Kacheln zeigte, und gibt alle wieder: Was
0 ist, entfällt im Satz (bis auf die Gesamtzahl), damit nichts verloren geht, ohne dass Nullen den
Satz füllen.
"""

from __future__ import annotations

from collections.abc import Mapping

from apps.common.formatting import count_label, format_count, join_parts


def documents_sentence(stats: Mapping[str, int]) -> str:
    """„6 Dokumente: 3 Entwürfe, 2 eingereicht und 1 erledigt. Überfällig: 1.“"""
    parts = [
        count_label(stats.get("draft", 0), "Entwurf", "Entwürfe") if stats.get("draft") else "",
        count_label(stats.get("submitted", 0), "eingereicht") if stats.get("submitted") else "",
        count_label(stats.get("in_consultation", 0), "in Beratung") if stats.get("in_consultation") else "",
        count_label(stats.get("completed", 0), "erledigt") if stats.get("completed") else "",
    ]
    sentence = count_label(stats.get("total", 0), "Dokument", "Dokumente")
    listed = join_parts(parts)
    sentence += f": {listed}." if listed else "."
    if stats.get("overdue"):
        sentence += f" Überfällig: {format_count(stats['overdue'])}."
    return sentence


def faction_meetings_sentence(stats: Mapping[str, int]) -> str:
    """„3 Sitzungen, davon 1 anstehend. Offene Protokolle: 1.“"""
    sentence = count_label(stats.get("total", 0), "Sitzung", "Sitzungen")
    if stats.get("upcoming"):
        sentence += f", davon {count_label(stats['upcoming'], 'anstehend')}"
    sentence += "."
    if stats.get("pending_protocol"):
        sentence += f" Offene Protokolle: {format_count(stats['pending_protocol'])}."
    return sentence


def ris_overview_sentence(stats: Mapping[str, int]) -> str:
    """„1.234 Vorgänge (56 in diesem Jahr), 345 Sitzungen (12 kommend), 40 Gremien (23 aktiv) und 120 Personen.“"""

    def with_detail(total: int, singular: str, plural: str, detail: int, detail_label: str) -> str:
        text = count_label(total, singular, plural)
        return f"{text} ({count_label(detail, detail_label)})" if detail else text

    parts = [
        with_detail(
            stats.get("papers_total", 0), "Vorgang", "Vorgänge", stats.get("papers_this_year", 0), "in diesem Jahr"
        ),
        with_detail(
            stats.get("meetings_total", 0), "Sitzung", "Sitzungen", stats.get("meetings_upcoming", 0), "kommend"
        ),
        with_detail(
            stats.get("organizations_total", 0), "Gremium", "Gremien", stats.get("organizations_active", 0), "aktiv"
        ),
        count_label(stats.get("persons_total", 0), "Person", "Personen"),
    ]
    return join_parts(parts) + "."
