# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lesende Abfragen der Startseite im neuen Rahmen (Issue #852).

Start beantwortet drei Fragen ohne Zählerkacheln: Was tagt als Nächstes und wie weit ist die Vorbereitung
(„Positionen: 2 von 9 Vorlagen“), was wartet auf mich („Für Sie“: Freigaben, Aufgaben, eigene Dokumente in
Arbeit) und was ist neu in meinen Gremien (Vorlagen auf den nächsten Tagesordnungen). RIS-Daten nur über die
Lese-Fassade ``hub.ris.selectors``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.urls import reverse
from django.utils import timezone

from hub.ris import selectors as ris

#: Zeitraum, aus dem „Neu in Ihren Gremien“ die Tagesordnungen liest
NEU_ZEITRAUM = timedelta(days=42)
#: Dokumentstände, die noch Arbeit der Autorin bzw. des Autors brauchen
DOKUMENT_IN_ARBEIT = ("draft", "review", "internal_review", "external_review")
WOCHENTAGE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
PRIORITAET = {"urgent": "Priorität dringend", "high": "Priorität hoch"}


@dataclass(frozen=True)
class Stand:
    """Vorbereitungsstand einer Gremiensitzung: Punkte, Punkte mit Vorlage und davon mit Position der Organisation."""

    tops: int
    vorlagen: int
    positionen: int


def kurzdatum(wert: date | datetime | None) -> str:
    """„Mi 14.10.“ – Wochentag und Datum ohne Jahr, wie in Satz und Liste der Startseite."""
    if wert is None:
        return ""
    if isinstance(wert, datetime):
        wert = timezone.localtime(wert).date() if timezone.is_aware(wert) else wert.date()
    return f"{WOCHENTAGE[wert.weekday()]} {wert:%d.%m.}"


def vorbereitungsstand(organization: Any, meeting_ids: list[uuid.UUID]) -> dict[uuid.UUID, Stand]:
    """
    Je Gremiensitzung: Tagesordnungspunkte, Punkte mit Vorlage und wie viele davon eine Position der Organisation
    haben (nicht „Noch offen“). Formalien ohne Vorlage zählen nicht (Konzept Abschnitt 5). Zwei Abfragen.
    """
    from apps.work.meetings.models import AgendaItemPosition

    uebersicht = ris.agenda_overview(meeting_ids)
    alle_mit_vorlage = [item for eintrag in uebersicht.values() for item in eintrag.with_paper]
    mit_position = set(
        AgendaItemPosition.objects.filter(organization=organization, agenda_item_id__in=alle_mit_vorlage)
        .exclude(position="open")
        .values_list("agenda_item_id", flat=True)
    )
    return {
        meeting_id: Stand(
            tops=eintrag.items,
            vorlagen=len(eintrag.with_paper),
            positionen=len(eintrag.with_paper & mit_position),
        )
        for meeting_id, eintrag in uebersicht.items()
    }


def fuer_sie(organization: Any, membership: Any, *, limit: int = 6) -> list[dict[str, Any]]:
    """
    Was auf das Mitglied wartet, wichtigstes zuerst: Freigaben, um die es gebeten ist, ihm zugewiesene offene
    Aufgaben und eigene Dokumente in Arbeit. Nur Dokumente, die es sehen darf (``Motion.visible_to``).
    """
    from apps.work.motions.models import Motion, MotionApproval
    from apps.work.tasks.models import Task

    slug = organization.slug
    eintraege: list[dict[str, Any]] = []
    sichtbar = Motion.visible_to(membership)  # type: ignore[no-untyped-call]  # Modell noch ohne Annotationen

    freigaben = (
        MotionApproval.objects.filter(approver=membership, approved__isnull=True, motion__in=sichtbar)
        .exclude(motion__status__in=("archived", "deleted"))
        .select_related("motion")
        .order_by("-created_at")[:limit]
    )
    for freigabe in freigaben:
        eintraege.append(
            {
                "art": f"Dokument · {freigabe.motion.get_status_display()}",
                "titel": freigabe.motion.title,
                "zeile": f"Wartet auf Ihre Freigabe als {freigabe.get_approval_type_display()}",
                "url": reverse("work:document_editor", kwargs={"org_slug": slug, "motion_id": freigabe.motion_id}),
            }
        )

    aufgaben = Task.objects.filter(
        organization=organization, assigned_to=membership, status__in=("todo", "in_progress")
    ).order_by("due_date", "-created_at")[:limit]
    for aufgabe in sorted(aufgaben, key=lambda a: (a.priority not in PRIORITAET, a.due_date or date.max)):
        teile = ["Aufgabe"]
        if aufgabe.due_date:
            teile.append(f"fällig {kurzdatum(aufgabe.due_date)}")
        if aufgabe.priority in PRIORITAET:
            teile.append(PRIORITAET[aufgabe.priority])
        eintraege.append(
            {
                "art": " · ".join(teile),
                "titel": aufgabe.title,
                "zeile": aufgabe.get_status_display(),
                "url": f"{reverse('work:tasks', kwargs={'org_slug': slug})}?open={aufgabe.id}",
            }
        )

    dokumente = (
        sichtbar.filter(author=membership, status__in=DOKUMENT_IN_ARBEIT)
        .exclude(pk__in=[f.motion_id for f in freigaben])
        .order_by("-updated_at")[:limit]
    )
    for dokument in dokumente:
        eintraege.append(
            {
                "art": "Ihr Dokument",
                "titel": dokument.title,
                "zeile": f"{dokument.get_status_display()}, zuletzt geändert {kurzdatum(dokument.updated_at)}",
                "url": reverse("work:document_editor", kwargs={"org_slug": slug, "motion_id": dokument.id}),
            }
        )
    return eintraege[:limit]


def neu_in_gremien(organization: Any, committee_ids: list[uuid.UUID] | None, *, limit: int = 5) -> list[dict[str, Any]]:
    """
    Vorlagen auf den Tagesordnungen der nächsten sechs Wochen, neueste Vorlage zuerst: mit „Meinen Gremien“ nur
    deren Sitzungen, sonst alle Sitzungen der Kommunen der Organisation.
    """
    jetzt = timezone.now()
    beginn = jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
    if committee_ids:
        sitzungen = ris.meetings_of_organizations(
            committee_ids, starts_from=beginn, starts_until=jetzt + NEU_ZEITRAUM, include_cancelled=False
        )
    else:
        bodies = organization.get_all_bodies()
        sitzungen = ris.upcoming_meetings(bodies, since=beginn).filter(start__lte=jetzt + NEU_ZEITRAUM)
    slug = organization.slug
    ergebnis = []
    for eintrag in ris.papers_on_agendas(sitzungen.prefetch_related("organizations"), limit=limit):
        papier, sitzung = eintrag.paper, eintrag.meeting
        gremien = [str(o.short_name or o.name) for o in sitzung.organizations.all()]
        datum = f"{papier.date:%d.%m.%Y}" if papier.date else ""
        kopf = " · ".join(str(t) for t in (papier.paper_type, papier.reference, datum) if t)
        ergebnis.append(
            {
                "art": kopf,
                "titel": papier.name or papier.reference or "Vorlage",
                "zeile": f"{', '.join(gremien) or sitzung.name or 'Sitzung'} {kurzdatum(sitzung.start)}".strip(),
                "url": reverse("work:ris_paper_detail", kwargs={"org_slug": slug, "paper_id": papier.id}),
            }
        )
    return ergebnis


def stand_fraktionssitzung(sitzung: dict[str, Any]) -> str:
    """„Geplant, Einladung noch nicht versandt“ – Stand einer Fraktionssitzung in einem Satz."""
    status = str(sitzung.get("status_display") or "")
    if sitzung.get("status") == "invited":
        return status
    if sitzung.get("invitation_sent"):
        return f"{status}, Einladung versandt"
    if sitzung.get("status") in ("draft", "planned"):
        return f"{status}, Einladung noch nicht versandt"
    return status


def satz_und_hauptaktion(
    organization: Any, sitzungen: list[dict[str, Any]], stand: dict[uuid.UUID, Stand]
) -> tuple[str, dict[str, str] | None]:
    """
    Ein Satz mit dem Stand für das Kopfband und höchstens eine Hauptaktion: die nächste Gremiensitzung mit
    Vorlagen vorbereiten. Ohne Sitzungen ein ruhiger Satz ohne Aktion.
    """
    teile: list[str] = []
    aktion = None
    naechste_ris = next(
        (s for s in sitzungen if s["type"] == "ris" and s["id"] in stand and stand[s["id"]].vorlagen), None
    )
    if naechste_ris is not None:
        s = stand[naechste_ris["id"]]
        wann = kurzdatum(naechste_ris["start"])
        vorlagen = "Vorlage" if s.vorlagen == 1 else "Vorlagen"
        teile.append(f"{naechste_ris['title']} am {wann}: {s.positionen} von {s.vorlagen} {vorlagen} mit Position.")
        aktion = {
            "label": f"Sitzung am {wann} vorbereiten",
            "url": reverse(
                "work:meeting_prepare", kwargs={"org_slug": organization.slug, "meeting_id": naechste_ris["id"]}
            ),
        }
    fraktion = next((s for s in sitzungen if s["type"] == "faction"), None)
    if fraktion is not None:
        teile.append(f"Die nächste Fraktionssitzung ist am {kurzdatum(fraktion['start'])}")
    if not teile:
        teile.append("In den nächsten Wochen stehen keine Sitzungen an.")
    return " ".join(teile), aktion
