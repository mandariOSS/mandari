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


class _Vorschau:
    """Suchdienst für die Kopfzeilen-Vorschau: liefert feste Treffer wie ``search_all``."""

    def __init__(self, treffer: list[dict[str, Any]]) -> None:
        self.treffer = treffer

    def search_all(self, **kwargs: Any) -> dict[str, Any]:
        return {"results": self.treffer, "total": len(self.treffer), "page": 1, "pages": 1}


def test_suchvorschau_der_kopfzeile_ohne_hausnummer(
    antrag: OParlPaper, make_paper: Callable[..., OParlPaper], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die Vorschau nutzt ``format_search_result`` statt ``present_groups``; auch dort keine Hausnummer (#1269)."""
    andere = make_paper(antrag.body, name="Sanierung Musterstraße 12", paper_type="Beschlussvorlage")
    treffer: list[dict[str, Any]] = [
        {
            "type": "paper",
            "id": str(antrag.pk),
            "name": TITEL,
            "paper_type": "Bürgerantrag",
            "_formatted": {
                "name": f"Parkverbot vor {HIGHLIGHT_PRE}Musterstraße{HIGHLIGHT_POST} 12a",
                "text_content": f"Absender: {HIGHLIGHT_PRE}Musterweg{HIGHLIGHT_POST} 3",
            },
        },
        {
            # Dokument eines Bürgerantrags, dessen Name die Art nicht nennt: Art kommt aus der Datenbank
            "type": "file",
            "id": "6f1c1f5e-2d0a-4c1b-9a51-0e8c2b7d9a10",
            "name": "Schreiben Musterweg 3",
            "paper_id": str(antrag.pk),
            "paper_name": "Parkverbot",
            "text_preview": "Erika Muster, Musterweg 3, 48143 Beispielstadt",
        },
        {"type": "paper", "id": str(andere.pk), "name": andere.name, "paper_type": "Beschlussvorlage"},
    ]
    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: _Vorschau(treffer))
    html = _aktiv(Client(), antrag.body).get("/insight/suche/partials/results/?q=Musterstra%C3%9Fe&dropdown=1")
    seite = html.content.decode()
    assert OHNE in seite and "12a" not in seite
    assert f"Schreiben Musterweg {OHNE_NR}" in seite and "Musterweg 3" not in seite
    # Andere Vorgänge behalten ihren Titel samt Hausnummer
    assert "Sanierung Musterstraße 12" in seite


def test_suchvorschau_ohne_suchdienst_ohne_hausnummer(antrag: OParlPaper, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rückfall auf die Datenbanksuche (Suchdienst nicht erreichbar), Vorschau und ganze Seite (#1269)."""

    class _Kaputt:
        def search_all(self, **kwargs: Any) -> Any:
            raise ConnectionError("Suchdienst nicht erreichbar")

        search_grouped = search_all

    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: _Kaputt())
    client = _aktiv(Client(), antrag.body)
    for dropdown in ("&dropdown=1", ""):
        seite = client.get(f"/insight/suche/partials/results/?q=Parkverbot{dropdown}").content.decode()
        assert OHNE in seite and "Musterstraße 12a" not in seite


def test_beschluesse_ohne_umsetzungsstand_ohne_hausnummer(antrag: OParlPaper) -> None:
    """„Zuletzt beraten“ auf /beschluesse ohne Umsetzungsstand (#1261) zeigt den Titel wie die Startseite (#1269)."""
    sitzung = OParlMeeting.objects.create(
        external_id=f"{antrag.external_id}/rat", body=antrag.body, name="Rat", start=timezone.now() - timedelta(days=2)
    )
    mit_vorgang = OParlAgendaItem.objects.create(
        external_id=f"{sitzung.external_id}/top1", meeting=sitzung, number="1", name=TITEL, result="beschlossen"
    )
    OParlConsultation.objects.create(
        external_id=f"{sitzung.external_id}/beratung",
        body=antrag.body,
        paper=antrag,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=mit_vorgang.external_id,
    )
    OParlAgendaItem.objects.create(
        external_id=f"{sitzung.external_id}/top2",
        meeting=sitzung,
        number="2",
        name="Bürgerantrag Am Wehr 4",
        result="abgelehnt",
    )
    html = _aktiv(Client(), antrag.body).get("/insight/beschluesse/").content.decode()
    assert "Zuletzt beraten" in html
    assert OHNE in html and "Musterstraße 12a" not in html
    assert f"Bürgerantrag Am Wehr {OHNE_NR}" in html and "Am Wehr 4" not in html


def test_ki_zusammenfassung_und_description_ohne_hausnummer(antrag: OParlPaper) -> None:
    """Die gespeicherte Zusammenfassung bleibt vollständig, die Seite und ihre Description lassen die Nummer aus."""
    from insight_core.services.ki_bewertung import fassung

    text = "Die Antragstellerin aus der Musterstraße 12a bittet um ein Parkverbot vor ihrem Haus."
    antrag.summary = text
    antrag.save(update_fields=["summary"])
    html = Client().get(f"/insight/vorgaenge/{antrag.pk}/").content.decode()
    teil = re.search(r'<div id="paper-summary"(.*?)</div>\s*</div>', html, re.S)
    assert teil is not None
    assert f"aus der Musterstraße {OHNE_NR} bittet" in teil.group(1)
    assert "Musterstraße 12a" not in html
    # Die Bewertung prüft weiter gegen den gespeicherten Text
    assert f'data-fassung="{fassung(text)}"' in html
    beschreibung = re.search(r'<meta name="description" content="([^"]*)"', html)
    assert beschreibung is not None and "12a" not in beschreibung.group(1)
    antrag.refresh_from_db()
    assert antrag.summary == text


def test_ki_zusammenfassung_nach_dem_erzeugen_ohne_hausnummer(antrag: OParlPaper) -> None:
    """Auch die Teilansicht nach „Zusammenfassen“ (HTMX-Endpunkt, schon vorhandene Zusammenfassung)."""
    antrag.summary = "Es geht um die Einfahrt Musterstraße 12a."
    antrag.save(update_fields=["summary"])
    html = Client().post(f"/insight/vorgaenge/{antrag.pk}/zusammenfassung/", HTTP_HX_REQUEST="true").content.decode()
    assert f"Einfahrt Musterstraße {OHNE_NR}." in html and "12a" not in html


def test_umsetzungsstand_eines_buergerantrags_ohne_hausnummer(antrag: OParlPaper) -> None:
    """Seite, Liste und Seitentitel eines Beschlusses mit Umsetzungsstand (#48); Work behält den Namen (#1269)."""
    from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant

    tenant = SessionTenant.objects.create(
        name="Verwaltung", slug="verwaltung", oparl_body=antrag.body, insight_publish=True, implementation_publish=True
    )
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, organization=rat, name="Rat", start=timezone.now() - timedelta(days=7), is_public=True
    )
    beschluss = SessionAgendaItem.objects.create(
        meeting=sitzung,
        number="1",
        name=f"Bürgerantrag: {TITEL}",
        vote_result="approved",
        implementation_public=True,
    )
    client = _aktiv(Client(), antrag.body)
    for adresse in (f"/insight/beschluesse/{beschluss.pk}/", "/insight/beschluesse/"):
        html = client.get(adresse).content.decode()
        assert f"Bürgerantrag: {OHNE}" in html, adresse
        assert "Musterstraße 12a" not in html, adresse
    beschluss.refresh_from_db()
    assert beschluss.name == f"Bürgerantrag: {TITEL}"
