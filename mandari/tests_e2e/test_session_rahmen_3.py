# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teil 3 des neuen Erscheinungsbilds im Sitzungsdienst im Browser (Issue #944).

Mit den Demo-Daten (setup_demo_environment) und eingeschaltetem Schalter stehen die letzten Seiten des
Sitzungsdienstes im neuen Erscheinungsbild und laufen am Handy (390 px) und am Tablet (768 px) nicht seitlich über,
ohne Fehler im Browser: Gremien- und Personenliste, Archiv, Vorlagen zur Prüfung, Meine Mitzeichnungen, Suche (leer
und mit Treffern), Berichte, Jahresplanung und die Sitzungsgelder eines Jahres.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect

from apps.common.demo_daten import DEMO_USERS
from tests_e2e.conftest import BrowserProblems, login_via_form, wait_for_bundle
from tests_e2e.test_session_rahmen import BREITEN, MANDANT, PASSWORT, UEBERLAUF, demo  # noqa: F401


def _adresse(name: str) -> str:
    return reverse(f"session:{name}", kwargs={"tenant_slug": MANDANT})


@pytest.fixture
def seiten_3(demo: dict[str, str]) -> dict[str, str]:  # noqa: F811
    """Die Seiten aus Teil 3; Suche und Jahresübersichten mit den Daten der Demo."""
    jahr = timezone.localdate().year
    return {
        "gremien": _adresse("organizations"),
        "personen": _adresse("persons"),
        "archiv": _adresse("archive"),
        "zu-pruefen": _adresse("papers_review"),
        "mitzeichnungen": _adresse("my_cosignatures"),
        "suche-leer": _adresse("search"),
        "suche": f"{_adresse('search')}?q=Spielplatz",
        "berichte": _adresse("reports"),
        "jahresplanung": f"{_adresse('meeting_plan')}?year={jahr}",
        "sitzungsgelder-jahr": f"{_adresse('allowance_year')}?year={jahr}",
    }


def test_letzte_seiten_am_handy_und_tablet(
    page: Any, live_server: Any, seiten_3: dict[str, str], problems: BrowserProblems, screenshot: Any
) -> None:
    login_via_form(page, live_server.url, DEMO_USERS["verwaltung"]["email"], PASSWORT)
    for breite in BREITEN:
        page.set_viewport_size({"width": breite, "height": 900})
        for name, pfad in seiten_3.items():
            antwort = page.goto(f"{live_server.url}{pfad}")
            assert antwort is not None and antwort.ok, f"{name}: {antwort.status if antwort else 'keine Antwort'}"
            wait_for_bundle(page)
            expect(page.locator("main#inhalt")).to_have_attribute("data-gestaltung", "neu")
            masse = page.evaluate(UEBERLAUF)
            assert masse["scroll"] <= masse["breite"] + 1, f"{name} bei {breite} px läuft seitlich über: {masse}"
            if breite == 390:
                screenshot(f"session-neu-3-{name}-390")
    problems.assert_clean("Letzte Seiten aus Teil 3 im neuen Rahmen")
