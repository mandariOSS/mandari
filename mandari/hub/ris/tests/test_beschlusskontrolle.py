# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlusskontrolle aus dem kanonischen Modell (Issue #538): Auswahl, Filter und Zahlen der Lese-Fassade.

Work und Bürgerportal lesen Beschlüsse und Umsetzungsstand mit ``RIS_DECISION_TRACKING=bestand`` nur noch hierüber,
für jede Quelle mit Abstimmungsergebnissen, nicht nur für Session-Mandanten.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from hub.ris import selectors as ris
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlOrganization, OParlSource

pytestmark = pytest.mark.django_db

BASIS = "https://ris.beispielstadt.example/oparl"
#: Sitzung mit festem Datum, damit das Jahr nie an einer Jahresgrenze liegt
FRUEHER = datetime(2024, 6, 12, 15, 0, tzinfo=UTC)


def _kennung(art: str) -> str:
    return f"{BASIS}/{art}/{uuid.uuid4()}"


def _gremium(body: OParlBody, name: str) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=_kennung("organizations"), body=body, name=name)


def _sitzung(body: OParlBody, start: datetime, gremium: OParlOrganization, **felder: Any) -> OParlMeeting:
    sitzung = OParlMeeting.objects.create(external_id=_kennung("meetings"), body=body, start=start, **felder)
    sitzung.organizations.set([gremium])
    return sitzung


def _punkt(sitzung: OParlMeeting, nummer: str, name: str, **felder: Any) -> OParlAgendaItem:
    return OParlAgendaItem.objects.create(
        external_id=_kennung("agendaitems"), meeting=sitzung, number=nummer, order=int(nummer), name=name, **felder
    )


@dataclass
class Welt:
    body: OParlBody
    fremd: OParlBody
    rat: OParlOrganization
    ausschuss: OParlOrganization
    bezirk: OParlOrganization
    sitzung: OParlMeeting
    radweg: OParlAgendaItem
    schule: OParlAgendaItem
    ohne_stand: OParlAgendaItem
    nichtoeffentlich: OParlAgendaItem
    spielplatz: OParlAgendaItem
    fremder_beschluss: OParlAgendaItem


@pytest.fixture
def welt() -> Welt:
    quelle = OParlSource.objects.create(name="Fremd-RIS", url=f"{BASIS}/system")
    body = OParlBody.objects.create(external_id=_kennung("bodies"), source=quelle, name="Musterstadt")
    fremd = OParlBody.objects.create(external_id=_kennung("bodies"), source=quelle, name="Nachbarstadt")
    rat = _gremium(body, "Rat")
    ausschuss = _gremium(body, "Ausschuss für Verkehr")
    bezirk = _gremium(body, "Bezirksvertretung Mitte")
    heute = timezone.localdate()

    sitzung = _sitzung(body, timezone.now() - timedelta(days=1), rat)
    radweg = _punkt(
        sitzung,
        "1",
        "Radweg Hafenstraße",
        vote_result="approved",
        resolution_number="B/2026/1",
        implementation_status="open",
        implementation_deadline=heute - timedelta(days=1),
        implementation_public_note="Die Ausschreibung läuft.",
    )
    schule = _punkt(
        sitzung,
        "2",
        "Schulhof",
        vote_result="approved",
        implementation_status="done",
        implementation_deadline=heute - timedelta(days=30),
    )
    ohne_stand = _punkt(sitzung, "3", "Ohne veröffentlichten Stand", vote_result="approved")
    _punkt(sitzung, "4", "Abgelehnter Antrag", vote_result="rejected")
    nichtoeffentlich = _punkt(
        sitzung, "5", "Grundstück", vote_result="approved", public=False, implementation_status="open"
    )
    _punkt(sitzung, "6", "In der Quelle gelöscht", vote_result="approved", deleted=True, implementation_status="open")
    _punkt(sitzung, "7", "Ohne Ergebnis")
    _punkt(sitzung, "8", "Zurückgezogen", vote_result="withdrawn")

    frueher = _sitzung(body, FRUEHER, ausschuss)
    spielplatz = _punkt(frueher, "1", "Spielplatz", vote_result="approved", implementation_status="in_progress")
    _punkt(frueher, "2", "Bericht", vote_result="noted")

    _punkt(_sitzung(body, FRUEHER, bezirk), "1", "Abgelehnt im Bezirk", vote_result="rejected")
    geloescht = _sitzung(body, FRUEHER, rat, deleted=True)
    _punkt(geloescht, "1", "Aus gelöschter Sitzung", vote_result="approved", implementation_status="open")

    fremder_beschluss = _punkt(
        _sitzung(fremd, timezone.now(), _gremium(fremd, "Rat")),
        "1",
        "Fremder Beschluss",
        vote_result="approved",
        implementation_status="open",
    )
    return Welt(
        body, fremd, rat, ausschuss, bezirk, sitzung, radweg, schule, ohne_stand, nichtoeffentlich, spielplatz,
        fremder_beschluss,
    )  # fmt: skip


def _namen(items: Any) -> list[str]:
    return [item.name for item in items]


def test_beschluesse_der_kommune_neueste_sitzung_zuerst(welt: Welt) -> None:
    items = ris.decided_agenda_items([welt.body])

    # Öffentlich, entschieden, nicht gelöscht; „zurückgezogen“ und Punkte ohne Ergebnis zählen nicht. Die beiden
    # früheren Sitzungen beginnen gleichzeitig, ihre Folge ist fest, aber fachlich offen.
    namen = _namen(items)
    assert namen[:4] == ["Radweg Hafenstraße", "Schulhof", "Ohne veröffentlichten Stand", "Abgelehnter Antrag"]
    assert sorted(namen[4:]) == ["Abgelehnt im Bezirk", "Bericht", "Spielplatz"]
    assert namen.index("Spielplatz") + 1 == namen.index("Bericht"), "Punkte einer Sitzung in Tagesordnungsfolge"
    assert _namen(ris.decided_agenda_items([welt.body])) == namen, "bei jeder Abfrage dieselbe Folge"
    assert _namen(ris.decided_agenda_items([welt.fremd])) == ["Fremder Beschluss"]


def test_nur_veroeffentlichter_umsetzungsstand_fuer_das_buergerportal(welt: Welt) -> None:
    items = ris.decided_agenda_items([welt.body], published_only=True)

    assert _namen(items) == ["Radweg Hafenstraße", "Schulhof", "Spielplatz"]


def test_filter_nach_text_gremium_und_jahr(welt: Welt) -> None:
    items = ris.decided_agenda_items([welt.body])

    assert _namen(ris.filter_decisions(items, query="ausschreibung")) == ["Radweg Hafenstraße"]
    assert _namen(ris.filter_decisions(items, query="B/2026")) == ["Radweg Hafenstraße"]
    assert _namen(ris.filter_decisions(items, organization_id=str(welt.ausschuss.pk))) == ["Spielplatz", "Bericht"]
    assert list(ris.filter_decisions(items, organization_id="keine-uuid")) == []
    assert set(_namen(ris.filter_decisions(items, year="2024"))) == {"Spielplatz", "Bericht", "Abgelehnt im Bezirk"}


@pytest.mark.parametrize("jahr", ["0", "10000", "²", "abc", "-1"])
def test_ungueltiges_jahr_wirkt_wie_keines(welt: Welt, jahr: str) -> None:
    items = ris.decided_agenda_items([welt.body])

    assert _namen(ris.filter_decisions(items, year=jahr)) == _namen(items)


def test_ohne_veroeffentlichten_stand_nie_ueberfaellig(welt: Welt) -> None:
    """Eine Frist ohne Stand (etwa von einem älteren Schreiber) macht einen Beschluss nicht überfällig."""
    heute = timezone.localdate()
    OParlAgendaItem.objects.filter(pk=welt.ohne_stand.pk).update(implementation_deadline=heute - timedelta(days=3))
    items = ris.decided_agenda_items([welt.body])

    assert _namen(ris.filter_implementation(items, overdue_only=True, today=heute)) == ["Radweg Hafenstraße"]
    assert ris.implementation_stats(items, heute)["overdue"] == 1


def test_filter_nach_umsetzungsstand(welt: Welt) -> None:
    items = ris.decided_agenda_items([welt.body])
    heute = timezone.localdate()

    assert _namen(ris.filter_implementation(items, status="done", today=heute)) == ["Schulhof"]
    assert _namen(ris.filter_implementation(items, overdue_only=True, today=heute)) == ["Radweg Hafenstraße"]
    assert _namen(ris.filter_implementation(items, status="unbekannt", today=heute)) == _namen(items)
    offen = ris.filter_implementation(
        ris.decided_agenda_items([welt.body], published_only=True), open_only=True, today=heute
    )
    assert _namen(offen) == ["Radweg Hafenstraße", "Spielplatz"]


def test_zahlen_in_einer_abfrage(welt: Welt) -> None:
    items = ris.decided_agenda_items([welt.body])

    with CaptureQueriesContext(connection) as abfragen:
        zahlen = ris.implementation_stats(items, timezone.localdate())

    assert len(abfragen) == 1

    assert zahlen == {"open": 1, "in_progress": 1, "done": 1, "deferred": 0, "overdue": 1, "total": 7}


def test_gremien_und_jahre_mit_beschluessen(welt: Welt) -> None:
    jahr = timezone.localtime(welt.sitzung.start).year

    assert list(ris.decision_organizations([welt.body])) == [welt.ausschuss, welt.bezirk, welt.rat]
    assert list(ris.decision_organizations([welt.body], published_only=True)) == [welt.ausschuss, welt.rat]
    assert ris.decision_years([welt.body]) == sorted({jahr, 2024}, reverse=True)
    assert ris.decision_years([welt.fremd], published_only=True) == [timezone.localtime().year]


def test_ein_beschluss(welt: Welt) -> None:
    assert ris.decided_agenda_item([welt.body], welt.radweg.pk) == welt.radweg
    assert ris.decided_agenda_item([welt.body], str(welt.ohne_stand.pk)) == welt.ohne_stand
    # Andere Kommune, nichtöffentlich, ungültige Kennung
    assert ris.decided_agenda_item([welt.fremd], welt.radweg.pk) is None
    assert ris.decided_agenda_item(None, welt.nichtoeffentlich.pk) is None
    assert ris.decided_agenda_item(None, "kaputt") is None
    # Bürgerportal: alle Kommunen, nur mit veröffentlichtem Umsetzungsstand
    assert ris.decided_agenda_item(None, welt.fremder_beschluss.pk, published_only=True) == welt.fremder_beschluss
    assert ris.decided_agenda_item(None, welt.ohne_stand.pk, published_only=True) is None


def test_ueberfaellig_am_tagesordnungspunkt(welt: Welt) -> None:
    assert welt.radweg.implementation_overdue is True
    assert welt.schule.implementation_overdue is False
    assert welt.ohne_stand.implementation_overdue is False
