# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kalender in Work (Issue #1489, Schritt 1): alle Termine, die es schon gibt, in einer Ansicht.

Der Kalender unter „Sitzungen“ zeigte nur die Sitzungen der Gremien. Jetzt laufen dort zusammen:

* **Sitzungen der Gremien** aus dem Ratsinformationssystem (wie bisher),
* **Fraktionssitzungen**, soweit das Mitglied sie sehen darf (Sitzungsarten, Issue #1239),
* **Fristen der Dokumente** (sichtbare, nicht abgeschlossene Dokumente mit Frist),
* **Aufgaben** mit Fälligkeit (sichtbare, offene Aufgaben).

Jede Art erscheint nur mit dem Recht, das ihre eigene Seite verlangt, und lässt sich im Kalender aus- und einblenden.
Sichtbarkeit und Schutz nichtöffentlicher Inhalte kommen aus den bestehenden Regeln der Bereiche; der Kalender zeigt
nur Titel, Zeit und den Weg zur Seite. Fristen und Fälligkeiten sind ganztägige Einträge.

Eigene Termine der Organisation und die Terminumfrage folgen in eigenen Schritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from . import selectors
from .serializers import serialize_calendar_event


@dataclass(frozen=True)
class Art:
    """Art eines Termins: Schlüssel (auch in der Adresse und als CSS-Klasse), Beschriftung, nötiges Recht."""

    key: str
    label: str
    recht: str


ARTEN: Final[tuple[Art, ...]] = (
    Art("sitzung", "Sitzungen der Gremien", "meetings.view"),
    Art("fraktion", "Fraktionssitzungen", "faction.view_public"),
    Art("dokument", "Fristen der Dokumente", "motions.view"),
    Art("aufgabe", "Aufgaben", "tasks.view"),
)
#: Höchstzahl je Art und Abruf: Ein Monatsraster zeigt ohnehin nur wenige Einträge je Tag
MAX_JE_ART: Final = 300


def arten_fuer(membership: Any) -> list[Art]:
    """Arten, die das Mitglied im Kalender sehen darf (Gäste: keine)."""
    if membership is None or getattr(membership, "is_guest", False):
        return []
    return [art for art in ARTEN if membership.has_permission(art.recht)]


def gewaehlt(wert: str | None, erlaubt: list[Art]) -> set[str]:
    """Arten aus dem Parameter ``arten`` (kommagetrennt); ohne Parameter alle erlaubten, nie mehr als erlaubt."""
    schluessel = {art.key for art in erlaubt}
    if wert is None:
        return schluessel
    return schluessel & {teil.strip() for teil in wert.split(",")}


def zeitfenster(parameter: Any) -> tuple[datetime, datetime, str | None] | None:
    """
    Beginn, Ende und gewählte Arten aus den Parametern des Abrufs (``start``, ``end``, ``arten``); ``None``, wenn das
    Zeitfenster fehlt, sich nicht lesen lässt oder keine Zeitzone trägt (FullCalendar sendet sie immer mit).
    """
    try:
        start = datetime.fromisoformat(str(parameter.get("start", "")).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(parameter.get("end", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if timezone.is_naive(start) or timezone.is_naive(end):
        return None
    return start, end, parameter.get("arten")


def _eintrag(art: str, kennung: Any, titel: str, url: str, **felder: Any) -> dict[str, Any]:
    return {
        "id": f"{art}-{kennung}",
        "title": titel,
        "url": url,
        "classNames": [f"termin-{art}"],
        "extendedProps": {"art": art, "fullTitle": titel},
        **felder,
    }


def _sitzungen(organization: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    bodies = selectors.organization_bodies(organization)
    if bodies is None:
        return []
    eintraege = []
    for meeting in selectors.meetings_between(bodies, start, end)[:MAX_JE_ART]:
        eintrag = serialize_calendar_event(meeting, organization.slug)
        eintrag["classNames"] = ["termin-sitzung"]
        eintrag["extendedProps"]["art"] = "sitzung"
        eintraege.append(eintrag)
    return eintraege


def _fraktionssitzungen(organization: Any, membership: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    from apps.work.faction.models import FactionMeeting
    from apps.work.faction.sitzungsarten import sichtbar_fuer

    # Auch Sitzungen, die vor dem Fenster begonnen haben und hineinreichen (über Mitternacht, mehrtägig); ohne Ende
    # zählt der Beginn
    im_fenster = Q(start__lte=end) & (Q(end__gte=start) | Q(end__isnull=True, start__gte=start))
    sitzungen = sichtbar_fuer(
        membership, FactionMeeting.objects.filter(im_fenster, organization=organization)
    ).order_by("start")[:MAX_JE_ART]
    return [
        _eintrag(
            "fraktion",
            sitzung.pk,
            f"{sitzung.title} (abgesagt)" if sitzung.status == "cancelled" else sitzung.title,
            reverse("work:faction_detail", kwargs={"org_slug": organization.slug, "meeting_id": sitzung.pk}),
            start=sitzung.start.isoformat(),
            end=sitzung.end.isoformat() if sitzung.end else None,
        )
        for sitzung in sitzungen
    ]


def _dokumentfristen(organization: Any, membership: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    from apps.work.motions.models import Motion

    dokumente = (
        Motion.visible_to(membership)  # type: ignore[no-untyped-call]  # Modell noch ohne Annotationen
        .filter(due_date__gte=start.date(), due_date__lte=end.date())
        .exclude(status__in=Motion.CLOSED_STATUSES)
        .order_by("due_date")
        .distinct()[:MAX_JE_ART]
    )
    return [
        _eintrag(
            "dokument",
            dokument.pk,
            f"Frist: {dokument.title}",
            reverse("work:document_editor", kwargs={"org_slug": organization.slug, "motion_id": dokument.pk}),
            start=dokument.due_date.isoformat(),
            allDay=True,
        )
        for dokument in dokumente
    ]


def _aufgaben(organization: Any, membership: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    from apps.work.tasks.selectors import visible_tasks

    aufgaben = (
        visible_tasks(organization, membership)
        .filter(due_date__gte=start.date(), due_date__lte=end.date(), status__in=("todo", "in_progress"))
        .order_by("due_date")[:MAX_JE_ART]
    )
    board = reverse("work:tasks", kwargs={"org_slug": organization.slug})
    return [
        _eintrag(
            "aufgabe",
            aufgabe.pk,
            f"Aufgabe: {aufgabe.title}",
            f"{board}?open={aufgabe.pk}",
            start=aufgabe.due_date.isoformat(),
            allDay=True,
        )
        for aufgabe in aufgaben
        if aufgabe.due_date is not None
    ]


def termine(
    organization: Any, membership: Any, start: datetime, end: datetime, arten: str | None = None
) -> list[dict[str, Any]]:
    """Einträge für FullCalendar im Zeitfenster: nur Arten, die das Mitglied sehen darf und gewählt hat."""
    wahl = gewaehlt(arten, arten_fuer(membership))
    eintraege: list[dict[str, Any]] = []
    if "sitzung" in wahl:
        eintraege += _sitzungen(organization, start, end)
    if "fraktion" in wahl:
        eintraege += _fraktionssitzungen(organization, membership, start, end)
    if "dokument" in wahl:
        eintraege += _dokumentfristen(organization, membership, start, end)
    if "aufgabe" in wahl:
        eintraege += _aufgaben(organization, membership, start, end)
    return eintraege
