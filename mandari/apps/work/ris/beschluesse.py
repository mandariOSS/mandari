# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlusskontrolle in Work: Auswahl aus der Anfrage und der Weg über den RIS-Bestand (Issue #538).

Woher die Beschlüsse kommen, bestimmt der Schalter ``RIS_DECISION_TRACKING``:

- ``session`` (Standard): aus den Session-Mandanten, die mit den Kommunen verknüpft sind (``selectors``), mit dem
  internen Stand der Verwaltung (Zuständigkeit, Erledigungsvermerk, Umsetzungsstand auch ohne Veröffentlichung).
- ``bestand``: über die Lese-Fassade aus dem RIS-Bestand (``hub.ris.selectors``, kanonisches Modell). Das gilt für jede
  Kommune, deren RIS Abstimmungsergebnisse liefert, auch ohne mandari Session; den Umsetzungsstand gibt es nur, wenn
  der Herausgeber ihn veröffentlicht.

Beide Wege geben den Vorlagen dieselben Angaben je Beschluss (``anzeige_session``, ``anzeige_bestand``): ``gremium``,
``sitzung_id`` (Sitzung in der Recherche), ``herausgeber``, ``vermerk``, ``stand`` und ``zustaendig``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.conf import settings
from django.db.models import QuerySet

from hub.ris import selectors as ris
from insight_core.models import OParlAgendaItem, OParlBody


def aus_bestand() -> bool:
    """Liest die Beschlusskontrolle den RIS-Bestand (``RIS_DECISION_TRACKING=bestand``)?"""
    return getattr(settings, "RIS_DECISION_TRACKING", "session") == "bestand"


@dataclass(frozen=True)
class Auswahl:
    """Filter der Liste aus der Adresse (``organization``, ``year``, ``status``, ``overdue``, ``q``, ``page``)."""

    organization_id: str = ""
    year: str = ""
    status: str = ""
    overdue: bool = False
    query: str = ""
    page: str | None = None

    @classmethod
    def aus_anfrage(cls, params: Mapping[str, str]) -> Auswahl:
        return cls(
            organization_id=params.get("organization", ""),
            year=params.get("year", ""),
            status=params.get("status", ""),
            overdue=params.get("overdue") == "1",
            query=(params.get("q") or "").strip(),
            page=params.get("page"),
        )

    def kontext(self) -> dict[str, Any]:
        """Gewählte Filter für die Vorlagen."""
        return {
            "filter_organization": self.organization_id,
            "filter_year": self.year,
            "filter_status": self.status,
            "filter_overdue": self.overdue,
            "search_query": self.query,
            "has_filter": bool(self.organization_id or self.year or self.status or self.overdue or self.query),
        }


def liste_aus_bestand(
    bodies: QuerySet[OParlBody], auswahl: Auswahl, today: date
) -> tuple[QuerySet[OParlAgendaItem], dict[str, Any]]:
    """Gefilterte Beschlüsse der Kommunen aus dem RIS-Bestand und die übrigen Angaben der Liste (Zahlen, Filter)."""
    alle = ris.decided_agenda_items(bodies)
    items = ris.filter_decisions(alle, organization_id=auswahl.organization_id, year=auswahl.year, query=auswahl.query)
    zahlen = ris.implementation_stats(items, today)
    items = ris.filter_implementation(items, status=auswahl.status, overdue_only=auswahl.overdue, today=today)
    return items, {
        "aus_bestand": True,
        "tracking_stats": zahlen,
        "ohne_beschluesse": not alle.exists(),
        "mehrere_herausgeber": bodies.count() > 1,
        "organizations": ris.decision_organizations(bodies),
        "years": ris.decision_years(bodies),
        "implementation_choices": ris.IMPLEMENTATION_CHOICES,
    }


def beschluss_aus_bestand(bodies: QuerySet[OParlBody], item_id: object) -> dict[str, Any] | None:
    """Ein Beschluss der Kommunen aus dem RIS-Bestand für die Detailseite; ``None``, wenn es ihn dort nicht gibt."""
    item = ris.decided_agenda_item(bodies, item_id)
    if item is None:
        return None
    anzeige_bestand([item])
    return {"aus_bestand": True, "item": item, "mehrere_herausgeber": bodies.count() > 1}


def anzeige_session(items: Iterable[Any]) -> None:
    """Angaben je Beschluss aus einem Session-Beschluss (interner Stand der Verwaltung)."""
    for item in items:
        item.gremium = item.meeting.organization.name if item.meeting.organization_id else ""
        item.sitzung_id = item.meeting.oparl_meeting_id
        item.herausgeber = item.meeting.tenant.name
        item.vermerk = item.implementation_note
        item.stand = item.implementation_updated_at
        item.zustaendig = item.implementation_recipient


def anzeige_bestand(items: Iterable[Any]) -> None:
    """Angaben je Beschluss aus dem RIS-Bestand: veröffentlichte Statusmeldung statt Vermerk, keine Zuständigkeit."""
    for item in items:
        item.gremium = ", ".join(o.name for o in item.meeting.organizations.all() if o.name)
        item.sitzung_id = item.meeting_id
        item.herausgeber = item.meeting.body.get_display_name()
        item.vermerk = item.implementation_public_note or ""
        item.stand = item.implementation_modified
        item.zustaendig = None
