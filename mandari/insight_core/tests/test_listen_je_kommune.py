# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Listen je Kommune mit eigener Adresse (Issue #939): ``/insight/k/<slug>/termine/``, ``…/vorgaenge/``, ``…/gremien/``
und ``…/personen/``.

Suchmaschinen haben keine Sitzung; die Listen im gemeinsamen Portal leiteten sie zur Kommunenauswahl um, und
``/insight/k/<slug>/vorgaenge/`` leitete dorthin weiter. Die Listen je Kommune antworten ohne Cookie direkt mit 200,
schreiben nichts in die Sitzung, blättern mit echten Links (Seite 1 im Index, weitere Seiten ``noindex, follow``),
sind kanonisch, stehen in der Grund-Sitemap und sind von Stadtseite, Navigation und Brotkrumen aus verlinkt.
Nicht gelistete, abgeschaltete und zurückgenommene Kommunen antworten wie die Stadtseite.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from insight_core import publication
from insight_core.models import (
    OParlBody,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)
from insight_core.views import _helpers

pytestmark = pytest.mark.django_db

HOST = "rat.nord.example"
#: Adresse je Kommune → Eintrag im Bestand (``_bestand``), dessen Detailseite die Liste verlinkt
LISTEN = {"termine": "sitzung", "vorgaenge": "vorgang", "gremien": "gremium", "personen": "person"}


def _kommune(nummer: int, name: str, slug: str, **felder: Any) -> OParlBody:
    source = OParlSource.objects.create(name=f"RIS {name}", url=f"https://ris{nummer}.example.org/system")
    return OParlBody.objects.create(
        external_id=f"https://ris{nummer}.example.org/body/1", source=source, name=name, slug=slug, **felder
    )


def _bestand(body: OParlBody, kuerzel: str, wort: str) -> dict[str, Any]:
    """Je Liste ein Eintrag, der in der Voreinstellung der Liste erscheint (kommende Sitzung, aktives Gremium)."""
    basis = body.external_id
    gremium = OParlOrganization.objects.create(external_id=f"{basis}/org/1", body=body, name=f"Rat {wort}")
    sitzung = OParlMeeting.objects.create(
        external_id=f"{basis}/meeting/1",
        body=body,
        name=f"Ratssitzung {wort}",
        start=timezone.now() + timedelta(days=7),
    )
    sitzung.organizations.add(gremium)
    vorgang = OParlPaper.objects.create(external_id=f"{basis}/paper/1", body=body, name=f"Radweg {wort}")
    familie = "Nordmann" if kuerzel == "nord" else "Südmann"
    vorname = "Anna" if kuerzel == "nord" else "Bernd"
    person = OParlPerson.objects.create(
        external_id=f"{basis}/person/1",
        body=body,
        name=f"{vorname} {familie}",
        given_name=vorname,
        family_name=familie,
    )
    OParlMembership.objects.create(external_id=f"{basis}/membership/1", person=person, organization=gremium)
    return {"gremium": gremium, "sitzung": sitzung, "vorgang": vorgang, "person": person}


@pytest.fixture
def kommunen(db: Any) -> dict[str, Any]:
    nord = _kommune(1, "Bezirk Musterstadt-Nord", "nord", display_name="Musterstadt-Nord")
    sued = _kommune(2, "Bezirk Süd", "sued")
    return {
        "nord": nord,
        "sued": sued,
        "bestand": _bestand(nord, "nord", "Nord"),
        "sued_bestand": _bestand(sued, "sued", "Süd"),
    }


def _robots(html: str) -> str | None:
    treffer = re.search(r'<meta name="robots" content="([^"]*)"', html)
    return treffer.group(1) if treffer else None


def _canonical(html: str) -> str | None:
    treffer = re.search(r'<link rel="canonical" href="([^"]*)"', html)
    return treffer.group(1) if treffer else None


def _json_ld(html: str) -> list[dict[str, Any]]:
    return [json.loads(t) for t in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def _bereiche(html: str) -> str:
    treffer = re.search(r'<nav[^>]*aria-label="Bereiche"[^>]*>(.*?)</nav>', html, re.S)
    assert treffer, "Navigation der Bereiche fehlt"
    return treffer.group(1)


def _vorgaenge(body: OParlBody, anzahl: int) -> None:
    OParlPaper.objects.bulk_create(
        OParlPaper(external_id=f"{body.external_id}/paper/viele/{nummer}", body=body, name=f"Vorgang {nummer:03d}")
        for nummer in range(anzahl)
    )


# =============================================================================
# Ohne Sitzung direkt erreichbar
# =============================================================================


class TestOhneSitzung:
    @pytest.mark.parametrize("liste", LISTEN)
    def test_direkt_200_mit_eintraegen_der_kommune(self, kommunen: dict[str, Any], liste: str) -> None:
        eigener = kommunen["bestand"][LISTEN[liste]].pk
        fremder = kommunen["sued_bestand"][LISTEN[liste]].pk

        antwort = Client().get(f"/insight/k/nord/{liste}/")

        assert antwort.status_code == 200
        html = antwort.content.decode()
        assert f'href="/insight/{liste}/{eigener}/"' in html, "Link auf die Detailseite"
        assert str(fremder) not in html
        assert antwort.context["insight_portal"].body == kommunen["nord"]
        assert antwort.context["active_body"] == kommunen["nord"]

    @pytest.mark.parametrize("liste", LISTEN)
    def test_keine_sitzung_fuer_suchmaschinen(self, kommunen: dict[str, Any], liste: str) -> None:
        client = Client(HTTP_USER_AGENT="Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)")
        # Gezählt wird der Zuwachs: Tests mit eigenen Verbindungen (Fäden) können festgeschriebene Sitzungen
        # hinterlassen, die je nach Aufteilung der CI vor diesem Test in derselben Datenbank stehen
        vorher = Session.objects.count()

        for pfad in (f"/insight/k/nord/{liste}/", f"/insight/k/nord/{liste}/?page=1"):
            antwort = client.get(pfad)
            assert antwort.status_code == 200, pfad
            assert settings.SESSION_COOKIE_NAME not in antwort.cookies, pfad
        assert Session.objects.count() == vorher

    def test_htmx_ausschnitt_ohne_sitzung(self, kommunen: dict[str, Any]) -> None:
        vorher = Session.objects.count()
        antwort = Client().get("/insight/k/nord/vorgaenge/", {"q": "Radweg"}, headers={"HX-Request": "true"})

        assert antwort.status_code == 200 and antwort["X-Robots-Tag"] == "noindex"
        html = antwort.content.decode()
        assert "Radweg Nord" in html and "Radweg Süd" not in html and "<html" not in html
        assert Session.objects.count() == vorher

    @pytest.mark.parametrize("liste", LISTEN)
    def test_filter_bleiben_auf_der_adresse(self, kommunen: dict[str, Any], liste: str) -> None:
        html = Client().get(f"/insight/k/nord/{liste}/").content.decode()

        assert f'hx-get="/insight/k/nord/{liste}/"' in html
        assert f'hx-get="/insight/{liste}/"' not in html

    def test_sitzung_einer_anderen_kommune_bleibt_unberuehrt(self, kommunen: dict[str, Any]) -> None:
        client = Client()
        client.get("/insight/k/sued/")

        antwort = client.get("/insight/k/nord/vorgaenge/")

        assert "Radweg Nord" in antwort.content.decode()
        assert client.get("/insight/vorgaenge/").context["insight_portal"].body == kommunen["sued"]


# =============================================================================
# Blättern, robots und Canonical
# =============================================================================


class TestBlaettern:
    def test_echte_seitenlinks_und_robots(self, kommunen: dict[str, Any]) -> None:
        _vorgaenge(kommunen["nord"], 30)

        erste = Client().get("/insight/k/nord/vorgaenge/").content.decode()
        zweite = Client().get("/insight/k/nord/vorgaenge/?page=2")

        assert 'href="?page=2"' in erste
        assert _robots(erste) == "index, follow"
        assert zweite.status_code == 200
        html = zweite.content.decode()
        # Eigene kanonische Adresse: Auf Seite 1 kanonisiert, folgt Google den Links auf Dauer nicht mehr (Issue #1145)
        assert _robots(html) == "index, follow"
        assert _canonical(html) == f"{settings.SITE_URL}/insight/k/nord/vorgaenge/?page=2"
        assert "<title>Vorgänge – Seite 2 |" in html
        assert 'href="?page=1"' in html
        assert Client().get("/insight/k/nord/vorgaenge/?page=3").status_code == 404

    @pytest.mark.parametrize(
        ("anfrage", "robots", "kanonisch"),
        [
            ("vorgaenge/?page=1", "index, follow", "vorgaenge/"),
            ("vorgaenge/?page=2&utm_source=x", "index, follow", "vorgaenge/?page=2"),
            ("vorgaenge/?page=last", "noindex, follow", "vorgaenge/"),
            ("vorgaenge/?page=2&q=Vorgang", "noindex, follow", "vorgaenge/"),
            ("vorgaenge/?period=past", "noindex, follow", "vorgaenge/"),
            ("termine/?period=past", "index, follow", "termine/?period=past"),
            ("termine/?page=2&period=past", "index, follow", "termine/?period=past&page=2"),
            ("termine/?period=all", "noindex, follow", "termine/"),
        ],
    )
    def test_blaetteransichten(self, kommunen: dict[str, Any], anfrage: str, robots: str, kanonisch: str) -> None:
        _vorgaenge(kommunen["nord"], 30)
        for nummer in range(30):
            OParlMeeting.objects.create(
                external_id=f"{kommunen['nord'].external_id}/meeting/alt/{nummer}",
                body=kommunen["nord"],
                name=f"Alte Sitzung {nummer}",
                start=timezone.now() - timedelta(days=nummer + 1),
            )

        html = Client().get(f"/insight/k/nord/{anfrage}").content.decode()

        assert _robots(html) == robots
        assert (_canonical(html) or "").replace("&amp;", "&") == f"{settings.SITE_URL}/insight/k/nord/{kanonisch}"

    def test_vergangene_sitzungen_haben_einen_eigenen_titel(self, kommunen: dict[str, Any]) -> None:
        html = Client().get("/insight/k/nord/termine/?period=past").content.decode()
        assert "<title>Vergangene Sitzungen |" in html

    def test_weitere_seite_im_gemeinsamen_portal_bleibt_noindex(self, kommunen: dict[str, Any]) -> None:
        _vorgaenge(kommunen["nord"], 30)
        client = Client()
        client.get(reverse("insight_core:insight:set_body", args=[kommunen["nord"].id]))

        html = client.get("/insight/vorgaenge/?page=2").content.decode()

        assert _robots(html) == "noindex, follow"
        assert _canonical(html) == f"{settings.SITE_URL}/insight/k/nord/vorgaenge/"

    def test_hoechstens_so_viele_seiten(self, kommunen: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_helpers, "LISTEN_SEITEN_HOECHSTENS", 1)
        _vorgaenge(kommunen["nord"], 30)

        erste = Client().get("/insight/k/nord/vorgaenge/").content.decode()

        assert "?page=2" not in erste
        assert Client().get("/insight/k/nord/vorgaenge/?page=2").status_code == 404
        # Gilt auch für die Liste im gemeinsamen Portal
        client = Client()
        client.get(reverse("insight_core:insight:set_body", args=[kommunen["nord"].id]))
        assert client.get("/insight/vorgaenge/?page=2").status_code == 404

    @pytest.mark.parametrize("liste", LISTEN)
    def test_canonical_und_brotkrumen(self, kommunen: dict[str, Any], liste: str) -> None:
        html = Client().get(f"/insight/k/nord/{liste}/").content.decode()

        adresse = f"{settings.SITE_URL}/insight/k/nord/{liste}/"
        assert _canonical(html) == adresse
        assert _robots(html) == "index, follow"
        krumen = _json_ld(html)[-1]
        assert krumen["@type"] == "BreadcrumbList"
        pfade = [re.sub(r"^https?://[^/]+", "", e["item"]) for e in krumen["itemListElement"]]
        assert pfade == ["/insight/", "/insight/k/nord/", f"/insight/k/nord/{liste}/"]
        # Sichtbare Brotkrume: Bereich ohne Link, mit aria-current
        assert re.search(r'<span aria-current="page"[^>]*>\s*(Sitzungen|Vorgänge|Gremien|Personen)\s*</span>', html)

    def test_liste_im_gemeinsamen_portal_verweist_auf_die_adresse_je_kommune(self, kommunen: dict[str, Any]) -> None:
        client = Client()
        client.get(reverse("insight_core:insight:set_body", args=[kommunen["nord"].id]))

        html = client.get("/insight/vorgaenge/").content.decode()

        assert _canonical(html) == f"{settings.SITE_URL}/insight/k/nord/vorgaenge/"
        assert 'hx-get="/insight/vorgaenge/"' in html, "für Nutzer bleibt die Liste, wie sie ist"

    def test_ohne_abfragen_je_eintrag(self, kommunen: dict[str, Any]) -> None:
        client = Client()
        client.get("/insight/k/nord/vorgaenge/")  # Aufwärmen (Cache für Slug und Branding)
        with CaptureQueriesContext(connection) as wenige:
            client.get("/insight/k/nord/vorgaenge/")
        _vorgaenge(kommunen["nord"], 24)
        with CaptureQueriesContext(connection) as viele:
            antwort = client.get("/insight/k/nord/vorgaenge/")

        assert antwort.content.decode().count("Vorgang 0") == 24
        assert len(viele) == len(wenige), "keine Abfrage je Eintrag"
        # Kommune, Anzahl (nur bis zur Grenze), Vorgänge, deren Stand, Beschlüsse in der Navigation, Typen im Filter
        assert len(viele) <= 8, [q["sql"] for q in viele.captured_queries]
        zaehlung = [q["sql"] for q in viele.captured_queries if "COUNT(" in q["sql"]]
        assert zaehlung and all("LIMIT" in sql for sql in zaehlung), "gezählt wird nur bis zur Grenze der Seiten"


# =============================================================================
# Verlinkung: Stadtseite, Navigation, Brotkrumen, Sitemap
# =============================================================================


class TestVerlinkung:
    def test_stadtseite_verlinkt_alle_listen(self, kommunen: dict[str, Any]) -> None:
        html = Client().get("/insight/k/nord/").content.decode()

        for liste in LISTEN:
            assert f'href="/insight/k/nord/{liste}/"' in html, liste
        for alt in ('href="/insight/termine/"', 'href="/insight/vorgaenge/"'):
            assert alt not in html, "keine Umwege über die Kommunenauswahl"

    def test_navigation_einer_liste_ohne_sitzung(self, kommunen: dict[str, Any]) -> None:
        html = Client().get("/insight/k/nord/gremien/").content.decode()
        nav = _bereiche(html)

        for liste in LISTEN:
            assert f'href="/insight/k/nord/{liste}/"' in nav, liste
            assert f'href="/insight/k/nord/{liste}/" rel=' not in html, f"{liste}: Suchmaschinen folgen"
        # Bereiche ohne eigene Adresse führen über den Einstieg, der den Kontext beim Klick festhält und dabei eine
        # Sitzung anlegt: nofollow, Suchmaschinen rufen ihn nicht auf
        assert 'href="/insight/k/nord/karte/" rel="nofollow"' in nav
        assert 'href="/insight/k/nord/chat/" rel="nofollow"' in nav
        assert html.count('href="/insight/k/nord/karte/" rel="nofollow"') == 2, "Seitenleiste und Leiste unten"
        assert 'href="/insight/k/nord/gespeichert/" rel="nofollow"' in html
        assert re.search(r'href="/insight/k/nord/karte/"(?! rel="nofollow")', html) is None
        assert 'href="/insight/k/nord/"' in nav, "die Stadtseite bleibt follow"

    def test_kalender_der_sitzungsliste_mit_nofollow(self, kommunen: dict[str, Any]) -> None:
        html = Client().get("/insight/k/nord/termine/").content.decode()

        assert '<a href="/insight/k/nord/termine/kalender/" rel="nofollow"' in html

    def test_ohne_nofollow_wenn_die_sitzung_den_kontext_haelt(self, kommunen: dict[str, Any]) -> None:
        client = Client()
        client.get("/insight/k/nord/")

        html = client.get("/insight/k/nord/gremien/").content.decode()

        assert 'href="/insight/karte/"' in html and "nofollow" not in _bereiche(html)
        assert '<a href="/insight/termine/kalender/" class=' in client.get("/insight/k/nord/termine/").content.decode()

    def test_einstieg_mit_pfad_haelt_den_kontext_fest(self, kommunen: dict[str, Any]) -> None:
        client = Client()

        antwort = client.get("/insight/k/nord/karte/")

        assert antwort.status_code == 302 and antwort["Location"] == "/insight/karte/"
        assert client.get("/insight/termine/").context["active_body"] == kommunen["nord"]

    def test_navigation_im_gemeinsamen_portal_unveraendert(self, kommunen: dict[str, Any]) -> None:
        client = Client()
        client.get(reverse("insight_core:insight:set_body", args=[kommunen["nord"].id]))

        nav = _bereiche(client.get("/insight/vorgaenge/").content.decode())

        assert 'href="/insight/vorgaenge/"' in nav and 'href="/insight/karte/"' in nav
        assert "/insight/k/" not in nav

    def test_brotkrume_der_detailseite_fuehrt_ohne_umweg_auf_die_liste(self, kommunen: dict[str, Any]) -> None:
        vorgang = kommunen["bestand"]["vorgang"]

        html = Client().get(f"/insight/vorgaenge/{vorgang.pk}/").content.decode()

        krumen = re.search(r'<nav aria-label="Brotkrumen"[^>]*>(.*?)</nav>', html, re.S)
        assert krumen
        link = re.search(r"<a ([^>]*)>\s*Vorgänge\s*</a>", krumen.group(1))
        assert link and 'href="/insight/k/nord/vorgaenge/"' in link.group(1) and "nofollow" not in link.group(1)
        zurueck = re.search(r'<nav aria-label="Zurück"[^>]*>\s*<a ([^>]*)>', html)
        assert zurueck and 'href="/insight/k/nord/vorgaenge/"' in zurueck.group(1)
        assert "nofollow" not in zurueck.group(1)
        daten = _json_ld(html)[-1]
        pfade = [re.sub(r"^https?://[^/]+", "", e["item"]) for e in daten["itemListElement"]]
        assert pfade == [
            "/insight/",
            "/insight/k/nord/",
            "/insight/k/nord/vorgaenge/",
            f"/insight/vorgaenge/{vorgang.pk}/",
        ]

    def test_grund_sitemap_enthaelt_die_listen(self, kommunen: dict[str, Any]) -> None:
        sitemap = Client().get("/sitemap-insight-nord.xml").content.decode()

        for liste in LISTEN:
            assert f"<loc>{settings.SITE_URL}/insight/k/nord/{liste}/</loc>" in sitemap, liste
        assert "/insight/k/sued/" not in sitemap


# =============================================================================
# Nicht gelistet, abgeschaltet, zurückgenommen, Archiv, eigener Host
# =============================================================================


class TestStand:
    @pytest.mark.parametrize("liste", LISTEN)
    def test_nicht_gelistet_oder_unbekannt_404(self, kommunen: dict[str, Any], liste: str) -> None:
        OParlBody.objects.filter(pk=kommunen["sued"].pk).update(is_listed=False)

        assert Client().get(f"/insight/k/sued/{liste}/").status_code == 404
        assert Client().get(f"/insight/k/gibt-es-nicht/{liste}/").status_code == 404

    def test_abgeschaltet_503(self, kommunen: dict[str, Any]) -> None:
        publication.set_source_state(kommunen["nord"].source, publication.PAUSED)

        antwort = Client().get("/insight/k/nord/vorgaenge/")

        assert antwort.status_code == 503 and antwort["Retry-After"]
        assert "Radweg Nord" not in antwort.content.decode()

    def test_zurueckgenommen_410(self, kommunen: dict[str, Any]) -> None:
        publication.set_source_state(kommunen["nord"].source, publication.WITHDRAWN)
        OParlBody.objects.filter(pk=kommunen["nord"].pk).update(is_listed=False)

        assert Client().get("/insight/k/nord/termine/").status_code == 410

    def test_archiv_lesbar_mit_hinweis(self, kommunen: dict[str, Any]) -> None:
        publication.set_source_state(kommunen["nord"].source, publication.ARCHIVED)

        antwort = Client().get("/insight/k/nord/vorgaenge/")

        assert antwort.status_code == 200
        assert antwort.context["active_body_archive"] is not None
        assert "Radweg Nord" in antwort.content.decode()

    def test_eigener_host(self, kommunen: dict[str, Any], settings: Any) -> None:
        settings.ALLOWED_HOSTS = ["testserver", HOST]
        settings.PORTAL_HOSTS = {HOST: "nord"}
        client = Client(HTTP_HOST=HOST)

        antwort = client.get("/insight/k/nord/vorgaenge/")

        assert antwort.status_code == 200 and antwort.context["insight_portal"].via_host
        assert client.get("/insight/k/sued/vorgaenge/").status_code == 404
