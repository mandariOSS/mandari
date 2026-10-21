# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Issue #1091: Anonyme Aufrufe des Bürgerportals legen keine Sitzung an.

Der Standardwert „alle Kommunen“ wurde bei jedem ersten Aufruf in die Sitzung geschrieben. Suchmaschinen schicken das
Cookie nicht zurück, also entstand je Abruf eine Sitzungszeile (in Produktion 1,4 Mio.). Ein fehlender Wert bedeutet
ohnehin „alle Kommunen“.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import Client

from insight_core.models import OParlBody, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _cache_leeren() -> None:
    cache.clear()


@pytest.fixture
def bestand(db: Any) -> dict[str, Any]:
    source = OParlSource.objects.create(name="RIS Musterstadt", url="https://ris.example.org/system")
    body = OParlBody.objects.create(
        external_id="https://ris.example.org/body/1", source=source, name="Musterstadt", slug="musterstadt"
    )
    # Zweite Kommune: Mit nur einer wählt die Startseite sie selbst (Self-Hosting, Tests unten)
    OParlBody.objects.create(external_id="https://ris.example.org/body/2", source=source, name="Beispielort")
    vorgang = OParlPaper.objects.create(external_id="https://ris.example.org/paper/1", body=body, name="Radweg")
    sitzung = OParlMeeting.objects.create(external_id="https://ris.example.org/meeting/1", body=body, name="Rat")
    return {"body": body, "vorgang": vorgang, "sitzung": sitzung}


@pytest.mark.parametrize("seite", ["start", "vorgang", "sitzung"])
def test_anonymer_aufruf_legt_keine_sitzung_an(bestand: dict[str, Any], seite: str) -> None:
    pfad = {
        "start": "/insight/",
        "vorgang": f"/insight/vorgaenge/{bestand['vorgang'].id}/",
        "sitzung": f"/insight/termine/{bestand['sitzung'].id}/",
    }[seite]

    vorher = Session.objects.count()  # andere Tests derselben Datenbank können Sitzungen hinterlassen

    antwort = Client().get(pfad)

    assert antwort.status_code == 200
    assert settings.SESSION_COOKIE_NAME not in antwort.cookies
    assert Session.objects.count() == vorher


def test_ohne_auswahl_gelten_weiter_alle_kommunen(bestand: dict[str, Any]) -> None:
    antwort = Client().get("/insight/")

    assert antwort.context["show_all_bodies"] is True
    assert antwort.context["active_body"] is None


def test_gewaehlte_kommune_bleibt_in_der_sitzung(bestand: dict[str, Any]) -> None:
    client = Client()

    client.get(f"/insight/kommune/{bestand['body'].id}/")

    assert client.session["active_body_id"] == str(bestand["body"].id)
    assert client.get("/insight/").context["active_body"] == bestand["body"]


def test_aufraeumen_loescht_nur_abgelaufene_sitzungen() -> None:
    from datetime import timedelta
    from io import StringIO

    from django.core.management import call_command
    from django.utils import timezone

    jetzt = timezone.now()
    for nummer in range(7):
        Session.objects.create(session_key=f"alt{nummer:029d}", session_data="", expire_date=jetzt - timedelta(days=1))
    Session.objects.create(session_key="neu" + "0" * 29, session_data="", expire_date=jetzt + timedelta(days=1))
    ausgabe = StringIO()

    call_command("sitzungen_aufraeumen", "--trotz-zeitplan", stdout=ausgabe)

    eigene = Session.objects.filter(session_key__regex=r"^(alt|neu)0")
    assert list(eigene.values_list("session_key", flat=True)) == ["neu" + "0" * 29]
    assert not Session.objects.filter(expire_date__lt=jetzt).exists()


GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


def test_crawler_auf_der_stadtseite_bekommt_keine_sitzung(bestand: dict[str, Any]) -> None:
    # Die Stadtseite hält den Kontext der Kommune in der Sitzung fest (portal.enter); für Crawler verwirft die
    # Middleware die neue Sitzung (Teil 2 von #1091)
    vorher = Session.objects.count()

    antwort = Client(HTTP_USER_AGENT=GOOGLEBOT).get("/insight/k/musterstadt/")

    assert antwort.status_code == 200
    assert settings.SESSION_COOKIE_NAME not in antwort.cookies
    assert Session.objects.count() == vorher


def test_mensch_auf_der_stadtseite_behaelt_den_kontext(bestand: dict[str, Any]) -> None:
    client = Client(HTTP_USER_AGENT=BROWSER)

    antwort = client.get("/insight/k/musterstadt/")

    assert antwort.status_code == 200
    assert settings.SESSION_COOKIE_NAME in antwort.cookies
    assert client.session["active_body_id"] == str(bestand["body"].id)


def test_kalender_abo_mit_kommune_legt_keine_sitzung_an(bestand: dict[str, Any]) -> None:
    # Kalenderprogramme rufen den Feed regelmäßig ohne Cookie ab; ?kommune= wählte dabei jedes Mal die Kommune
    vorher = Session.objects.count()

    antwort = Client(HTTP_USER_AGENT="Microsoft Office/16.0 (Windows NT 10.0; Microsoft Outlook 16.0)").get(
        f"/insight/termine/kalender.ics?kommune={bestand['body'].id}"
    )

    assert antwort.status_code == 200
    assert settings.SESSION_COOKIE_NAME not in antwort.cookies
    assert Session.objects.count() == vorher


def test_bestehende_sitzung_bleibt_auch_fuer_crawler_kennung(bestand: dict[str, Any]) -> None:
    client = Client(HTTP_USER_AGENT=BROWSER)
    client.get(f"/insight/kommune/{bestand['body'].id}/")
    schluessel = client.session.session_key

    client.get("/insight/", HTTP_USER_AGENT=GOOGLEBOT)

    assert Session.objects.filter(session_key=schluessel).exists()


@pytest.fixture
def eine_kommune(db: Any) -> OParlBody:
    source = OParlSource.objects.create(name="RIS Einzelstadt", url="https://ris.einzel.example.org/system")
    return OParlBody.objects.create(
        external_id="https://ris.einzel.example.org/body/1", source=source, name="Einzelstadt", slug="einzelstadt"
    )


@pytest.mark.parametrize(
    "kennung", [GOOGLEBOT, "python-requests/2.32", "Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0"]
)
def test_startseite_mit_einer_kommune_ohne_weiterleitung(eine_kommune: OParlBody, kennung: str) -> None:
    """Issue #1110: Die Startseite wählt die einzige Kommune selbst, ohne Weiterleitung auf sich selbst.

    Für Crawler verwirft die Middleware die neue Sitzung; eine Weiterleitung liefe dann im Kreis.
    """
    antwort = Client(HTTP_USER_AGENT=kennung).get("/insight/")

    assert antwort.status_code == 200
    assert "Einzelstadt" in antwort.content.decode()
