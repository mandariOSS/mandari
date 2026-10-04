# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rahmen des Bürgerportals in Variante A (Issue #783, Stufe 2): Seitenleiste als eigene graue Fläche mit der Kommune
als weißer Schaltfläche, Kopfzeile mit Brotkrumen (mit Kommune) und Suche, Hauptseiten in Bändern statt Karten,
Fuß als Band in Tinte, Kommunenwechsel ohne Liste aller Kommunen.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from insight_core.models import OParlBody, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db


@pytest.fixture
def kommune(db: Any) -> OParlBody:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    for nummer in range(2, 40):
        OParlBody.objects.create(
            external_id=f"https://ris.example.org/body/{nummer}", source=source, name=f"Gemeinde Ort{nummer:02d}"
        )
    return OParlBody.objects.create(
        external_id="https://ris.example.org/body/1",
        source=source,
        name="Stadt Beispielstadt",
        display_name="Beispielstadt",
        slug="beispielstadt",
        ags="05999000",
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


def _brotkrumen(html: str) -> str:
    treffer = re.search(r'<nav aria-label="Brotkrumen"[^>]*>(.*?)</nav>', html, re.S)
    assert treffer, "Brotkrumen in der Kopfzeile fehlen"
    return re.sub(r"\s+", " ", treffer.group(1))


class TestSeitenleiste:
    def test_eigene_flaeche_und_kommune_als_schaltflaeche(self, besucher: Client) -> None:
        html = _seite(besucher, "paper_list")
        leiste = html[html.index('<aside id="insight-navigation"') : html.index("</aside>")]
        assert "bg-band-grau" in leiste and "border-r" not in leiste
        knopf = re.search(r"<button[^>]*data-kommune-wechseln[^>]*>(.*?)</button>", leiste, re.S)
        assert knopf and "bg-band-hell" in knopf.group(0) and "Beispielstadt" in knopf.group(1)
        assert 'aria-haspopup="dialog"' in knopf.group(0)

    def test_keine_liste_aller_kommunen_auf_jeder_seite(self, besucher: Client) -> None:
        html = _seite(besucher, "paper_list")
        assert "Ort02" not in html and "Ort39" not in html, "der Wechsel lädt Vorschläge erst bei der Eingabe"
        assert 'data-vorschlaege-url="/insight/kommunen/vorschlaege/"' in html

    def test_dialog_kommune_wechseln(self, besucher: Client) -> None:
        html = _seite(besucher, "paper_list")
        assert 'role="dialog" aria-modal="true" aria-labelledby="kommune-dialog-titel"' in html
        assert 'placeholder="Kommune oder Postleitzahl"' in html
        assert "In meiner Nähe" in html

    def test_zuletzt_besucht_nur_im_browser(self, besucher: Client, kommune: OParlBody) -> None:
        html = _seite(besucher, "paper_list")
        assert 'data-kommune-name="Beispielstadt"' in html
        assert f'data-kommune-url="/insight/kommune/{kommune.id}/"' in html
        antwort = besucher.get(reverse("insight_core:insight:paper_list"))
        assert not [name for name in antwort.cookies if "zuletzt" in name], "kein Cookie für „Zuletzt besucht“"


class TestKopfzeile:
    def test_brotkrumen_mit_kommune_und_bereich(self, besucher: Client) -> None:
        krumen = _brotkrumen(_seite(besucher, "paper_list"))
        assert ">Beispielstadt</a>" in krumen
        assert (
            '<span aria-current="page" class="font-medium text-gray-900 dark:text-gray-100">Vorgänge</span>' in krumen
        )

    def test_brotkrumen_einer_detailseite(self, besucher: Client, kommune: OParlBody) -> None:
        sitzung = OParlMeeting.objects.create(
            external_id="https://ris.example.org/meeting/1", body=kommune, name="Ratssitzung", start=timezone.now()
        )
        html = _seite(besucher, "meeting_detail", sitzung.pk)
        krumen = _brotkrumen(html)
        assert f'href="{reverse("insight_core:insight:meeting_list")}"' in krumen and ">Sitzungen</a>" in krumen
        assert 'aria-current="page"' in krumen and "Ratssitzung" in krumen
        assert '<nav aria-label="Zurück" class="lg:hidden mb-4">' in html, "am Handy der Weg zurück zur Liste"

    def test_uebersicht_ohne_zweites_suchfeld_in_der_kopfzeile(self, besucher: Client) -> None:
        html = _seite(besucher, "portal_home")
        assert 'id="kopf-suche"' not in html and 'id="uebersicht-suche"' in html
        assert (
            '<span aria-current="page" class="font-medium text-gray-900 dark:text-gray-100">Beispielstadt</span>'
            in html
        )


class TestBaender:
    def test_uebersicht_in_baendern_ohne_karten_und_zaehler(self, besucher: Client, kommune: OParlBody) -> None:
        OParlPaper.objects.create(
            external_id="https://ris.example.org/paper/1", body=kommune, name="Radweg am Markt", date=timezone.now()
        )
        OParlMeeting.objects.create(
            external_id="https://ris.example.org/meeting/2",
            body=kommune,
            name="Sitzung",
            start=timezone.now() + timedelta(days=2),
        )
        html = _seite(besucher, "portal_home")
        assert "Beispielstadt transparent" in html and "bg-primary-100" in html
        assert html.count('type="submit"') >= 1 and ">Suchen</button>" in html
        assert "Nächste Sitzungen" in html and "Neue Vorgänge" in html and "Radweg am Markt" in html
        assert "Was passiert in Ihrer Nähe?" in html
        for slop in ("in Zahlen", "Was möchten Sie wissen", "fade-up", "rounded-xl border border-gray-200"):
            assert slop not in html, slop

    @pytest.mark.parametrize(
        ("name", "titel"),
        [
            ("meeting_list", "Sitzungen"),
            ("paper_list", "Vorgänge"),
            ("organization_list", "Gremien"),
            ("person_list", "Personen"),
        ],
    )
    def test_listenseiten_mit_kopfband(self, besucher: Client, name: str, titel: str) -> None:
        html = _seite(besucher, name)
        kopf = re.search(r'<section class="bg-band-grau"[^>]*>\s*<div[^>]*>\s*<h1[^>]*>(.*?)</h1>', html, re.S)
        assert kopf and kopf.group(1).strip() == titel
        assert "von Beispielstadt" in html

    def test_listen_ohne_karte_und_ohne_grossbuchstaben(self, besucher: Client, kommune: OParlBody) -> None:
        OParlPaper.objects.create(external_id="https://ris.example.org/paper/2", body=kommune, name="Haushalt")
        html = _seite(besucher, "paper_list")
        liste = html[html.index('id="papers-list"') :]
        assert "uppercase" not in liste[: liste.index("</table>")]
        assert "rounded-xl border border-gray-200" not in liste[: liste.index("</table>")]

    def test_fuss_als_band_in_tinte(self, besucher: Client) -> None:
        html = _seite(besucher, "paper_list")
        assert '<footer class="bg-band-tinte' in html
        assert reverse("insight_core:insight:kommunen") in html[html.index("<footer") :]


class TestAuswahlseite:
    def test_suche_statt_liste(self, client: Client, kommune: OParlBody) -> None:
        client.get(reverse("insight_core:insight:clear_body"))
        html = _seite(client, "portal_home")
        assert "hlen Sie Ihre Kommune" in html and 'id="auswahl-eingabe"' in html
        assert "Ort02" not in html and "Beispielstadt transparent" not in html
        assert "select-hero-glow" not in html, "kein Schimmer"
        assert "In allen Kommunen suchen" in html
