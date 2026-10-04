# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Navigation des Bürgerportals (Issue #783, Stufe 0): sieben Bereiche ohne Gruppen, Beschlüsse und
Ratsfragen nur mit Inhalten, kein „Neu“, kein Zähler, kein schwebender Chat-Knopf, aktiver Bereich mit
``aria-current``, Menü als Dialog, Suchfeld in der Kopfzeile, Fuß mit Datenstand und Pflichtlinks.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.test import Client, override_settings
from django.urls import get_resolver, reverse
from django.utils import timezone

from apps.session.models import SessionTenant
from insight_core.models import OParlBody, OParlMeeting, OParlSource
from insight_core.navigation import NAV_AREAS

pytestmark = pytest.mark.django_db

BEREICHE = ["Übersicht", "Sitzungen", "Vorgänge", "Gremien", "Personen", "Karte", "KI-Assistent"]


@pytest.fixture
def kommune(db: Any) -> OParlBody:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    return OParlBody.objects.create(
        external_id="https://ris.example.org/body/1",
        source=source,
        name="Stadt Beispielstadt",
        display_name="Beispielstadt",
        slug="beispielstadt",
        last_sync=timezone.now(),
    )


@pytest.fixture
def besucher(client: Client, kommune: OParlBody) -> Client:
    client.get(reverse("insight_core:insight:set_body", args=[kommune.id]))
    return client


def _seite(client: Client, name: str, *args: Any) -> str:
    antwort = client.get(reverse(f"insight_core:insight:{name}", args=args))
    assert antwort.status_code == 200
    return antwort.content.decode()


def _bereiche(html: str) -> str:
    treffer = re.search(r'<nav[^>]*aria-label="Bereiche"[^>]*>(.*?)</nav>', html, re.S)
    assert treffer, "Navigation der Bereiche fehlt"
    return treffer.group(1)


def _beschriftungen(nav: str) -> list[str]:
    return [t.strip() for t in re.findall(r'<span class="flex-1 min-w-0 truncate">(.*?)</span>', nav, re.S)]


class TestBereiche:
    def test_sieben_bereiche_in_fester_folge(self, besucher: Client) -> None:
        nav = _bereiche(_seite(besucher, "paper_list"))
        assert _beschriftungen(nav) == BEREICHE

    def test_ohne_gruppen_abzeichen_und_zaehler(self, besucher: Client, kommune: OParlBody) -> None:
        OParlMeeting.objects.create(
            external_id="https://ris.example.org/meeting/1",
            body=kommune,
            name="Rat",
            start=timezone.now() + timedelta(days=3),
        )
        html = _seite(besucher, "paper_list")
        nav = _bereiche(html)
        assert "Neu" not in nav
        assert not re.search(r">\s*\d+\s*<", nav), "kein Zähler an den Bereichen"
        for alt in ("Portal", "Entdecken", "Meine Stadt", "Nachbarschaft", "Dokumente", "Suche", "KI-Chat"):
            assert alt not in nav

    def test_aktiver_bereich_mit_aria_current(self, besucher: Client, kommune: OParlBody) -> None:
        nav = _bereiche(_seite(besucher, "paper_list"))
        assert nav.count('aria-current="page"') == 1
        aktiv = re.search(r'<a[^>]*aria-current="page"[^>]*>.*?</a>', nav, re.S)
        assert aktiv and "Vorgänge" in aktiv.group(0)

    def test_nachbarschaft_gehoert_zur_karte(self, besucher: Client) -> None:
        nav = _bereiche(_seite(besucher, "neighborhood"))
        aktiv = re.search(r'<a[^>]*aria-current="page"[^>]*>.*?</a>', nav, re.S)
        assert aktiv and "Karte" in aktiv.group(0)

    def test_beschluesse_nur_wenn_die_kommune_sie_veroeffentlicht(self, besucher: Client, kommune: OParlBody) -> None:
        assert "Beschlüsse" not in _bereiche(_seite(besucher, "paper_list"))
        SessionTenant.objects.create(
            name="Verwaltung Beispielstadt",
            slug="beispielstadt",
            oparl_body=kommune,
            insight_publish=True,
            implementation_publish=True,
        )
        assert "Beschlüsse" in _beschriftungen(_bereiche(_seite(besucher, "paper_list")))

    def test_ratsfragen_nur_wenn_eingeschaltet(self, besucher: Client) -> None:
        with override_settings(INSIGHT_QUESTIONS_ENABLED=False):
            assert "Ratsfragen" not in _bereiche(_seite(besucher, "paper_list"))
        with override_settings(INSIGHT_QUESTIONS_ENABLED=True):
            assert "Ratsfragen" in _beschriftungen(_bereiche(_seite(besucher, "paper_list")))

    def test_dokumente_leiten_in_die_suche_weiter(self, besucher: Client) -> None:
        antwort = besucher.get("/insight/dokumente/?q=Radweg&page=2")
        assert antwort.status_code == 301
        assert antwort["Location"] == "/insight/suche/?type=file&q=Radweg"
        assert besucher.get("/insight/dokumente/")["Location"] == "/insight/suche/?type=file"

    def test_jeder_url_name_der_zuordnung_existiert(self) -> None:
        namen = set(get_resolver().namespace_dict["insight_core"][1].namespace_dict["insight"][1].reverse_dict)
        unbekannt = [name for name in NAV_AREAS if name not in namen]
        assert not unbekannt, f"URL-Namen ohne Route: {unbekannt}"


class TestRahmen:
    def test_kein_schwebender_chat_knopf(self, besucher: Client) -> None:
        html = _seite(besucher, "paper_list")
        assert "chatbotPopup" not in html
        assert "fixed bottom-4 right-4" not in html

    def test_menue_ist_ein_dialog(self, besucher: Client) -> None:
        html = _seite(besucher, "paper_list")
        knopf = re.search(r'<button[^>]*x-ref="menuButton"[^>]*>', html, re.S)
        assert knopf and 'aria-controls="insight-navigation"' in knopf.group(0)
        assert 'aria-expanded="false"' in knopf.group(0)
        leiste = re.search(r'<aside id="insight-navigation"[^>]*>', html, re.S)
        assert leiste
        assert ":role=\"sidebarOpen ? 'dialog' : null\"" in leiste.group(0)
        assert "x-trap" in leiste.group(0) and "@keydown.escape.window" in leiste.group(0)
        # Geschlossen schon im HTML (#763): mobil außerhalb und unsichtbar, ab lg sichtbar
        assert "-translate-x-full invisible lg:translate-x-0 lg:visible" in leiste.group(0)

    def test_suche_in_der_kopfzeile_ausser_auf_uebersicht_und_suche(self, besucher: Client) -> None:
        assert 'role="search"' in _seite(besucher, "paper_list")
        assert 'id="kopf-suche"' not in _seite(besucher, "search")

    def test_schrift_inter_selbst_gehostet(self, besucher: Client) -> None:
        html = _seite(besucher, "paper_list")
        assert "vendor/inter/inter-latin" in html and "@font-face" in html
        assert "fonts.googleapis" not in html

    def test_fuss_mit_datenstand_und_barrierefreiheit(self, besucher: Client) -> None:
        html = _seite(besucher, "paper_list")
        fuss = html[html.index("<footer") :]
        assert "Letzter Abgleich" in fuss
        assert "/barrierefreiheit/" in fuss and "Quellcode" in fuss
        assert "Mandari" not in fuss

    def test_titel_mit_kleinem_mandari(self, besucher: Client) -> None:
        html = _seite(besucher, "paper_list")
        titel = re.search(r"<title>(.*?)</title>", html, re.S)
        assert titel and "mandari Insight" in titel.group(1) and "Mandari" not in titel.group(1)


class TestAnsprache:
    @pytest.mark.parametrize("name", ["portal_home", "saved"])
    def test_sie_form(self, besucher: Client, name: str) -> None:
        html = _seite(besucher, name)
        text = re.sub(r"<[^>]+>", " ", html)
        for wort in ("deine", "Deine", "deiner", "Wähle", "Klicke", "Melde dich", "möchtest du"):
            assert not re.search(rf"\b{wort}\b", text), wort


class TestSitzungsende:
    def _sitzung(self, kommune: OParlBody, start: datetime, end: datetime | None) -> OParlMeeting:
        return OParlMeeting.objects.create(
            external_id=f"https://ris.example.org/meeting/{start:%H%M}-{end:%H%M}"
            if end
            else "https://ris.example.org/m",
            body=kommune,
            name="Jugendrat",
            start=start,
            end=end,
        )

    def test_ende_um_mitternacht_vor_dem_beginn_fehlt(self, besucher: Client, kommune: OParlBody) -> None:
        tag = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2)
        sitzung = self._sitzung(kommune, tag.replace(hour=17), tag)
        html = _seite(besucher, "meeting_detail", sitzung.pk)
        assert ">Ende</dt>" not in html
        assert ">Beginn</dt>" in html

    def test_ende_am_selben_tag_nur_mit_uhrzeit(self, besucher: Client, kommune: OParlBody) -> None:
        tag = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2)
        sitzung = self._sitzung(kommune, tag.replace(hour=17), tag.replace(hour=19, minute=30))
        html = _seite(besucher, "meeting_detail", sitzung.pk)
        ende = re.search(r'data-testid="sitzung-ende">(.*?)</dd>', html, re.S)
        assert ende and ende.group(1).split() == ["19:30", "Uhr"]
