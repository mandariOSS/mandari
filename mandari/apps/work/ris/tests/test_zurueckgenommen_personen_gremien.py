# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Von mandari Session zurückgenommene Personen und Gremien in der Recherche von Work (Teil von #1172, wie Issue #853).

Wie Sitzungen und Vorgänge: Was Session aus der Veröffentlichung zurückgenommen hat, erscheint weder in Listen,
Kennzahlen, Mitgliederlisten und Datenbanksuche noch auf der Detailseite; diese antwortet mit 410 und einem Hinweis
ohne Namen und Kontaktdaten. In fremden Ratsinformationssystemen gelöschte Einträge bleiben wie bisher sichtbar,
unbekannte Kennungen ergeben 404.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
from django.http import Http404
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.work.organization import selectors as organisation
from apps.work.ris import selectors
from insight_core.models import (
    OParlBody,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPerson,
    OParlSource,
)

pytestmark = pytest.mark.django_db

SESSION = "https://work.example.org/session/stadt/api/oparl"
FREMD = "https://ris.fremd.example/oparl"

ARTEN = (("sichtbar", False, SESSION), ("zurueck", True, SESSION), ("quelle", True, FREMD))


@pytest.fixture
def kommune(org: Any) -> OParlBody:
    quelle = OParlSource.objects.create(name="Session", url=f"{SESSION}/system")
    body = OParlBody.objects.create(external_id=f"{SESSION}/body/1/", source=quelle, name="Stadt Beispiel")
    org.body = body
    org.save(update_fields=["body"])
    return body


@pytest.fixture
def mitglied(org: Any, make_member: Any, kommune: OParlBody) -> Any:
    return make_member(org, ["ris.view"], email="recherche@example.org")


@pytest.fixture
def daten(kommune: OParlBody) -> dict[str, Any]:
    """Je eine sichtbare, eine zurückgenommene und eine in der Quelle gelöschte Person bzw. ein solches Gremium."""
    werte: dict[str, Any] = {}
    for art, deleted, praefix in ARTEN:
        werte[f"gremium_{art}"] = OParlOrganization.objects.create(
            external_id=f"{praefix}/organization/{art}/", body=kommune, name=f"Ausschuss {art}", deleted=deleted
        )
        werte[f"person_{art}"] = OParlPerson.objects.create(
            external_id=f"{praefix}/person/{art}/",
            body=kommune,
            name=f"Kim {art}",
            family_name=art.capitalize(),
            given_name="Kim",
            email=f"{art}@rat.example",
            deleted=deleted,
        )
    # Jede Person sitzt im sichtbaren Gremium, die sichtbare Person in jedem Gremium
    for art, _deleted, praefix in ARTEN:
        OParlMembership.objects.create(
            external_id=f"{praefix}/membership/person-{art}/",
            person=werte[f"person_{art}"],
            organization=werte["gremium_sichtbar"],
        )
        if art != "sichtbar":
            OParlMembership.objects.create(
                external_id=f"{praefix}/membership/gremium-{art}/",
                person=werte["person_sichtbar"],
                organization=werte[f"gremium_{art}"],
            )
    return werte


def _namen(objekte: Any) -> set[str]:
    return {objekt.name for objekt in objekte}


#: Eine in der Quelle gelöschte Person zeigt die Recherche nicht mehr (Issue #1560); das Gremium bleibt
PERSONEN = {"Kim sichtbar"}
GREMIEN = {"Ausschuss sichtbar", "Ausschuss quelle"}


def test_listen_kennzahlen_und_suche_ohne_zurueckgenommene(org: Any, daten: dict[str, Any]) -> None:
    bodies = selectors.bodies_for_organization(org)

    assert _namen(selectors.persons_queryset(bodies)) == PERSONEN
    assert _namen(selectors.persons_queryset(bodies, search="zurueck@rat.example")) == set()
    # Die Personensuche durchsucht keine E-Mail-Adressen mehr (Issue #1560)
    assert _namen(selectors.persons_queryset(bodies, search="sichtbar@rat.example")) == set()
    assert _namen(selectors.persons_queryset(bodies, search="Kim")) == PERSONEN
    alle = selectors.filter_organizations(selectors.organizations_with_meeting_info(bodies), active_only=False)
    assert _namen(alle) == GREMIEN
    assert _namen(selectors.organizations_for_filter(bodies)) == GREMIEN
    assert set(selectors.search_committees(bodies)) == GREMIEN
    assert selectors.organization_tab_counts(bodies)["all_count"] == 2
    stats = selectors.overview_stats(bodies)
    assert stats["persons_total"] == 1 and stats["organizations_total"] == 2
    funde = selectors.orm_search([str(org.body_id)], query="Kim")
    assert _namen(funde["persons"]) == PERSONEN
    funde = selectors.orm_search([str(org.body_id)], query="Ausschuss")
    assert _namen(funde["organizations"]) == GREMIEN


def test_mitgliederlisten_ohne_zurueckgenommene_gegenseite(daten: dict[str, Any]) -> None:
    mitglieder = selectors.organization_members(daten["gremium_sichtbar"])["active"]
    assert {m.person.name for m in mitglieder} == PERSONEN

    gremien = selectors.person_memberships(daten["person_sichtbar"])["active"]
    assert {m.organization.name for m in gremien} == GREMIEN


def test_zurueckgenommene_mitgliedschaft_fehlt_auf_beiden_seiten(daten: dict[str, Any]) -> None:
    OParlMembership.objects.filter(person=daten["person_quelle"]).update(
        deleted=True, external_id=f"{SESSION}/membership/einzeln/"
    )

    mitglieder = selectors.organization_members(daten["gremium_sichtbar"])["active"]
    assert {m.person.name for m in mitglieder} == {"Kim sichtbar"}
    assert list(selectors.person_memberships(daten["person_quelle"])["active"]) == []


def test_personenliste_ratsrolle_und_zaehler_ohne_zurueckgenommene_mitgliedschaft(
    org: Any, kommune: OParlBody, daten: dict[str, Any]
) -> None:
    """Die Liste nennt nur, was die Detailseite der Person auch zeigt."""
    rat = OParlOrganization.objects.create(external_id=f"{SESSION}/organization/rat/", body=kommune, name="Rat")
    for art, deleted, rolle in (("sichtbar", True, "Ratsmitglied"), ("quelle", False, "Vorsitz")):
        OParlMembership.objects.create(
            external_id=f"{SESSION}/membership/rat-{art}/",
            person=daten[f"person_{art}"],
            organization=rat,
            role=rolle,
            deleted=deleted,
        )
    bodies = selectors.bodies_for_organization(org)

    # Die Annotationen der Abfrage kennt die Typprüfung nicht
    personen = {person.name: cast(Any, person) for person in selectors.persons_queryset(bodies)}

    # Kim sichtbar: Die Ratsmitgliedschaft ist zurückgenommen; es bleiben das sichtbare und das in der Quelle
    # gelöschte Gremium, nicht das zurückgenommene
    assert personen["Kim sichtbar"].council_role is None
    assert personen["Kim sichtbar"].membership_count == 2
    assert personen["Kim sichtbar"].membership_count == sum(
        gruppe.count() for gruppe in selectors.person_memberships(daten["person_sichtbar"]).values()
    )
    # Die in der Quelle gelöschte Person steht nicht mehr in der Liste (Issue #1560)
    assert "Kim quelle" not in personen


def test_gremienliste_nennt_keine_zurueckgenommene_sitzung(org: Any, kommune: OParlBody, daten: dict[str, Any]) -> None:
    """Nächster Termin und „hat Sitzungen“ zählen nur, was noch veröffentlicht ist."""
    morgen = timezone.now() + timedelta(days=1)
    sitzung = OParlMeeting.objects.create(
        external_id=f"{SESSION}/meeting/zurueck/", body=kommune, name="Sitzung zurueck", start=morgen, deleted=True
    )
    sitzung.organizations.add(daten["gremium_sichtbar"])
    bodies = selectors.bodies_for_organization(org)

    # Die Annotationen der Abfrage kennt die Typprüfung nicht
    gremium = cast(Any, selectors.organizations_with_meeting_info(bodies).get(pk=daten["gremium_sichtbar"].pk))

    assert gremium.next_meeting is None and gremium.has_meetings is False
    assert selectors.organization_tab_counts(bodies)["active_count"] == 0


def test_vorschlag_zur_verknuepfung_ohne_zurueckgenommene_person(org: Any, daten: dict[str, Any]) -> None:
    bodies = selectors.bodies_for_organization(org)

    def vorschlaege(nachname: str) -> set[str]:
        konto = cast(Any, UserFactory).build(first_name="Kim", last_name=nachname)
        return _namen(organisation.find_matching_persons(konto, bodies))

    assert vorschlaege("Sichtbar") == {"Kim sichtbar"}
    assert vorschlaege("Zurueck") == set()


def test_verknuepfen_ueber_die_kennung_findet_keine_zurueckgenommene_person(org: Any, daten: dict[str, Any]) -> None:
    """Auch wer die Kennung kennt, verknüpft eine Mitgliedschaft nicht mit einer zurückgenommenen Person."""
    bodies = selectors.bodies_for_organization(org)

    with pytest.raises(Http404):
        organisation.get_oparl_person_or_404(bodies, daten["person_zurueck"].pk)

    for art in ("sichtbar", "quelle"):
        assert organisation.get_oparl_person_or_404(bodies, daten[f"person_{art}"].pk) == daten[f"person_{art}"]


def test_gremienauswahl_und_vorschlaege_am_mitglied_ohne_zurueckgenommene(org: Any, daten: dict[str, Any]) -> None:
    """Auswahllisten und Vorschläge der Mitgliederverwaltung nennen weder zurückgenommene Gremien noch Mitgliedschaften."""
    bodies = selectors.bodies_for_organization(org)
    heute = timezone.now().date()

    assert _namen(organisation.active_committees(bodies, heute)) == GREMIEN
    OParlOrganization.objects.all().update(end_date=heute - timedelta(days=1))
    assert _namen(organisation.inactive_committees(bodies, heute)) == GREMIEN
    OParlOrganization.objects.all().update(end_date=None)

    # Die sichtbare Person sitzt in jedem Gremium: vorgeschlagen werden nur die nicht zurückgenommenen
    assert _namen(organisation.suggested_committees(daten["person_sichtbar"], bodies, heute)) == GREMIEN
    OParlMembership.objects.filter(person=daten["person_sichtbar"], organization=daten["gremium_quelle"]).update(
        deleted=True, external_id=f"{SESSION}/membership/einzeln/"
    )
    assert _namen(organisation.suggested_committees(daten["person_sichtbar"], bodies, heute)) == {"Ausschuss sichtbar"}


@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
@pytest.mark.parametrize(
    ("art", "adresse", "kennung"),
    [("person", "work:ris_person_detail", "person_id"), ("gremium", "work:ris_organization_detail", "org_id")],
)
def test_detailseite_zurueckgenommen_410_ohne_inhalt(
    org: Any, mitglied: Any, client_for: Any, daten: dict[str, Any], neu: bool, art: str, adresse: str, kennung: str
) -> None:
    org.work_new_design = neu
    org.save(update_fields=["work_new_design"])
    client = client_for(mitglied.user)

    def aufruf(objekt_id: Any) -> Any:
        return client.get(reverse(adresse, kwargs={"org_slug": org.slug, kennung: objekt_id}))

    antwort = aufruf(daten[f"{art}_zurueck"].pk)
    assert antwort.status_code == 410
    html = antwort.content.decode()
    assert 'data-testid="zurueckgenommen"' in html and "Nicht mehr öffentlich" in html
    assert daten[f"{art}_zurueck"].name not in html and "zurueck@rat.example" not in html
    assert antwort["Cache-Control"] == "no-store" and antwort["X-Robots-Tag"] == "noindex"
    # Sichtbar bleiben der veröffentlichte und der in der Quelle gelöschte Eintrag; Unbekanntes ergibt 404
    sichtbar = aufruf(daten[f"{art}_sichtbar"].pk)
    assert sichtbar.status_code == 200
    # Die Seite nennt weder die zurückgenommene Person noch das zurückgenommene Gremium als Mitgliedschaft
    inhalt = sichtbar.content.decode()
    assert "Kim zurueck" not in inhalt and "Ausschuss zurueck" not in inhalt
    # In der Quelle gelöscht: das Gremium bleibt sichtbar, die Person nicht mehr (Issue #1560)
    assert aufruf(daten[f"{art}_quelle"].pk).status_code == (404 if art == "person" else 200)
    assert aufruf(uuid.uuid4()).status_code == 404


def test_zurueckgenommen_einer_fremden_kommune_bleibt_404(
    org: Any, mitglied: Any, client_for: Any, daten: dict[str, Any]
) -> None:
    """Der Hinweis verrät nicht, dass es den Eintrag in einer anderen Kommune gibt."""
    quelle = OParlSource.objects.create(name="Andere", url="https://work.example.org/session/andere/api/oparl/system")
    andere = OParlBody.objects.create(external_id=f"{SESSION}/body/2/", source=quelle, name="Andere Stadt")
    person = OParlPerson.objects.create(
        external_id=f"{SESSION}/person/andere/", body=andere, name="Fremd", deleted=True
    )
    gremium = OParlOrganization.objects.create(
        external_id=f"{SESSION}/organization/andere/", body=andere, name="Fremd", deleted=True
    )
    client = client_for(mitglied.user)

    for adresse, kennung, objekt in (
        ("work:ris_person_detail", "person_id", person),
        ("work:ris_organization_detail", "org_id", gremium),
    ):
        antwort = client.get(reverse(adresse, kwargs={"org_slug": org.slug, kennung: objekt.pk}))
        assert antwort.status_code == 404


@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
def test_listenseiten_ohne_zurueckgenommene(
    org: Any, mitglied: Any, client_for: Any, daten: dict[str, Any], neu: bool
) -> None:
    org.work_new_design = neu
    org.save(update_fields=["work_new_design"])
    client = client_for(mitglied.user)

    def seite(name: str, **parameter: str) -> str:
        return str(client.get(reverse(name, kwargs={"org_slug": org.slug}), parameter).content.decode())

    personen, gremien = seite("work:ris_persons"), seite("work:ris_organizations", tab="all")

    assert str(daten["person_sichtbar"].pk) in personen and str(daten["person_zurueck"].pk) not in personen
    assert "zurueck@rat.example" not in personen and "Kim zurueck" not in personen
    assert str(daten["gremium_sichtbar"].pk) in gremien and str(daten["gremium_zurueck"].pk) not in gremien
    # Auch die Suche über die Adresse findet die zurückgenommene Person nicht
    assert "Kim zurueck" not in seite("work:ris_persons", q="zurueck@rat.example")
