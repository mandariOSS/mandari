# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Filter der Listen in der Adresse (Issue #1260): Vorgänge, Sitzungen, Gremien und Personen.

Suche, Filter und Reiter schreiben die Adresse per htmx fort (``hx-push-url``); die Antwort trägt die bereinigte
Adresse ohne leere Felder im Kopf ``HX-Push-Url``. Ein Aufruf mit Parametern stellt die Auswahl wieder her, und stellt
htmx eine Seite aus dem Verlauf wieder her (``HX-History-Restore-Request``), kommt die ganze Seite. Reiter behalten den
Suchbegriff, sauber kodiert.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import pytest
from django.http import QueryDict
from django.template import engines
from django.test import Client, RequestFactory
from django.utils import timezone

from insight_core.listenadresse import ohne_leere, saubere_adresse
from insight_core.models import (
    OParlBody,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

pytestmark = pytest.mark.django_db

RIS = "https://ris.adressstadt.example/oparl"
HTMX = {"HX-Request": "true"}
VERLAUF = {"HX-Request": "true", "HX-History-Restore-Request": "true"}


@pytest.fixture
def stadt() -> dict[str, Any]:
    source = OParlSource.objects.create(name="RIS Adressstadt", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Adressstadt", slug="adressstadt")
    rat = OParlOrganization.objects.create(external_id=f"{RIS}/org/1", body=body, name="Rat der Stadt")
    sitzungen: dict[str, OParlMeeting] = {}
    for nummer, (name, art, tage) in enumerate(
        [("Radweg am Kanal", "Antrag", -20), ("Radweg & Gehweg", "Anfrage", -10), ("Spielplatz", "Antrag", 5)]
    ):
        OParlPaper.objects.create(external_id=f"{RIS}/paper/{nummer}", body=body, name=name, paper_type=art)
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/{nummer}",
            body=body,
            name=f"Sitzung {name}",
            start=timezone.now() + timedelta(days=tage),
        )
        sitzung.organizations.add(rat)
        sitzungen[name] = sitzung
    person = OParlPerson.objects.create(
        external_id=f"{RIS}/person/1", body=body, name="Anna Rad", given_name="Anna", family_name="Rad"
    )
    OParlMembership.objects.create(external_id=f"{RIS}/membership/1", person=person, organization=rat)
    return {"body": body, "sitzungen": sitzungen}


def _get(pfad: str, daten: dict[str, str] | None = None, **headers: str) -> Any:
    return Client().get(pfad, daten or {}, headers=headers)


class TestAdresse:
    def test_ohne_leere_und_saubere_adresse(self) -> None:
        params = QueryDict("q=&type=Antrag&fraktion=%20&tag=a&tag=&page=3", mutable=True)
        assert ohne_leere(params).urlencode() == "type=Antrag&tag=a&page=3"
        request = RequestFactory().get("/insight/vorgaenge/", {"q": "", "type": "Antrag", "page": "2"})
        assert saubere_adresse(request) == "/insight/vorgaenge/?type=Antrag"
        assert saubere_adresse(RequestFactory().get("/insight/vorgaenge/", {"q": ""})) == "/insight/vorgaenge/"

    def test_reiter_behalten_die_suche_kodiert(self) -> None:
        request = RequestFactory().get(
            "/insight/termine/", {"q": "Rad & Weg", "period": "past", "page": "4", "next": "https://evil.example/"}
        )
        html = (
            engines["django"]
            .from_string("{% load insight_listen %}{% adresse_mit behalten='q' period='all' %}")
            .render({"request": request})
        )
        # Nur der Suchbegriff bleibt; fremde Parameter und die Seitenzahl nicht
        assert html == "?q=Rad+%26+Weg&amp;period=all"


class TestVorgaenge:
    pfad = "/insight/k/adressstadt/vorgaenge/"

    def test_htmx_liste_mit_adresse_und_vary(self, stadt: dict[str, Any]) -> None:
        antwort = _get(self.pfad, {"q": "Radweg", "type": "Antrag", "fraktion": ""}, **HTMX)
        html = antwort.content.decode()

        assert "<html" not in html and "Radweg am Kanal" in html and "Radweg &amp; Gehweg" not in html
        assert antwort["HX-Push-Url"] == f"{self.pfad}?q=Radweg&type=Antrag"
        assert "HX-Request" in antwort["Vary"] and "HX-History-Restore-Request" in antwort["Vary"]

    def test_aufruf_mit_parametern_stellt_die_auswahl_wieder_her(self, stadt: dict[str, Any]) -> None:
        antwort = _get(self.pfad, {"q": "Radweg", "type": "Anfrage"})
        html = antwort.content.decode()

        assert "<html" in html and "HX-Push-Url" not in antwort
        assert 'value="Radweg"' in html
        assert re.search(r'<option value="Anfrage"\s+selected>', html)
        assert "Radweg &amp; Gehweg" in html and "Radweg am Kanal" not in html
        # Jedes Filterfeld schreibt die Adresse fort (Suche und Typ; Fraktionen gibt es hier nicht), ohne Schnappschuss
        assert html.count('hx-push-url="true"') == 2 and 'hx-history="false"' in html

    def test_verlauf_ohne_speicher_bekommt_die_ganze_seite(self, stadt: dict[str, Any]) -> None:
        antwort = _get(self.pfad, {"q": "Radweg"}, **VERLAUF)
        assert "<html" in antwort.content.decode() and "HX-Push-Url" not in antwort
        assert "HX-History-Restore-Request" in antwort["Vary"]


class TestSitzungen:
    pfad = "/insight/k/adressstadt/termine/"

    def test_suche_nimmt_den_zeitraum_mit(self, stadt: dict[str, Any]) -> None:
        html = _get(self.pfad, {"period": "past", "q": "Rad"}).content.decode()

        assert '<input type="hidden" name="period" value="past">' in html
        assert 'hx-include="closest form"' in html and 'hx-push-url="true"' in html
        assert 'value="Rad"' in html
        # Reiter: gewählter Zeitraum markiert, Suchbegriff bleibt erhalten
        assert 'href="?q=Rad&amp;period=all"' in html
        assert re.search(r'href="\?q=Rad&amp;period=past" aria-current="page"', html)

    def test_htmx_suche_im_zeitraum(self, stadt: dict[str, Any]) -> None:
        antwort = _get(self.pfad, {"period": "past", "q": "Radweg"}, **HTMX)
        html = antwort.content.decode()

        sitzungen = stadt["sitzungen"]
        assert "<html" not in html and str(sitzungen["Radweg am Kanal"].pk) in html
        assert str(sitzungen["Spielplatz"].pk) not in html
        assert antwort["HX-Push-Url"] == f"{self.pfad}?period=past&q=Radweg"

    def test_ohne_zeitraum_bleibt_die_adresse_kurz(self, stadt: dict[str, Any]) -> None:
        antwort = _get(self.pfad, {"period": "", "q": "Spielplatz"}, **HTMX)
        assert antwort["HX-Push-Url"] == f"{self.pfad}?q=Spielplatz"
        assert str(stadt["sitzungen"]["Spielplatz"].pk) in antwort.content.decode()


class TestGremienUndPersonen:
    def test_gremien_reiter_in_der_adresse(self, stadt: dict[str, Any]) -> None:
        pfad = "/insight/k/adressstadt/gremien/"
        html = _get(pfad, {"tab": "all", "q": "Rat"}).content.decode()

        assert '<input type="hidden" name="tab" value="all">' in html and "hx-vals" not in html
        assert re.search(r'href="\?q=Rat&amp;tab=all" aria-current="page"', html)
        antwort = _get(pfad, {"tab": "all", "q": "Rat"}, **HTMX)
        assert antwort["HX-Push-Url"] == f"{pfad}?tab=all&q=Rat" and "Rat der Stadt" in antwort.content.decode()

    def test_personen_suche_in_der_adresse(self, stadt: dict[str, Any]) -> None:
        pfad = "/insight/k/adressstadt/personen/"
        html = _get(pfad, {"q": "Anna"}).content.decode()
        assert 'hx-push-url="true"' in html and 'value="Anna"' in html and "Anna Rad" in html
        antwort = _get(pfad, {"q": "Anna"}, **HTMX)
        assert antwort["HX-Push-Url"] == f"{pfad}?q=Anna" and "<html" not in antwort.content.decode()
