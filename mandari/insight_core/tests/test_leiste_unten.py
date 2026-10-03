# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mobile Navigation in Variante C (Issue #783, Stufe 3): Leiste unten mit fünf Zielen (Start, Sitzungen, Vorgänge,
Karte, Mehr), Blatt „Mehr“ als modaler Dialog mit den übrigen Bereichen, Kommunenwechsel, Dunkelmodus und Konto,
Safe-Area unten. Die Seitenleiste bleibt dem Desktop vorbehalten.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from insight_core.models import OParlBody, OParlSource

pytestmark = pytest.mark.django_db


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


def _seite(client: Client, name: str) -> str:
    antwort = client.get(reverse(f"insight_core:insight:{name}"))
    assert antwort.status_code == 200
    return antwort.content.decode()


def _leiste(html: str) -> str:
    treffer = re.search(r'<nav aria-label="Hauptbereiche"[^>]*>(.*?)</nav>', html, re.S)
    assert treffer, "Leiste unten fehlt"
    return treffer.group(0)


def _blatt(html: str) -> str:
    start = html.index('<div id="insight-mehr"')
    return html[start : html.index("</ul>\n    </div>\n</div>", start)]


def test_fuenf_ziele_in_fester_folge(besucher: Client) -> None:
    leiste = _leiste(_seite(besucher, "paper_list"))
    beschriftungen = re.findall(r'<span class="max-w-full truncate">(.*?)</span>', leiste) + ["Mehr"]
    assert beschriftungen == ["Start", "Sitzungen", "Vorgänge", "Karte", "Mehr"]
    assert "lg:hidden fixed inset-x-0 bottom-0" in leiste
    assert "pb-[env(safe-area-inset-bottom)]" in leiste, "Safe-Area unten"


@pytest.mark.parametrize(
    ("seite", "ziel"), [("meeting_list", "Sitzungen"), ("paper_list", "Vorgänge"), ("map", "Karte")]
)
def test_aktives_ziel_mit_aria_current(besucher: Client, seite: str, ziel: str) -> None:
    leiste = _leiste(_seite(besucher, seite))
    aktiv = re.findall(
        r'<a href="[^"]*" aria-current="page".*?<span class="max-w-full truncate">(.*?)</span>', leiste, re.S
    )
    assert aktiv == [ziel]


@pytest.mark.parametrize("seite", ["organization_list", "person_list", "chat", "saved"])
def test_mehr_markiert_bereiche_im_blatt(besucher: Client, seite: str) -> None:
    html = _seite(besucher, seite)
    leiste = _leiste(html)
    assert 'aria-current="page"' not in leiste
    assert "enthält den aktuellen Bereich" in leiste
    assert 'aria-current="page"' in _blatt(html), "im Blatt ist der Bereich hervorgehoben"


def test_blatt_mehr_inhalt(besucher: Client) -> None:
    blatt = _blatt(_seite(besucher, "paper_list"))
    for eintrag in (
        "Gremien",
        "Personen",
        "KI-Assistent",
        "Gespeichert",
        "Kommune wechseln",
        "Dunkler Modus",
        "Anmelden",
    ):
        assert eintrag in blatt, eintrag
    assert "Beschlüsse" not in blatt and "Ratsfragen" not in blatt, "nur mit Inhalten"
    assert "pb-[max(1.25rem,env(safe-area-inset-bottom))]" in blatt
    assert 'aria-label="Schließen"' in blatt


@override_settings(INSIGHT_QUESTIONS_ENABLED=True, INSIGHT_SUBSCRIPTIONS_ENABLED=True)
def test_blatt_mit_ratsfragen_und_benachrichtigungen(besucher: Client) -> None:
    blatt = _blatt(_seite(besucher, "paper_list"))
    assert "Ratsfragen" in blatt and "Benachrichtigungen" in blatt


def test_kopfzeile_am_handy_ohne_menue(besucher: Client) -> None:
    html = _seite(besucher, "paper_list")
    kopf = html[html.index("<header") : html.index("</header>")]
    assert "Menü" not in kopf and "openMenu" not in kopf
    assert re.search(r'<button[^>]*data-kommune-wechseln[^>]*>\s*<span class="truncate">Beispielstadt</span>', kopf)
    assert 'aria-label="Suche"' in kopf


def test_inhalt_nicht_unter_der_leiste(besucher: Client) -> None:
    html = _seite(besucher, "paper_list")
    assert "pb-[calc(4rem+env(safe-area-inset-bottom))] lg:pb-0" in html
