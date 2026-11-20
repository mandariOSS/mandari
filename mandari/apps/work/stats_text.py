# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kennzahlen der Work-Übersichten als Satz statt als Zählerkacheln (Issue #851, Stufe 0).

Jede Funktion bekommt die Zahlen, die die Seite vorher als Kacheln zeigte, und gibt alle wieder: Was
0 ist, entfällt im Satz (bis auf die Gesamtzahl), damit nichts verloren geht, ohne dass Nullen den
Satz füllen.
"""

from __future__ import annotations

from collections.abc import Mapping

from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

from apps.common.formatting import count_label, format_count, join_parts

#: Teile des Satzes über der Dokumentliste und die Status, die sie zählen (Schlüssel wie in ``stats``)
DOKUMENT_STATUS: dict[str, tuple[str, ...]] = {
    "draft": ("draft",),
    "submitted": ("submitted",),
    "in_consultation": ("at_admin", "on_agenda"),
    "completed": ("completed", "adopted"),
}


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


def documents_sentence_links(stats: Mapping[str, int], url: str) -> SafeString:
    """
    Derselbe Satz wie :func:`documents_sentence`, jede Zahl als Link auf die gefilterte Liste (Issue #1492):
    „54 Dokumente“ zeigt alle, „11 Entwürfe“ nur die Entwürfe. ``url`` ist die Adresse der Liste ohne Parameter.
    """

    def link(text: str, schluessel: str = "") -> SafeString:
        query = "&".join(f"status={status}" for status in DOKUMENT_STATUS.get(schluessel, ()))
        return format_html(
            '<a href="{}" class="underline decoration-gray-400 underline-offset-4 hover:text-gray-900 '
            'dark:hover:text-white">{}</a>',
            f"{url}?{query}" if query else url,
            text,
        )

    teile = [
        link(count_label(stats.get("draft", 0), "Entwurf", "Entwürfe"), "draft") if stats.get("draft") else "",
        link(count_label(stats.get("submitted", 0), "eingereicht"), "submitted") if stats.get("submitted") else "",
        link(count_label(stats.get("in_consultation", 0), "in Beratung"), "in_consultation")
        if stats.get("in_consultation")
        else "",
        link(count_label(stats.get("completed", 0), "erledigt"), "completed") if stats.get("completed") else "",
    ]
    vorhanden = [teil for teil in teile if teil]
    satz = link(count_label(stats.get("total", 0), "Dokument", "Dokumente"))
    if vorhanden:
        # Wie join_parts: Komma zwischen den Teilen, „und“ vor dem letzten
        aufzaehlung = (
            vorhanden[0]
            if len(vorhanden) == 1
            else format_html("{} und {}", mark_safe(", ".join(vorhanden[:-1])), vorhanden[-1])  # noqa: S308
        )
        satz = format_html("{}: {}.", satz, aufzaehlung)
    else:
        satz = format_html("{}.", satz)
    if stats.get("overdue"):
        satz = format_html("{} Überfällig: {}.", satz, format_count(stats["overdue"]))
    return satz


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
