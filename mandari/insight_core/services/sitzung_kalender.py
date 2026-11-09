# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungen im Kalender (Issues #82, #1274): ein Termin, gleich im Kalender-Abo und in der Kalenderdatei je Sitzung.

Die UID ``meeting-<uuid>@mandari.de`` hängt nur an der kanonischen Kennung der Sitzung. Kalenderprogramme erkennen
den Termin daran wieder: Wer die Datei einer Sitzung nach einer Verschiebung erneut öffnet, aktualisiert ihn, statt
einen zweiten anzulegen. ``SEQUENCE`` folgt dem Stand der Sitzung in der Quelle, damit die neuere Fassung gewinnt.

Eine kommende Sitzung ohne Tagesordnung ist „angekündigt“ (``services.ergebnis``); die Beschreibung sagt das mit
demselben Satz wie die Sitzungsseite.
"""

from __future__ import annotations

from typing import Any, Final

from django.db.models import Exists, OuterRef
from django.urls import reverse
from django.utils import timezone

from ..models import OParlAgendaItem
from .feed_eintraege import ANGEKUENDIGT_SATZ

#: Teil der UID nach dem „@“; bleibt für immer gleich (sonst entstehen doppelte Termine)
UID_DOMAIN: Final = "mandari.de"


def tagesordnung_vorhanden() -> Exists:
    """Annotation für Sitzungslisten: Gibt es Tagesordnungspunkte (ohne gelöschte, damit auch ohne zurückgenommene)?"""
    return Exists(OParlAgendaItem.objects.filter(meeting=OuterRef("pk"), deleted=False))


def hat_tagesordnung(meeting: Any) -> bool:
    """Gibt es zur Sitzung Tagesordnungspunkte (wie die Sitzungsseite: ohne gelöschte)?"""
    return OParlAgendaItem.objects.filter(meeting=meeting, deleted=False).exists()


def uid(meeting: Any) -> str:
    """Feste UID des Termins einer Sitzung."""
    return f"meeting-{meeting.id}@{UID_DOMAIN}"


def _sequenz(meeting: Any) -> int:
    """Versionszähler: Minuten seit 1970 bis zur letzten Änderung in der Quelle (bzw. bei uns), sonst 0."""
    stand = getattr(meeting, "oparl_modified", None) or getattr(meeting, "updated_at", None)
    return int(stand.timestamp()) // 60 if stand else 0


def ics_ereignis(meeting: Any, basis: str, *, angekuendigt: bool, mit_sequenz: bool = True) -> dict[str, Any]:
    """
    Angaben eines VEVENT für ``apps.common.ical`` (``build_ics_event``, ``build_ics_feed``).

    ``basis`` ist die Adresse der Website ohne Schrägstrich am Ende; ``angekuendigt`` gilt nur für kommende Sitzungen
    ohne Tagesordnung. Ohne ``mit_sequenz`` bleibt ``SEQUENCE`` 0 (Kalender-Abo wie bisher).
    """
    seite = basis.rstrip("/") + reverse("insight_core:insight:meeting_detail", kwargs={"pk": meeting.id})
    kommend = meeting.start is not None and meeting.start > timezone.now()
    zeilen = []
    if meeting.cancelled:
        zeilen.append("Die Sitzung ist abgesagt.")
    elif angekuendigt and kommend:
        zeilen.append(f"angekündigt: {ANGEKUENDIGT_SATZ}")
    zeilen.append(f"Tagesordnung und Unterlagen: {seite}")
    return {
        "uid": uid(meeting),
        "summary": meeting.get_display_name() + (" (abgesagt)" if meeting.cancelled else ""),
        "start": meeting.start,
        "end": meeting.end if meeting.end and meeting.start and meeting.end > meeting.start else None,
        "location": meeting.location_name or "",
        "description": "\n".join(zeilen),
        "status": "CANCELLED" if meeting.cancelled else "CONFIRMED",
        "url": seite,
        "sequence": _sequenz(meeting) if mit_sequenz else 0,
    }
