# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Keine Hausnummern von Privatanschriften in öffentlichen Listen (Issue #1269): Vorgangsliste, Startseite, Merkliste,
Tagesordnung, Karte, Umkreissuche, Suchtreffer und Benachrichtigungen. Work und die Datenbank behalten den vollen Titel.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from insight_core.models import OParlAgendaItem, OParlBody, OParlConsultation, OParlMeeting, OParlPaper
from insight_core.services import karten_punkte
from insight_core.services.ortsschutz import OHNE_NR
from insight_core.services.paper_locations import nearby_papers, sync_paper_locations
from insight_core.services.search_presentation import present_groups
from insight_core.services.search_service import HIGHLIGHT_POST, HIGHLIGHT_PRE
from insight_core.tests.conftest import CENTER_LAT, CENTER_LON

pytestmark = pytest.mark.django_db

TITEL = "Parkverbot vor Musterstraße 12a"
OHNE = f"Parkverbot vor Musterstraße {OHNE_NR}"


@pytest.fixture
def antrag(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> OParlPaper:
    paper = make_paper(
        geo_body,
        name=TITEL,
        paper_type="Bürgerantrag",
        reference="BA/0001/2026",
        date=timezone.localdate(),
        locations=[{"lat": CENTER_LAT, "lon": CENTER_LON, "name": "Musterstraße 12a", "source": "oparl"}],
    )
    sync_paper_locations(paper)
    return paper


def _aktiv(client: Client, body: OParlBody) -> Client:
    session = client.session
    session["active_body_id"] = str(body.pk)
    session.save()
    return client


def test_vorgangsliste_ohne_hausnummer(antrag: OParlPaper) -> None:
    client = _aktiv(Client(), antrag.body)
    html = client.get("/insight/vorgaenge/", HTTP_HX_REQUEST="true").content.decode()
    assert OHNE in html and "Musterstraße 12a" not in html


def test_tagesordnung_ohne_hausnummer(antrag: OParlPaper) -> None:
    sitzung = OParlMeeting.objects.create(
        external_id=f"{antrag.external_id}/meeting",
        body=antrag.body,
        name="Bezirksvertretung Mitte",
        start=timezone.now() - timedelta(days=1),
    )
    punkt = OParlAgendaItem.objects.create(
        external_id=f"{sitzung.external_id}/top", meeting=sitzung, number="3", name=f"Bürgerantrag: {TITEL}"
    )
    OParlConsultation.objects.create(
        external_id=f"{sitzung.external_id}/consultation",
        body=antrag.body,
        paper=antrag,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=punkt.external_id,
    )
    html = Client().get(f"/insight/termine/{sitzung.pk}/").content.decode()
    treffer = re.search(r'<section aria-labelledby="tagesordnung-titel">(.*?)</section>', html, re.S)
    assert treffer is not None
    teil = treffer.group(1)
    assert f"Bürgerantrag: {OHNE}" in teil and "Musterstraße 12a" not in teil


def test_karte_im_buergerportal_ohne_hausnummer_in_work_vollstaendig(antrag: OParlPaper) -> None:
    oeffentlich = karten_punkte.geojson([antrag.body], oeffentlich=True)["features"][0]["properties"]
    assert (oeffentlich["title"], oeffentlich["location_name"]) == (OHNE, "Musterstraße")
    intern = karten_punkte.geojson([antrag.body])["features"][0]["properties"]
    assert (intern["title"], intern["location_name"]) == (TITEL, "Musterstraße 12a")
    antwort = _aktiv(Client(), antrag.body).get("/insight/karte/partials/markers/?all=1")
    assert antwort.status_code == 200
    assert OHNE in antwort.json()["features"][0]["properties"]["title"]


def test_umkreissuche_ohne_hausnummer(antrag: OParlPaper) -> None:
    treffer = nearby_papers(antrag.body, CENTER_LAT, CENTER_LON, 200)
    assert [t["name"] for t in treffer] == [OHNE]


def test_suchtreffer_ohne_hausnummer(antrag: OParlPaper) -> None:
    gruppe: dict[str, Any] = {
        "paper": {
            "id": str(antrag.pk),
            "name": TITEL,
            "paper_type": "Bürgerantrag",
            "reference": antrag.reference,
            "_formatted": {"name": f"Parkverbot vor {HIGHLIGHT_PRE}Musterstraße{HIGHLIGHT_POST} 12a"},
        },
        "file": {
            "id": "1",
            "name": "Antrag Musterstraße 12a",
            "text_content": "x",
            "_formatted": {"text_content": f"Absender: Erika, {HIGHLIGHT_PRE}Musterweg{HIGHLIGHT_POST} 3 & Co"},
        },
        "others": [],
    }
    oeffentlich = present_groups([gruppe])[0]
    assert str(oeffentlich["title"]) == OHNE
    assert str(oeffentlich["snippet"]) == f"Absender: Erika, Musterweg {OHNE_NR} &amp; Co"
    assert oeffentlich["fundstelle"]["label"] == f"Antrag Musterstraße {OHNE_NR}"
    intern = present_groups([gruppe], oeffentlich=False)[0]
    assert "12a" in str(intern["title"])


def test_startseite_ohne_hausnummer(antrag: OParlPaper) -> None:
    html = _aktiv(Client(), antrag.body).get("/insight/").content.decode()
    assert OHNE in html and "Musterstraße 12a" not in html


def test_benachrichtigung_ohne_hausnummer(antrag: OParlPaper, monkeypatch: pytest.MonkeyPatch) -> None:
    from insight_core.management.commands.generate_alerts import Command
    from insight_core.models import InsightSubscriber, SubscriptionAlert

    def ohne_suchdienst() -> Any:
        raise ConnectionError("Suchdienst nicht erreichbar")

    monkeypatch.setattr("insight_core.services.search_service.get_search_service", ohne_suchdienst)
    abonnent = InsightSubscriber.objects.create(
        email="leserin@example.org", body=antrag.body, keyword="Parkverbot", keyword_active=True, confirmed=True
    )
    cast(Any, Command())._generate_keyword_alerts(abonnent, timezone.now() - timedelta(days=7), dry_run=False)
    assert list(SubscriptionAlert.objects.values_list("entity_title", flat=True)) == [OHNE]
