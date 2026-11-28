# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlusskontrolle in Work aus dem RIS-Bestand (Issue #538, ``RIS_DECISION_TRACKING=bestand``).

Die Kommune hat keine Verwaltung in mandari Session, ihr RIS liefert aber Abstimmungsergebnisse und einen
veröffentlichten Umsetzungsstand. Mit dem Schalter zeigt Work diese Beschlüsse; ohne ihn bleibt es beim Hinweis, dass
die Beschlusskontrolle eine Verwaltung in Session braucht.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.tenants.models import Organization
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlOrganization, OParlSource

pytestmark = pytest.mark.django_db

BASIS = "https://ris.nachbarstadt.example/oparl"


def _kennung(art: str) -> str:
    return f"{BASIS}/{art}/{uuid.uuid4()}"


@dataclass
class Welt:
    org: Organization
    leser: Any
    body: OParlBody
    radweg: OParlAgendaItem
    ohne_stand: OParlAgendaItem
    fremd: OParlAgendaItem


@pytest.fixture
def welt(org: Organization, make_member: Any) -> Welt:
    quelle = OParlSource.objects.create(name="Fremd-RIS", url=f"{BASIS}/system")
    body = OParlBody.objects.create(external_id=_kennung("bodies"), source=quelle, name="Nachbarstadt")
    org.body = body
    org.save(update_fields=["body"])
    rat = OParlOrganization.objects.create(external_id=_kennung("organizations"), body=body, name="Rat der Stadt")
    sitzung = OParlMeeting.objects.create(
        external_id=_kennung("meetings"), body=body, name="Ratssitzung", start=timezone.now() - timedelta(days=2)
    )
    sitzung.organizations.set([rat])

    def punkt(nummer: str, name: str, **felder: Any) -> OParlAgendaItem:
        return OParlAgendaItem.objects.create(
            external_id=_kennung("agendaitems"), meeting=sitzung, number=nummer, order=int(nummer), name=name, **felder
        )

    radweg = punkt(
        "1",
        "Radweg Hafenstraße",
        vote_result="approved",
        resolution_number="B/2026/17",
        resolution_text="Der Rat beschließt den Radweg.",
        implementation_status="in_progress",
        implementation_deadline=timezone.localdate() + timedelta(days=90),
        implementation_public_note="Die Ausschreibung läuft.",
        implementation_modified=timezone.now(),
    )
    ohne_stand = punkt("2", "Spielplatz Nord", vote_result="approved")
    punkt("3", "Abgelehnter Antrag", vote_result="rejected")
    punkt("4", "Geheim", vote_result="approved", public=False, implementation_status="open")

    andere = OParlBody.objects.create(external_id=_kennung("bodies"), source=quelle, name="Andere Stadt")
    andere_sitzung = OParlMeeting.objects.create(
        external_id=_kennung("meetings"), body=andere, name="Fremd", start=timezone.now()
    )
    fremd = OParlAgendaItem.objects.create(
        external_id=_kennung("agendaitems"),
        meeting=andere_sitzung,
        number="1",
        name="Fremder Beschluss",
        vote_result="approved",
    )
    leser = make_member(org, ["ris.view"], email="leser-beschluesse@example.org")
    return Welt(org=org, leser=leser, body=body, radweg=radweg, ohne_stand=ohne_stand, fremd=fremd)


def _seite(welt: Welt, client_for: Any, name: str, anfrage: dict[str, str] | None = None, **kw: Any) -> Any:
    return client_for(welt.leser.user).get(reverse(name, kwargs={"org_slug": welt.org.slug, **kw}), anfrage or {})


@pytest.mark.parametrize("neu", [True, False], ids=["neues-design", "bisheriges-design"])
def test_liste_aus_dem_bestand_ohne_session(welt: Welt, client_for: Any, settings: Any, neu: bool) -> None:
    settings.RIS_DECISION_TRACKING = "bestand"
    Organization.objects.filter(pk=welt.org.pk).update(work_new_design=neu)

    antwort = _seite(welt, client_for, "work:ris_decisions")

    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert "Noch keine Beschlusskontrolle verfügbar" not in html
    for text in (
        "Radweg Hafenstraße",
        "B/2026/17",
        "Die Ausschreibung läuft.",
        "Spielplatz Nord",
        "Abgelehnter Antrag",
    ):
        assert text in html
    assert "Rat der Stadt" in html
    assert "kein Stand veröffentlicht" in html
    # Nie nichtöffentliche Punkte, nie andere Kommunen, keine Zuständigkeit (gibt es im Bestand nicht)
    assert "Geheim" not in html and "Fremder Beschluss" not in html
    assert "Zuständig" not in html
    sitzung = reverse(
        "work:ris_meeting_detail", kwargs={"org_slug": welt.org.slug, "meeting_id": welt.radweg.meeting_id}
    )
    assert sitzung in html
    if neu:
        # Eine eigene Seite je Beschluss hat nur das neue Erscheinungsbild (Issue #951)
        detail = reverse("work:ris_decision_detail", kwargs={"org_slug": welt.org.slug, "item_id": welt.radweg.pk})
        assert detail in html
    assert antwort.context["tracking_stats"] == {
        "open": 0,
        "in_progress": 1,
        "done": 0,
        "deferred": 0,
        "overdue": 0,
        "total": 3,
    }


def test_filter_aus_dem_bestand(welt: Welt, client_for: Any, settings: Any) -> None:
    settings.RIS_DECISION_TRACKING = "bestand"

    nach_stand = _seite(welt, client_for, "work:ris_decisions", {"status": "in_progress"})
    nach_text = _seite(welt, client_for, "work:ris_decisions", {"q": "spielplatz"})

    assert [item.name for item in nach_stand.context["items"]] == ["Radweg Hafenstraße"]
    assert [item.name for item in nach_text.context["items"]] == ["Spielplatz Nord"]
    assert [o.name for o in nach_stand.context["organizations"]] == ["Rat der Stadt"]


def test_detailseite_aus_dem_bestand(welt: Welt, client_for: Any, settings: Any) -> None:
    settings.RIS_DECISION_TRACKING = "bestand"

    antwort = _seite(welt, client_for, "work:ris_decision_detail", item_id=welt.radweg.pk)

    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert "Der Rat beschließt den Radweg." in html and "Die Ausschreibung läuft." in html
    assert "Rat der Stadt" in html and "In Umsetzung" in html
    assert "Zuständig" not in html
    sitzung = reverse(
        "work:ris_meeting_detail", kwargs={"org_slug": welt.org.slug, "meeting_id": welt.radweg.meeting_id}
    )
    assert sitzung in html
    assert _seite(welt, client_for, "work:ris_decision_detail", item_id=welt.fremd.pk).status_code == 404
    assert _seite(welt, client_for, "work:ris_decision_detail", item_id=uuid.uuid4()).status_code == 404


def test_kommune_ohne_beschluesse_im_bestand(welt: Welt, client_for: Any, settings: Any) -> None:
    settings.RIS_DECISION_TRACKING = "bestand"
    Organization.objects.filter(pk=welt.org.pk).update(work_new_design=True)
    OParlAgendaItem.objects.filter(meeting__body=welt.body).update(vote_result=None)

    html = _seite(welt, client_for, "work:ris_decisions").content.decode()

    assert 'data-testid="ohne-beschluesse"' in html


def test_ohne_schalter_bleibt_es_bei_session(welt: Welt, client_for: Any, settings: Any) -> None:
    """Standard ``session``: Ohne Verwaltung in mandari Session gibt es keine Beschlusskontrolle (wie bisher)."""
    assert settings.RIS_DECISION_TRACKING == "session"

    antwort = _seite(welt, client_for, "work:ris_decisions")

    assert antwort.context["no_session_tenant"] is True
    assert "Radweg Hafenstraße" not in antwort.content.decode()
    assert _seite(welt, client_for, "work:ris_decision_detail", item_id=welt.radweg.pk).status_code == 200
    assert (
        "Radweg Hafenstraße"
        not in _seite(welt, client_for, "work:ris_decision_detail", item_id=welt.radweg.pk).content.decode()
    )
