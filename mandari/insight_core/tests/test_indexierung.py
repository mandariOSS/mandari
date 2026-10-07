# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Indexierung des Bürgerportals durch Suchmaschinen (Issue #914).

- Sitemaps: alle öffentlichen Vorgänge und Sitzungen in nummerierten Dateien (höchstens ``EINTRAEGE_JE_DATEI``),
  im Index gelistet mit ``lastmod`` je Datei; Pfade mit dem Präfix ``/sitemap-insight-``; Stadtseite in der
  Grund-Sitemap; Personen nur mit laufender Mitgliedschaft.
- Personen ohne laufende Mitgliedschaft: ``noindex, follow``, Seite bleibt erreichbar.
- ``noindex`` für Suche, Listen mit Parametern, Teilansichten und Dateien; Beschlussseiten mit robots und Canonical.
- Crawlbare Wege: Brotkrume auf die Stadtseite ohne Weiterleitung, Umwege über die Wahl der Kommune mit ``nofollow``,
  Stadtseiten auf ``/insight/`` als Textzeile.
"""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from insight_core import publication
from insight_core.models import (
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
    validate_body_slug,
)
from insight_core.services import file_cache, indexierung, safe_fetch, sitemaps

pytestmark = pytest.mark.django_db

NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
HEUTE = date(2026, 10, 7)
# Den Cache leert vor und nach jedem Test die Fixture in insight_core/tests/conftest.py


def _kommune(nummer: int, name: str, **felder: Any) -> OParlBody:
    source = OParlSource.objects.create(name=f"RIS {name}", url=f"https://ris{nummer}.example.org/system")
    return OParlBody.objects.create(
        external_id=f"https://ris{nummer}.example.org/body/1", source=source, name=name, **felder
    )


def _vorgang(body: OParlBody, nummer: int, **felder: Any) -> OParlPaper:
    return OParlPaper.objects.create(
        external_id=f"{body.external_id}/paper/{nummer}", body=body, name=f"Vorgang {nummer}", **felder
    )


def _sitzung(body: OParlBody, nummer: int, **felder: Any) -> OParlMeeting:
    return OParlMeeting.objects.create(
        external_id=f"{body.external_id}/meeting/{nummer}", body=body, name=f"Sitzung {nummer}", **felder
    )


def _person(body: OParlBody, nummer: int, **felder: Any) -> OParlPerson:
    return OParlPerson.objects.create(
        external_id=f"{body.external_id}/person/{nummer}", body=body, name=f"Person {nummer}", **felder
    )


def _gremium(body: OParlBody, nummer: int, **felder: Any) -> OParlOrganization:
    felder.setdefault("name", f"Gremium {nummer}")
    return OParlOrganization.objects.create(
        external_id=f"{body.external_id}/organization/{nummer}", body=body, **felder
    )


def _mitgliedschaft(person: OParlPerson, gremium: OParlOrganization, **felder: Any) -> OParlMembership:
    return OParlMembership.objects.create(
        external_id=f"{person.external_id}/membership/{gremium.pk}/{uuid.uuid4()}",
        person=person,
        organization=gremium,
        **felder,
    )


def _xml(client: Client, url: str) -> ET.Element:
    antwort = client.get(url)
    assert antwort.status_code == 200, (url, antwort.status_code)
    assert antwort["Content-Type"].startswith("application/xml")
    assert antwort["Cache-Control"] == "public, max-age=86400"
    return ET.fromstring(antwort.content)


def _locs(wurzel: ET.Element) -> list[str]:
    return [loc.text or "" for loc in wurzel.iterfind(".//s:loc", NS)]


def _lastmods(wurzel: ET.Element) -> dict[str, str]:
    """``loc`` → ``lastmod`` je Eintrag eines Sitemap-Index."""
    return {
        eintrag.findtext("s:loc", "", NS): eintrag.findtext("s:lastmod", "", NS)
        for eintrag in wurzel.iterfind("s:sitemap", NS)
    }


def _robots(html: str) -> str | None:
    treffer = re.search(r'<meta name="robots" content="([^"]*)"', html)
    return treffer.group(1) if treffer else None


def _canonical(html: str) -> str | None:
    treffer = re.search(r'<link rel="canonical" href="([^"]*)"', html)
    return treffer.group(1) if treffer else None


# =============================================================================
# Sitemaps
# =============================================================================


@pytest.fixture
def klein(monkeypatch: pytest.MonkeyPatch) -> int:
    """Drei Einträge je Datei: Die Aufteilung lässt sich mit wenigen Objekten prüfen."""
    monkeypatch.setattr(sitemaps, "EINTRAEGE_JE_DATEI", 3)
    return 3


@pytest.fixture
def stadt(klein: int) -> dict[str, Any]:
    """Kommune mit Slug, sieben Vorgängen (einer gelöscht) und vier Sitzungen; Eingang in gemischter Reihenfolge."""
    body = _kommune(1, "Stadt Köln", display_name="Köln", slug="koeln", last_sync=timezone.now())
    vorgaenge = []
    for nummer in range(8):
        vorgaenge.append(
            _vorgang(
                body,
                nummer,
                deleted=nummer == 7,
                oparl_modified=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=10 * nummer),
            )
        )
    # Eingang rückwärts: Vorgang 0 kam zuletzt, die Aufteilung folgt dem Eingang, nicht der Nummer
    eingang = datetime(2025, 1, 1, tzinfo=UTC)
    for rang, vorgang in enumerate(reversed(vorgaenge)):
        OParlPaper.objects.filter(pk=vorgang.pk).update(created_at=eingang + timedelta(hours=rang))
    sitzungen = [_sitzung(body, nummer, start=timezone.now()) for nummer in range(4)]
    return {"body": body, "vorgaenge": vorgaenge, "sitzungen": sitzungen}


class TestSitemapIndex:
    def test_listet_grund_sitemap_und_nummerierte_dateien(self, client: Client, stadt: dict[str, Any]) -> None:
        site = settings.SITE_URL
        index = _xml(client, "/sitemap-insight-index.xml")

        assert _locs(index) == [
            f"{site}/sitemap-insight-koeln.xml",
            f"{site}/sitemap-insight-koeln-vorgaenge-1.xml",
            f"{site}/sitemap-insight-koeln-vorgaenge-2.xml",
            f"{site}/sitemap-insight-koeln-vorgaenge-3.xml",
            f"{site}/sitemap-insight-koeln-sitzungen-1.xml",
            f"{site}/sitemap-insight-koeln-sitzungen-2.xml",
        ]

    def test_alle_pfade_mit_dem_praefix_fuer_django(self, client: Client, stadt: dict[str, Any]) -> None:
        _kommune(2, "Stadt Darmstadt")  # ohne Slug: Kennung ist die ID
        for loc in _locs(_xml(client, "/sitemap-insight-index.xml")):
            assert loc.startswith(f"{settings.SITE_URL}/sitemap-insight-"), loc
            assert loc.endswith(".xml"), loc

    def test_lastmod_je_datei_ist_die_juengste_aenderung_ihrer_eintraege(
        self, client: Client, stadt: dict[str, Any]
    ) -> None:
        lastmods = _lastmods(_xml(client, "/sitemap-insight-index.xml"))
        site = settings.SITE_URL

        # Eingang rückwärts: Datei 1 = Vorgänge 6, 5, 4 (Vorgang 7 ist gelöscht), Datei 3 = Vorgang 0
        assert lastmods[f"{site}/sitemap-insight-koeln-vorgaenge-1.xml"] == "2026-03-02T00:00:00+00:00"
        assert lastmods[f"{site}/sitemap-insight-koeln-vorgaenge-2.xml"] == "2026-01-31T00:00:00+00:00"
        assert lastmods[f"{site}/sitemap-insight-koeln-vorgaenge-3.xml"] == "2026-01-01T00:00:00+00:00"
        assert lastmods[f"{site}/sitemap-insight-koeln.xml"] == stadt["body"].last_sync.strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )

    def test_abfragen_haengen_nicht_von_der_zahl_der_eintraege_ab(self, client: Client, klein: int) -> None:
        body = _kommune(1, "Stadt Köln", slug="koeln")
        _vorgang(body, 0)
        client.get("/sitemap-insight-index.xml")  # Aufwärmen (Sitzung, Einstellungen)
        cache.clear()
        with CaptureQueriesContext(connection) as wenige:
            client.get("/sitemap-insight-index.xml")
        for nummer in range(1, 20):
            _vorgang(body, nummer)
            _sitzung(body, nummer, start=timezone.now())
        cache.clear()
        with CaptureQueriesContext(connection) as viele:
            index = client.get("/sitemap-insight-index.xml").content.decode()
        assert "/sitemap-insight-koeln-vorgaenge-7.xml" in index, "ohne Cache neu aufgeteilt"
        assert len(viele) == len(wenige)

    def test_aufteilung_aus_dem_cache(self, client: Client, stadt: dict[str, Any]) -> None:
        """Die Aufteilung liest den ganzen Bestand einer Kommune: Der Index rechnet sie nicht bei jedem Abruf neu."""

        def aufteilungen(erfasst: CaptureQueriesContext) -> int:
            return sum("ROW_NUMBER" in abfrage["sql"].upper() for abfrage in erfasst.captured_queries)

        with CaptureQueriesContext(connection) as erster:
            vorher = client.get("/sitemap-insight-index.xml").content
        with CaptureQueriesContext(connection) as zweiter:
            nachher = client.get("/sitemap-insight-index.xml").content
        assert aufteilungen(erster) == len(sitemaps.ARTEN)
        assert aufteilungen(zweiter) == 0
        assert nachher == vorher

        # Neue Vorgänge erscheinen spätestens nach Ablauf des Caches
        for nummer in range(10, 13):
            _vorgang(stadt["body"], nummer)
        assert "koeln-vorgaenge-4.xml" not in client.get("/sitemap-insight-index.xml").content.decode()
        cache.clear()
        assert "koeln-vorgaenge-4.xml" in client.get("/sitemap-insight-index.xml").content.decode()

    def test_cache_gilt_nur_fuer_die_gleiche_dateigroesse(
        self, client: Client, stadt: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert "koeln-vorgaenge-3.xml" in client.get("/sitemap-insight-index.xml").content.decode()
        monkeypatch.setattr(sitemaps, "EINTRAEGE_JE_DATEI", 10)
        index = client.get("/sitemap-insight-index.xml").content.decode()
        assert "koeln-vorgaenge-1.xml" in index and "koeln-vorgaenge-2.xml" not in index

    def test_kommune_ohne_vorgaenge_hat_nur_die_grund_sitemap(self, client: Client) -> None:
        _kommune(1, "Stadt Leerheim", slug="leerheim")
        assert _locs(_xml(client, "/sitemap-insight-index.xml")) == [
            f"{settings.SITE_URL}/sitemap-insight-leerheim.xml"
        ]


class TestHead:
    """Suchmaschinen und Prüfwerkzeuge fragen robots.txt und Sitemaps auch per HEAD an (vorher 405)."""

    @pytest.mark.parametrize(
        "pfad",
        [
            "/robots.txt",
            "/sitemap-insight-index.xml",
            "/sitemap-insight-koeln.xml",
            "/sitemap-insight-koeln-vorgaenge-1.xml",
            "/sitemap-insight-koeln-sitzungen-1.xml",
        ],
    )
    def test_head_wie_get_ohne_inhalt(self, client: Client, stadt: dict[str, Any], pfad: str) -> None:
        antwort = client.head(pfad)

        assert antwort.status_code == 200
        assert antwort.content == b""
        assert antwort["Content-Type"] == client.get(pfad)["Content-Type"]

    def test_andere_methoden_bleiben_abgelehnt(self, client: Client, stadt: dict[str, Any]) -> None:
        assert client.post("/sitemap-insight-index.xml").status_code == 405


class TestNummerierteDateien:
    def test_alle_vorgaenge_genau_einmal_in_der_reihenfolge_des_eingangs(
        self, client: Client, stadt: dict[str, Any]
    ) -> None:
        dateien = [_locs(_xml(client, f"/sitemap-insight-koeln-vorgaenge-{n}.xml")) for n in (1, 2, 3)]
        ids = [str(v.pk) for v in stadt["vorgaenge"]]
        site = settings.SITE_URL

        assert [len(d) for d in dateien] == [3, 3, 1]
        alle = [loc for datei in dateien for loc in datei]
        assert alle == [f"{site}/insight/vorgaenge/{ids[n]}/" for n in (6, 5, 4, 3, 2, 1, 0)]
        assert f"/insight/vorgaenge/{ids[7]}/" not in "".join(alle), "gelöschte Vorgänge fehlen"

    def test_sitzungen(self, client: Client, stadt: dict[str, Any]) -> None:
        eins = _xml(client, "/sitemap-insight-koeln-sitzungen-1.xml")
        zwei = _xml(client, "/sitemap-insight-koeln-sitzungen-2.xml")
        alle = _locs(eins) + _locs(zwei)
        assert sorted(alle) == sorted(f"{settings.SITE_URL}/insight/termine/{s.pk}/" for s in stadt["sitzungen"])
        assert eins.findtext("s:url/s:changefreq", "", NS) == "weekly"
        assert eins.findtext("s:url/s:priority", "", NS) == "0.7"

    def test_eintrag_mit_lastmod(self, client: Client, stadt: dict[str, Any]) -> None:
        datei = _xml(client, "/sitemap-insight-koeln-vorgaenge-3.xml")
        assert datei.findtext("s:url/s:lastmod", "", NS) == "2026-01-01T00:00:00+00:00"
        assert datei.findtext("s:url/s:priority", "", NS) == "0.6"

    def test_hoechstens_zehntausend_je_datei(self) -> None:
        assert sitemaps.EINTRAEGE_JE_DATEI == 10_000

    @pytest.mark.parametrize(
        "pfad",
        [
            "/sitemap-insight-koeln-vorgaenge-4.xml",  # hinter der letzten Datei
            "/sitemap-insight-koeln-vorgaenge-0.xml",
            "/sitemap-insight-koeln-vorgaenge-01.xml",
            "/sitemap-insight-koeln-beschluesse-1.xml",
            "/sitemap-insight-bonn-vorgaenge-1.xml",
        ],
    )
    def test_keine_datei(self, client: Client, stadt: dict[str, Any], pfad: str) -> None:
        assert client.get(pfad).status_code == 404

    def test_id_leitet_auf_den_slug_um(self, client: Client, stadt: dict[str, Any]) -> None:
        antwort = client.get(f"/sitemap-insight-{stadt['body'].pk}-vorgaenge-2.xml")
        assert antwort.status_code == 301
        assert antwort["Location"] == "/sitemap-insight-koeln-vorgaenge-2.xml"

    def test_kommune_ohne_slug_ueber_die_id(self, client: Client, klein: int) -> None:
        body = _kommune(2, "Stadt Darmstadt")
        vorgang = _vorgang(body, 1)
        assert (
            f"/sitemap-insight-{body.pk}-vorgaenge-1.xml</loc>"
            in client.get("/sitemap-insight-index.xml").content.decode()
        )
        assert (
            f"/insight/vorgaenge/{vorgang.pk}/"
            in client.get(f"/sitemap-insight-{body.pk}-vorgaenge-1.xml").content.decode()
        )

    def test_nicht_gelistete_kommune(self, client: Client, klein: int) -> None:
        body = _kommune(3, "Pilotstadt", slug="pilot", is_listed=False)
        _vorgang(body, 1)
        assert "pilot" not in client.get("/sitemap-insight-index.xml").content.decode()
        assert client.get("/sitemap-insight-pilot-vorgaenge-1.xml").status_code == 404

    def test_slug_mit_endung_der_nummerierten_dateien_ist_reserviert(self) -> None:
        for slug in ("koeln-vorgaenge-2", "bonn-sitzungen-10"):
            with pytest.raises(ValidationError, match="reserviert"):
                validate_body_slug(slug)
        validate_body_slug("vorgaenge-koeln")
        validate_body_slug("koeln-sitzungen")


class TestGrundSitemap:
    def test_stadtseite_mit_hoher_prioritaet(self, client: Client, stadt: dict[str, Any]) -> None:
        grund = _xml(client, "/sitemap-insight-koeln.xml")
        erster = grund.find("s:url", NS)
        assert erster is not None
        assert erster.findtext("s:loc", "", NS) == f"{settings.SITE_URL}/insight/k/koeln/"
        assert erster.findtext("s:priority", "", NS) == "0.9"
        assert erster.findtext("s:changefreq", "", NS) == "daily"

    def test_ohne_slug_keine_stadtseite(self, client: Client) -> None:
        body = _kommune(2, "Stadt Darmstadt")
        assert "/insight/k/" not in client.get(f"/sitemap-insight-{body.pk}.xml").content.decode()

    def test_vorgaenge_und_sitzungen_nur_in_den_nummerierten_dateien(
        self, client: Client, stadt: dict[str, Any]
    ) -> None:
        grund = client.get("/sitemap-insight-koeln.xml").content.decode()
        assert "/insight/vorgaenge/" not in grund and "/insight/termine/" not in grund

    def test_personen_nur_mit_laufender_mitgliedschaft(self, client: Client) -> None:
        body = _kommune(1, "Stadt Köln", slug="koeln")
        rat = _gremium(body, 1)
        aufgeloest = _gremium(body, 2, deleted=True)
        heute = timezone.localdate()
        laufend = _person(body, 1)
        _mitgliedschaft(laufend, rat, end_date=None)
        endet_heute = _person(body, 2)
        _mitgliedschaft(endet_heute, rat, end_date=heute)
        ausgeschieden = _person(body, 3)
        _mitgliedschaft(ausgeschieden, rat, end_date=heute - timedelta(days=1))
        ohne = _person(body, 4)
        nur_geloescht = _person(body, 5)
        _mitgliedschaft(nur_geloescht, rat, deleted=True)
        nur_aufgeloest = _person(body, 6)
        _mitgliedschaft(nur_aufgeloest, aufgeloest)

        grund = client.get("/sitemap-insight-koeln.xml").content.decode()

        assert f"/insight/personen/{laufend.pk}/" in grund
        assert f"/insight/personen/{endet_heute.pk}/" in grund
        for person in (ausgeschieden, ohne, nur_geloescht, nur_aufgeloest):
            assert f"/insight/personen/{person.pk}/" not in grund, person.name
        assert f"/insight/gremien/{rat.pk}/" in grund

    def test_personen_ohne_n_plus_1(self, client: Client) -> None:
        body = _kommune(1, "Stadt Köln", slug="koeln")
        rat = _gremium(body, 1)
        _mitgliedschaft(_person(body, 0), rat)
        client.get("/sitemap-insight-koeln.xml")
        with CaptureQueriesContext(connection) as wenige:
            client.get("/sitemap-insight-koeln.xml")
        for nummer in range(1, 15):
            _mitgliedschaft(_person(body, nummer), rat)
        with CaptureQueriesContext(connection) as viele:
            antwort = client.get("/sitemap-insight-koeln.xml")
        assert antwort.content.decode().count("/insight/personen/") == 15
        assert len(viele) == len(wenige)

    def test_mit_laufender_mitgliedschaft_eine_abfrage(self) -> None:
        body = _kommune(1, "Stadt Köln", slug="koeln")
        rat = _gremium(body, 1)
        for nummer in range(5):
            _mitgliedschaft(_person(body, nummer), rat, end_date=HEUTE if nummer % 2 else HEUTE - timedelta(days=1))
        with CaptureQueriesContext(connection) as erfasst:
            namen = sorted(
                indexierung.mit_laufender_mitgliedschaft(OParlPerson.objects.all(), HEUTE).values_list(
                    "name", flat=True
                )
            )
        assert namen == ["Person 1", "Person 3"]
        assert len(erfasst) == 1


# =============================================================================
# Personen ohne laufende Mitgliedschaft
# =============================================================================


class TestPersonenseite:
    @pytest.fixture
    def rat(self) -> OParlOrganization:
        return _gremium(_kommune(1, "Stadt Köln", slug="koeln"), 1, name="Rat")

    def _seite(self, client: Client, person: OParlPerson) -> str:
        antwort = client.get(reverse("insight_core:insight:person_detail", args=[person.pk]))
        assert antwort.status_code == 200, "die Seite bleibt erreichbar"
        return antwort.content.decode()

    def test_mit_laufender_mitgliedschaft_im_index(self, client: Client, rat: OParlOrganization) -> None:
        person = _person(rat.body, 1)
        _mitgliedschaft(person, rat, role="Ratsmitglied")
        assert _robots(self._seite(client, person)) == "index, follow"

    def test_ausgeschieden_noindex(self, client: Client, rat: OParlOrganization) -> None:
        person = _person(rat.body, 1)
        _mitgliedschaft(person, rat, end_date=timezone.localdate() - timedelta(days=1))
        html = self._seite(client, person)
        assert _robots(html) == "noindex, follow"
        assert _canonical(html) == f"{settings.SITE_URL}/insight/personen/{person.pk}/"

    def test_ohne_mitgliedschaft_noindex(self, client: Client, rat: OParlOrganization) -> None:
        assert _robots(self._seite(client, _person(rat.body, 1))) == "noindex, follow"

    @pytest.mark.parametrize("mit_mitgliedschaft", [True, False])
    def test_keine_zusaetzliche_abfrage(
        self, client: Client, rat: OParlOrganization, monkeypatch: pytest.MonkeyPatch, mit_mitgliedschaft: bool
    ) -> None:
        person = _person(rat.body, 1)
        if mit_mitgliedschaft:
            _mitgliedschaft(person, rat)
        self._seite(client, person)
        with CaptureQueriesContext(connection) as mit_robots:
            self._seite(client, person)
        # Gegenprobe ohne die robots-Entscheidung: Sie nutzt die schon geladenen Mitgliedschaften
        monkeypatch.setattr(indexierung, "robots_fuer_person", lambda seo, mitgliedschaften: seo)
        with CaptureQueriesContext(connection) as ohne_robots:
            self._seite(client, person)
        assert len(mit_robots) == len(ohne_robots)


# =============================================================================
# noindex: Suche, Listen, Teilansichten, Dateien
# =============================================================================


@pytest.fixture
def gewaehlt(client: Client) -> OParlBody:
    body = _kommune(1, "Stadt Köln", display_name="Köln", slug="koeln")
    _kommune(2, "Stadt Bonn", display_name="Bonn", slug="bonn")
    client.get(reverse("insight_core:insight:set_body", args=[body.id]))
    return body


@pytest.fixture
def _ohne_elasticsearch(monkeypatch: pytest.MonkeyPatch) -> None:
    def _nicht_erreichbar() -> Any:
        raise ConnectionError("kein Suchdienst im Test")

    monkeypatch.setattr("insight_core.services.search_service.get_search_service", _nicht_erreichbar)


@pytest.mark.usefixtures("_ohne_elasticsearch")
class TestSuche:
    def test_suchseite_immer_noindex(self, client: Client, gewaehlt: OParlBody) -> None:
        for parameter in ({}, {"q": "Radweg"}):
            antwort = client.get("/insight/suche/", parameter)
            assert antwort.status_code == 200
            assert _robots(antwort.content.decode()) == "noindex, follow", parameter
            assert antwort["X-Robots-Tag"] == "noindex"

    def test_htmx_ausschnitt_der_suche(self, client: Client, gewaehlt: OParlBody) -> None:
        antwort = client.get("/insight/suche/", {"q": "Radweg"}, headers={"HX-Request": "true"})
        assert antwort.status_code == 200 and antwort["X-Robots-Tag"] == "noindex"

    def test_ergebnis_teilansicht(self, client: Client, gewaehlt: OParlBody) -> None:
        antwort = client.get("/insight/suche/partials/results/", {"q": "Radweg"})
        assert antwort.status_code == 200 and antwort["X-Robots-Tag"] == "noindex"


class TestListen:
    LISTEN = (
        "/insight/vorgaenge/",
        "/insight/termine/",
        "/insight/gremien/",
        "/insight/personen/",
        "/insight/termine/jahresplan/",
        "/insight/termine/kalender/",
        "/insight/karte/",
        "/insight/nachbarschaft/",
    )

    @pytest.mark.parametrize("pfad", LISTEN)
    def test_ohne_parameter_bleibt_die_liste_wie_sie_ist(self, client: Client, gewaehlt: OParlBody, pfad: str) -> None:
        antwort = client.get(pfad)
        assert antwort.status_code == 200
        html = antwort.content.decode()
        assert _robots(html) == "index, follow"
        assert _canonical(html) == f"{settings.SITE_URL}{pfad}"
        assert "X-Robots-Tag" not in antwort

    @pytest.mark.parametrize("pfad", LISTEN)
    @pytest.mark.parametrize(
        "parameter", [{"page": "1"}, {"q": "Radweg"}, {"type": "Antrag"}, {"period": "past"}, {"sort": "name"}]
    )
    def test_mit_filter_seite_oder_sortierung_noindex(
        self, client: Client, gewaehlt: OParlBody, pfad: str, parameter: dict[str, str]
    ) -> None:
        antwort = client.get(pfad, parameter)
        assert antwort.status_code == 200
        html = antwort.content.decode()
        assert _robots(html) == "noindex, follow"
        assert _canonical(html) == f"{settings.SITE_URL}{pfad}", "Canonical unverändert"

    def test_weiterleitung_ohne_kommune_per_kopf(self, gewaehlt: OParlBody) -> None:
        """Ohne gewählte Kommune leiten Listen zur Auswahl weiter; mit Parametern trägt die Weiterleitung noindex."""
        antwort = Client().get("/insight/vorgaenge/", {"page": "2"})
        assert antwort.status_code == 302
        assert antwort["X-Robots-Tag"] == "noindex"
        assert "X-Robots-Tag" not in Client().get("/insight/vorgaenge/")

    def test_kampagnen_kennungen_zaehlen_nicht(self, client: Client, gewaehlt: OParlBody) -> None:
        antwort = client.get("/insight/vorgaenge/", {"utm_source": "newsletter", "fbclid": "x"})
        assert _robots(antwort.content.decode()) == "index, follow"

    def test_htmx_ausschnitt_per_kopf(self, client: Client, gewaehlt: OParlBody) -> None:
        _vorgang(gewaehlt, 1)
        antwort = client.get("/insight/vorgaenge/", {"page": "1"}, headers={"HX-Request": "true"})
        assert antwort.status_code == 200 and antwort["X-Robots-Tag"] == "noindex"

    def test_kommunenauswahl_und_merkliste_bleiben_noindex(self, client: Client, gewaehlt: OParlBody) -> None:
        assert _robots(client.get("/insight/kommunen/").content.decode()) == "noindex, follow"
        assert _robots(client.get("/insight/gespeichert/").content.decode()) == "noindex, follow"


class TestTeilansichtenUndDateien:
    def test_teilansichten_per_kopf(self, client: Client, gewaehlt: OParlBody) -> None:
        vorgang = _vorgang(gewaehlt, 1)
        for pfad in (
            f"/insight/vorgaenge/{vorgang.pk}/zusammenfassung/",
            "/insight/termine/partials/calendar-events/",
            "/insight/termine/kalender.ics",
            "/insight/karte/partials/markers/",
            "/insight/nachbarschaft/autocomplete/?q=H",
            "/insight/nachbarschaft/partials/results/",
            "/insight/kommunen/vorschlaege/?q=K",
            "/insight/kommunen/stoebern/",
            "/insight/merkliste/api/ids/",
        ):
            antwort = client.get(pfad)
            assert antwort.status_code == 200, pfad
            assert antwort["X-Robots-Tag"] == "noindex", pfad

    def test_detailseiten_ohne_kopf(self, client: Client, gewaehlt: OParlBody) -> None:
        vorgang = _vorgang(gewaehlt, 1)
        antwort = client.get(f"/insight/vorgaenge/{vorgang.pk}/")
        assert antwort.status_code == 200 and "X-Robots-Tag" not in antwort
        assert _robots(antwort.content.decode()) == "index, follow"

    @pytest.mark.parametrize("download", [False, True])
    def test_dateivorschau_und_download(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, download: bool) -> None:
        def antwort_der_quelle(self: Any, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"%PDF-1.4 klein", headers={"content-type": "application/pdf"})

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", antwort_der_quelle)
        monkeypatch.setattr(safe_fetch, "_resolve", lambda host: ["93.184.215.14"])
        monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
        source = OParlSource.objects.create(name="Fremd-RIS", url="https://ris.fremd.example/oparl/system")
        body = OParlBody.objects.create(
            external_id="https://ris.fremd.example/oparl/body/1", source=source, name="Beispielstadt"
        )
        datei = OParlFile.objects.create(
            external_id="https://ris.fremd.example/oparl/file/1",
            body=body,
            name="Vorlage",
            file_name="vorlage.pdf",
            mime_type="application/pdf",
            download_url="https://ris.fremd.example/files/vorlage.pdf",
        )

        antwort = Client().get(f"/insight/dokumente/{datei.id}/preview/" + ("?download=1" if download else ""))

        assert antwort.status_code == 200
        assert antwort["Content-Type"].startswith("application/pdf")
        assert antwort["X-Robots-Tag"] == "noindex"


# =============================================================================
# Beschlussseiten: robots und Canonical
# =============================================================================


@pytest.fixture
def beschluss() -> Any:
    from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant

    tenant = SessionTenant.objects.create(
        name="Bezirk Nord",
        slug="nord",
        insight_publish=True,
        implementation_publish=True,
        oparl_public_since=timezone.now(),
    )
    source = OParlSource.objects.get(sync_config__session_tenant="nord")
    body = OParlBody.objects.create(external_id=f"{source.url}body/", source=source, name="Bezirk Nord", slug="nord")
    tenant.oparl_body = body
    tenant.save(update_fields=["oparl_body"])
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant,
        name="Bauausschuss",
        organization=gremium,
        start=timezone.now() - timedelta(days=3),
        is_public=True,
    )
    return SessionAgendaItem.objects.create(
        meeting=sitzung,
        number="1",
        order=1,
        name="Radweg beschlossen",
        vote_result="approved",
        implementation_public=True,
    )


class TestBeschluesse:
    def test_liste_mit_robots_und_canonical(self, client: Client, beschluss: Any) -> None:
        client.get(reverse("insight_core:insight:set_body", args=[beschluss.meeting.tenant.oparl_body_id]))
        html = client.get("/insight/beschluesse/").content.decode()
        assert _robots(html) == "index, follow"
        assert _canonical(html) == f"{settings.SITE_URL}/insight/beschluesse/"

        gefiltert = client.get("/insight/beschluesse/", {"status": "open"}).content.decode()
        assert _robots(gefiltert) == "noindex, follow"
        assert _canonical(gefiltert) == f"{settings.SITE_URL}/insight/beschluesse/"

    def test_detail_mit_robots_und_canonical(self, client: Client, beschluss: Any) -> None:
        antwort = client.get(f"/insight/beschluesse/{beschluss.pk}/")
        assert antwort.status_code == 200
        html = antwort.content.decode()
        assert _robots(html) == "index, follow"
        assert _canonical(html) == f"{settings.SITE_URL}/insight/beschluesse/{beschluss.pk}/"
        assert "Radweg beschlossen" in html


# =============================================================================
# Crawlbare Wege: Brotkrumen, nofollow, Stadtseiten auf /insight/
# =============================================================================


def _brotkrumen(html: str) -> str:
    treffer = re.search(r'<nav aria-label="Brotkrumen"[^>]*>(.*?)</nav>', html, re.S)
    assert treffer, "Brotkrumen in der Kopfzeile fehlen"
    return re.sub(r"\s+", " ", treffer.group(1))


def _link(html: str, text: str) -> str:
    """Öffnendes ``<a …>`` des Links mit diesem Text."""
    treffer = re.search(r"<a ([^>]*)>" + re.escape(text) + "</a>", html)
    assert treffer, text
    return treffer.group(1)


class TestBrotkrumen:
    def test_kommune_mit_slug_fuehrt_ohne_weiterleitung_auf_die_stadtseite(self, client: Client) -> None:
        body = _kommune(1, "Stadt Köln", display_name="Köln", slug="koeln")
        _kommune(2, "Stadt Bonn", slug="bonn")
        vorgang = _vorgang(body, 1)

        # Wie eine Suchmaschine: ohne Sitzung direkt auf den Vorgang
        krumen = _brotkrumen(client.get(f"/insight/vorgaenge/{vorgang.pk}/").content.decode())
        kommune = _link(krumen, "Köln")
        assert 'href="/insight/k/koeln/"' in kommune and "nofollow" not in kommune
        bereich = _link(krumen, "Vorgänge")
        waehlen = reverse("insight_core:insight:set_body", args=[body.id])
        assert f'href="{waehlen}?weiter=/insight/vorgaenge/"' in bereich and 'rel="nofollow"' in bereich

        stadtseite = Client().get("/insight/k/koeln/")
        assert stadtseite.status_code == 200, "kein Umweg: die Stadtseite antwortet direkt"

    @pytest.mark.parametrize("felder", [{}, {"slug": "pilot", "is_listed": False}])
    def test_ohne_stadtseite_bleibt_die_wahl_mit_nofollow(self, client: Client, felder: dict[str, Any]) -> None:
        body = _kommune(1, "Pilotstadt", **felder)
        _kommune(2, "Stadt Bonn", slug="bonn")
        sitzung = _sitzung(body, 1, start=timezone.now())

        html = client.get(f"/insight/termine/{sitzung.pk}/").content.decode()
        kommune = _link(_brotkrumen(html), "Pilotstadt")
        waehlen = reverse("insight_core:insight:set_body", args=[body.id])
        assert f'href="{waehlen}?weiter=/insight/"' in kommune and 'rel="nofollow"' in kommune
        assert "/insight/k/" not in _brotkrumen(html)

    def test_weg_zurueck_am_handy_mit_nofollow(self, client: Client) -> None:
        body = _kommune(1, "Stadt Köln", display_name="Köln", slug="koeln")
        _kommune(2, "Stadt Bonn", slug="bonn")
        vorgang = _vorgang(body, 1)
        html = client.get(f"/insight/vorgaenge/{vorgang.pk}/").content.decode()
        zurueck = re.search(r'<nav aria-label="Zurück"[^>]*>\s*<a ([^>]*)>', html)
        assert zurueck and "weiter=/insight/vorgaenge/" in zurueck.group(1) and 'rel="nofollow"' in zurueck.group(1)

    def test_eigene_kommune_ohne_nofollow(self, client: Client) -> None:
        body = _kommune(1, "Stadt Köln", display_name="Köln", slug="koeln")
        _kommune(2, "Stadt Bonn", slug="bonn")
        vorgang = _vorgang(body, 1)
        client.get(reverse("insight_core:insight:set_body", args=[body.id]))
        krumen = _brotkrumen(client.get(f"/insight/vorgaenge/{vorgang.pk}/").content.decode())
        assert 'href="/insight/"' in krumen and 'href="/insight/vorgaenge/"' in krumen
        assert "nofollow" not in krumen


class TestStadtseitenAufDerAuswahl:
    def _zeile(self, html: str) -> str:
        treffer = re.search(r"Direkt zu den Ratsinformationen:(.*?)</p>", html, re.S)
        assert treffer, "Textzeile mit den Stadtseiten fehlt"
        return treffer.group(1)

    def test_alphabetisch_nur_gelistete_mit_slug(self, client: Client) -> None:
        _kommune(1, "Stadt Münster", display_name="Münster", slug="muenster")
        _kommune(2, "Stadt Köln", display_name="Köln", slug="koeln")
        _kommune(3, "Bundesstadt Bonn", display_name="Bonn", slug="bonn")
        _kommune(4, "Stadt Krefeld", display_name="Krefeld", slug="krefeld")
        _kommune(5, "Stadt Darmstadt")  # ohne Slug
        _kommune(6, "Pilotstadt", slug="pilot", is_listed=False)
        abgeschaltet = _kommune(7, "Stadt Abgeschaltet", slug="abgeschaltet")
        publication.set_source_state(abgeschaltet.source, publication.PAUSED)

        antwort = client.get("/insight/")
        assert antwort.status_code == 200
        zeile = self._zeile(antwort.content.decode())

        namen = re.findall(r'<a href="(/insight/k/[a-z-]+/)"[^>]*>([^<]+)</a>', zeile)
        assert namen == [
            ("/insight/k/bonn/", "Bonn"),
            ("/insight/k/koeln/", "Köln"),
            ("/insight/k/krefeld/", "Krefeld"),
            ("/insight/k/muenster/", "Münster"),
        ]
        assert "nofollow" not in zeile
        for pfad, _name in namen:
            assert Client().get(pfad).status_code == 200, pfad

    def test_hoechstens_zwoelf(self, client: Client) -> None:
        for nummer in range(15):
            _kommune(nummer, f"Stadt {nummer:02d}", slug=f"stadt-{nummer:02d}")
        zeile = self._zeile(client.get("/insight/").content.decode())
        assert zeile.count("<a ") == indexierung.STADTSEITEN_HOECHSTENS == 12
        assert "Stadt 11" in zeile and "Stadt 12" not in zeile

    def test_ohne_kommunen_mit_slug_keine_zeile(self, client: Client) -> None:
        _kommune(1, "Stadt Darmstadt")
        _kommune(2, "Stadt Erbach")
        assert "Direkt zu den Ratsinformationen" not in client.get("/insight/").content.decode()


def test_sortierung_nach_din_5007() -> None:
    namen = ["Münster", "Mülheim an der Ruhr", "Köln", "Krefeld", "Aachen", "Österreich", "Offenbach"]
    sortiert = sorted(namen, key=indexierung._sortierschluessel)
    assert sortiert == ["Aachen", "Köln", "Krefeld", "Mülheim an der Ruhr", "Münster", "Offenbach", "Österreich"]


def test_stadtseite_url() -> None:
    assert indexierung.stadtseite_url(None) == ""
    body = _kommune(1, "Stadt Köln", slug="koeln")
    assert indexierung.stadtseite_url(body) == "/insight/k/koeln/"
    body.is_listed = False
    assert indexierung.stadtseite_url(body) == ""
    assert indexierung.stadtseite_url(cast(Any, _kommune(2, "Stadt Darmstadt"))) == ""
