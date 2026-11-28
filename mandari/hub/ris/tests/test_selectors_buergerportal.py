# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lese-Fassade für das Bürgerportal (Issue #1593): Kommunenauswahl, Listen, Feeds."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from django.db.models import Q

from hub.ris import selectors as ris
from insight_ai.tests.musterstadt import Musterstadt, baue_musterstadt, kennung, zeit
from insight_core.models import (
    REASON_WITHDRAWN,
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMembership,
    OParlPaper,
    OParlPerson,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def stadt() -> Musterstadt:
    return baue_musterstadt()


def test_kommunen_in_jedem_zustand_und_gelistete(stadt: Musterstadt) -> None:
    OParlBody.objects.filter(pk=stadt.body.pk).update(slug="musterstadt")
    OParlBody.objects.filter(pk=stadt.fremd.pk).update(is_listed=False, slug="nachbarort")

    assert ris.body_by_id(stadt.fremd.pk) == stadt.fremd
    assert ris.body_by_id(str(stadt.body.pk)) == stadt.body
    assert ris.body_by_id("keine-kennung") is None
    assert ris.listed_body_by_id(stadt.body.pk) == stadt.body
    assert ris.listed_body_by_id(stadt.fremd.pk) is None
    assert ris.listed_body_by_id(None) is None
    assert ris.listed_body_by_slug("musterstadt") == stadt.body
    assert ris.listed_body_by_slug("nachbarort") is None
    assert ris.listed_body_by_slug("") is None
    # Kennung zu Kurzname oder Kennung, auch für nicht gelistete (Sitemap einer zurückgenommenen Kommune)
    assert ris.body_id_by_reference("nachbarort") == stadt.fremd.pk
    assert ris.body_id_by_reference(str(stadt.body.pk)) == stadt.body.pk
    assert ris.body_id_by_reference("unbekannt") is None
    assert ris.body_id_by_reference(" ") is None
    assert set(ris.bodies_of_sources([stadt.body.source_id])) == {stadt.body, stadt.fremd}
    assert list(ris.bodies_of_sources([stadt.body.source_id]).listed()) == [stadt.body]


def test_sitzungen_der_gremien_ohne_geloeschte(stadt: Musterstadt) -> None:
    ab = zeit(1, 0)
    assert list(ris.public_meetings_of_organizations([stadt.rat], starts_from=ab)) == [
        stadt.rat_sitzung,
        stadt.spaetere_sitzung,
    ]
    assert list(ris.public_meetings_of_organizations([stadt.rat.pk], starts_from=ab, starts_until=zeit(10, 0))) == [
        stadt.rat_sitzung
    ]
    ausschuss = ris.public_meetings_of_organizations([stadt.ausschuss], starts_from=ab, include_cancelled=False)
    assert list(ausschuss) == [stadt.ausschuss_sitzung]
    vorher = ris.public_meetings_of_organizations([stadt.ausschuss], starts_before=ab)
    assert list(vorher) == [stadt.vergangene_sitzung]
    # Mehrere Gremien: jede Sitzung einmal
    beide = ris.public_meetings_of_organizations([stadt.rat, stadt.ausschuss], starts_from=zeit(7, 0))
    assert list(beide) == [
        stadt.rat_sitzung,
        stadt.ausschuss_sitzung,
        stadt.abgesagte_sitzung,
        stadt.spaetere_sitzung,
    ]
    assert list(ris.public_meetings_of_organizations([])) == []


def test_sitzungen_der_kommunen_und_einzelne_sitzung(stadt: Musterstadt) -> None:
    alle = set(ris.public_meetings([stadt.body]))
    assert stadt.geloeschte_sitzung not in alle
    assert {stadt.abgesagte_sitzung, stadt.vergangene_sitzung} <= alle
    assert stadt.fremde_sitzung not in alle
    kommende = list(ris.public_upcoming_meetings([stadt.body], since=zeit(7, 0)))
    assert kommende == [stadt.rat_sitzung, stadt.ausschuss_sitzung, stadt.spaetere_sitzung]

    sitzung = ris.meeting_with_organizations(stadt.geloeschte_sitzung.pk)
    assert sitzung == stadt.geloeschte_sitzung
    assert [g.pk for g in sitzung.organizations.all()] == [stadt.rat.pk]
    assert ris.meeting_with_organizations(uuid.uuid4()) is None


def test_tagesordnung_als_ausdruck_je_sitzung(stadt: Musterstadt) -> None:
    OParlAgendaItem.objects.create(
        external_id=kennung("agendaitems"), meeting=stadt.ausschuss_sitzung, number="1", deleted=True
    )
    zeilen = {
        sitzung.pk: sitzung
        for sitzung in ris.public_meetings([stadt.body]).annotate(
            anzahl=ris.public_agenda_item_count(),
            seit=ris.first_public_agenda_item_created(),
            tagesordnung=ris.has_agenda(),
        )
    }
    # Der nichtöffentliche Punkt zählt nicht, der gelöschte auch nicht
    assert zeilen[stadt.rat_sitzung.pk].anzahl == 2
    assert zeilen[stadt.rat_sitzung.pk].seit == min(stadt.top_eroeffnung.created_at, stadt.top_radweg.created_at)
    assert zeilen[stadt.rat_sitzung.pk].tagesordnung is True
    assert zeilen[stadt.ausschuss_sitzung.pk].anzahl == 0
    assert zeilen[stadt.ausschuss_sitzung.pk].seit is None
    assert zeilen[stadt.ausschuss_sitzung.pk].tagesordnung is False
    assert ris.meeting_has_agenda(stadt.rat_sitzung) is True
    assert ris.meeting_has_agenda(stadt.ausschuss_sitzung) is False


def test_juengste_ergebnisse_mit_gremium_und_vorgang(stadt: Musterstadt) -> None:
    top_ausschuss = OParlAgendaItem.objects.get(meeting=stadt.vergangene_sitzung)
    punkte: list[Any] = list(ris.recent_public_results([stadt.body], since=zeit(1, 0, monat=9), until=zeit(10, 0)))
    # Der nichtöffentliche Punkt mit Ergebnis bleibt außen vor, Punkte ohne Ergebnis ebenso
    assert punkte == [top_ausschuss]
    assert punkte[0].gremium == stadt.ausschuss.name
    ohne = ris.recent_public_results(
        [stadt.body], since=zeit(1, 0, monat=9), until=zeit(10, 0), exclude=Q(result__icontains="empfohlen")
    )
    assert list(ohne) == []
    assert list(ris.recent_public_results([stadt.body], since=zeit(1, 0), until=zeit(10, 0))) == []

    assert ris.first_public_paper_of_agenda_items([top_ausschuss.external_id, ""]) == {
        top_ausschuss.external_id: stadt.vorlage
    }
    assert ris.first_public_paper_of_agenda_items([]) == {}
    OParlPaper.objects.filter(pk=stadt.vorlage.pk).update(deleted=True)
    assert ris.first_public_paper_of_agenda_items([top_ausschuss.external_id]) == {}


def test_kuerzlich_beratene_vorlagen_ohne_zurueckgenommene_beratungen(stadt: Musterstadt) -> None:
    september = (zeit(1, 0, monat=9), zeit(30, 0, monat=9))
    assert set(OParlPaper.objects.filter(pk__in=ris.paper_ids_consulted_between([stadt.body], *september))) == {
        stadt.vorlage
    }
    assert list(ris.paper_ids_consulted_between([stadt.fremd], *september)) == []
    OParlConsultation.objects.filter(meeting_external_id=stadt.vergangene_sitzung.external_id).update(
        deleted=True, deletion_reason=REASON_WITHDRAWN
    )
    assert list(ris.paper_ids_consulted_between([stadt.body], *september)) == []


def test_vorlagen_der_listen_und_feeds(stadt: Musterstadt) -> None:
    geloescht = OParlPaper.objects.create(
        external_id=kennung("papers"), body=stadt.body, name="Alt", paper_type="Anfrage", deleted=True
    )
    assert list(ris.recent_public_papers([stadt.body], limit=1)) == [stadt.grundstueck]
    assert list(ris.public_paper_types([stadt.body])) == ["Beschlussvorlage"]

    gefunden = ris.paper_by_id(geloescht.pk)
    assert gefunden == geloescht
    assert gefunden.body == stadt.body
    assert ris.paper_by_id("x") is None
    # In der Quelle gelöscht zählt mit, von Session zurückgenommen nicht
    assert ris.not_withdrawn_paper(geloescht.pk) == geloescht
    OParlPaper.objects.filter(pk=geloescht.pk).update(deletion_reason=REASON_WITHDRAWN)
    assert ris.not_withdrawn_paper(geloescht.pk) is None
    assert ris.not_withdrawn_paper(None) is None


def test_gremien_der_listen_und_feeds(stadt: Musterstadt) -> None:
    stichtag = date(2026, 10, 6)
    aktive = [str(o.name) for o in ris.public_organizations([stadt.body], on=stichtag)]
    alle = [str(o.name) for o in ris.public_organizations([stadt.body], on=stichtag, include_ended=True)]
    assert "Sonderausschuss Stadtfest" not in aktive
    assert sorted(aktive + ["Sonderausschuss Stadtfest"]) == alle
    assert [o.name for o in ris.public_organizations([stadt.body], "sonder", include_ended=True)] == [
        "Sonderausschuss Stadtfest"
    ]

    assert ris.public_organization([stadt.body], stadt.rat.pk) == stadt.rat
    assert ris.public_organization([stadt.fremd], stadt.rat.pk) is None
    assert ris.public_organization([stadt.body], "kein-gremium") is None
    assert ris.organization_by_id(stadt.rat.pk) == stadt.rat
    assert ris.not_withdrawn_organization(stadt.rat.pk) == stadt.rat
    assert list(ris.organizations_by_ids([stadt.rat.pk, stadt.ausschuss.pk, "x"])) == [stadt.ausschuss, stadt.rat]

    gremien: list[Any] = list(ris.public_organizations_with_meetings([stadt.body]))
    mit_sitzungen = {gremium.name: gremium.meeting_count for gremium in gremien}
    # Die gelöschte Sitzung zählt nicht; der Sonderausschuss hat keine
    assert mit_sitzungen == {stadt.ausschuss.name: 3, stadt.rat.name: 2}


def test_ausdruecke_je_gremium(stadt: Musterstadt) -> None:
    jetzt = zeit(8, 0)
    OParlMembership.objects.create(
        external_id=kennung("memberships"), person=stadt.person, organization=stadt.rat, role="Vorsitz", deleted=True
    )
    zeilen = {
        o.pk: o
        for o in ris.public_organizations([stadt.body], include_ended=True).annotate(
            naechste=ris.next_meeting_start(jetzt),
            letzte=ris.last_meeting_start(jetzt),
            hat_sitzungen=ris.has_public_meetings(),
            mitglieder=ris.current_member_count(date(2026, 10, 6)),
            ohne_vorsitz=ris.current_member_count(date(2026, 10, 6), exclude=Q(role="Vorsitz")),
        )
    }
    rat, ausschuss = zeilen[stadt.rat.pk], zeilen[stadt.ausschuss.pk]
    assert rat.naechste == stadt.spaetere_sitzung.start
    assert rat.letzte == stadt.rat_sitzung.start
    assert ausschuss.naechste == stadt.ausschuss_sitzung.start
    assert rat.hat_sitzungen is True
    assert (rat.mitglieder, rat.ohne_vorsitz) == (1, 0)
    # Die Mitgliedschaft im Ausschuss endete 2025
    assert ausschuss.mitglieder == 0
    sonder = next(o for o in zeilen.values() if o.name == "Sonderausschuss Stadtfest")
    assert (sonder.naechste, sonder.hat_sitzungen) == (None, False)


def test_person_nur_mit_laufendem_amt(stadt: Musterstadt) -> None:
    ohne_amt = OParlPerson.objects.create(
        external_id=kennung("persons"), body=stadt.body, name="Max Ohneamt", given_name="Max", family_name="Ohneamt"
    )
    assert ris.public_person([stadt.body], stadt.person.pk) == stadt.person
    assert ris.public_person([stadt.fremd], stadt.person.pk) is None
    assert ris.public_person([stadt.body], ohne_amt.pk) is None
    assert ris.public_person([stadt.body], "x") is None
