# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Von mandari Session zurückgenommene Sitzungen und Vorgänge in der Recherche von Work (Issue #853).

Wie im Bürgerportal: Was Session aus der Veröffentlichung zurückgenommen hat (``withdrawn_by_publisher``, etwa eine
Ö→NÖ-Umstellung), erscheint weder in Listen, Übersicht, Gremium, Dokumenten und Datenbanksuche noch auf der
Detailseite; diese antwortet mit 410 und einem Hinweis ohne Titel und Inhalt. In fremden Ratsinformationssystemen
gelöschte Einträge bleiben wie bisher sichtbar, unbekannte Kennungen ergeben 404.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.ris import selectors
from insight_core.models import OParlBody, OParlFile, OParlMeeting, OParlOrganization, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

SESSION = "https://work.example.org/session/stadt/api/oparl"
FREMD = "https://ris.fremd.example/oparl"


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
    """Je eine sichtbare, eine zurückgenommene und eine in der Quelle gelöschte Sitzung bzw. ein solcher Vorgang."""
    morgen = timezone.now() + timedelta(days=1)
    gremium = OParlOrganization.objects.create(external_id=f"{SESSION}/organization/1/", body=kommune, name="Rat")
    werte: dict[str, Any] = {"gremium": gremium}
    for art, deleted, praefix in (("sichtbar", False, SESSION), ("zurueck", True, SESSION), ("quelle", True, FREMD)):
        sitzung = OParlMeeting.objects.create(
            external_id=f"{praefix}/meeting/{art}/", body=kommune, name=f"Sitzung {art}", start=morgen, deleted=deleted
        )
        sitzung.organizations.add(gremium)
        vorgang = OParlPaper.objects.create(
            external_id=f"{praefix}/paper/{art}/", body=kommune, name=f"Vorgang {art}", deleted=deleted
        )
        OParlFile.objects.create(
            external_id=f"{praefix}/file/{art}/", body=kommune, paper=vorgang, name=f"Datei {art}", deleted=deleted
        )
        werte[f"sitzung_{art}"] = sitzung
        werte[f"vorgang_{art}"] = vorgang
    # Sichtbarer Vorgang mit einer einzeln zurückgenommenen Anlage
    OParlFile.objects.create(
        external_id=f"{SESSION}/file/anlage-zurueck/", body=kommune, paper=werte["vorgang_sichtbar"],
        name="Anlage zurück", deleted=True,
    )  # fmt: skip
    return werte


def _namen(objekte: Any) -> set[str]:
    return {objekt.name for objekt in objekte}


def test_listen_ohne_zurueckgenommene(org: Any, daten: dict[str, Any]) -> None:
    bodies = selectors.bodies_for_organization(org)

    sitzungen = {"Sitzung sichtbar", "Sitzung quelle"}
    vorgaenge = {"Vorgang sichtbar", "Vorgang quelle"}
    assert _namen(selectors.meetings_queryset(bodies, view_mode="all")) == sitzungen
    assert _namen(selectors.upcoming_meetings(bodies)) == sitzungen
    assert _namen(selectors.papers_queryset(bodies)) == vorgaenge
    assert _namen(selectors.recent_papers(bodies)) == vorgaenge
    stats = selectors.overview_stats(bodies)
    assert stats["papers_total"] == 2 and stats["meetings_total"] == 2 and stats["meetings_upcoming"] == 2
    assert _namen(selectors.organization_meetings(daten["gremium"])["upcoming"]) == sitzungen
    assert _namen(selectors.files_queryset(bodies)) == {"Datei sichtbar", "Datei quelle"}
    dateien, aus_json = selectors.paper_files(daten["vorgang_sichtbar"])
    assert _namen(dateien) == {"Datei sichtbar"} and not aus_json
    funde = selectors.orm_search([str(org.body_id)], query="sitzung zurueck")
    assert not funde["meetings"]
    funde = selectors.orm_search([str(org.body_id)], query="Vorgang")
    assert _namen(funde["papers"]) == vorgaenge


@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
@pytest.mark.parametrize(
    ("art", "adresse", "kennung"),
    [("sitzung", "work:ris_meeting_detail", "meeting_id"), ("vorgang", "work:ris_paper_detail", "paper_id")],
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
    assert f"{art.capitalize()} zurueck" not in html  # weder Titel noch Inhalt
    assert antwort["Cache-Control"] == "no-store" and antwort["X-Robots-Tag"] == "noindex"
    # Sichtbar bleiben der veröffentlichte und der in der Quelle gelöschte Eintrag; Unbekanntes ergibt 404
    assert aufruf(daten[f"{art}_sichtbar"].pk).status_code == 200
    assert aufruf(daten[f"{art}_quelle"].pk).status_code == 200
    assert aufruf(uuid.uuid4()).status_code == 404


def test_zurueckgenommen_einer_fremden_kommune_bleibt_404(
    org: Any, mitglied: Any, client_for: Any, daten: dict[str, Any]
) -> None:
    """Der Hinweis verrät nicht, dass es den Eintrag in einer anderen Kommune gibt."""
    quelle = OParlSource.objects.create(name="Andere", url="https://work.example.org/session/andere/api/oparl/system")
    andere = OParlBody.objects.create(external_id=f"{SESSION}/body/2/", source=quelle, name="Andere Stadt")
    fremd = OParlPaper.objects.create(external_id=f"{SESSION}/paper/andere/", body=andere, name="Fremd", deleted=True)

    antwort = client_for(mitglied.user).get(
        reverse("work:ris_paper_detail", kwargs={"org_slug": org.slug, "paper_id": fremd.pk})
    )

    assert antwort.status_code == 404


@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
def test_listenseiten_ohne_zurueckgenommene(
    org: Any, mitglied: Any, client_for: Any, daten: dict[str, Any], neu: bool
) -> None:
    """Listen verlinken die Einträge über ihre Kennung (Sitzungen zeigen das Gremium statt eines Namens)."""
    org.work_new_design = neu
    org.save(update_fields=["work_new_design"])
    client = client_for(mitglied.user)

    def seite(name: str) -> str:
        return str(client.get(reverse(name, kwargs={"org_slug": org.slug})).content.decode())

    sitzungen, vorgaenge, uebersicht = seite("work:ris_meetings"), seite("work:ris_papers"), seite("work:ris_overview")

    assert str(daten["sitzung_sichtbar"].pk) in sitzungen and str(daten["sitzung_zurueck"].pk) not in sitzungen
    assert str(daten["vorgang_sichtbar"].pk) in vorgaenge and str(daten["vorgang_zurueck"].pk) not in vorgaenge
    assert str(daten["sitzung_sichtbar"].pk) in uebersicht and str(daten["vorgang_sichtbar"].pk) in uebersicht
    assert str(daten["sitzung_zurueck"].pk) not in uebersicht and str(daten["vorgang_zurueck"].pk) not in uebersicht
