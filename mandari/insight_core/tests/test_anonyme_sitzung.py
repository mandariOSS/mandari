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
    # Zweite Kommune: Mit nur einer wählt die Startseite sie selbst (Self-Hosting) und leitet weiter
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

    antwort = Client().get(pfad)

    assert antwort.status_code == 200
    assert settings.SESSION_COOKIE_NAME not in antwort.cookies
    assert not Session.objects.exists()


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

    assert list(Session.objects.values_list("session_key", flat=True)) == ["neu" + "0" * 29]
    assert "gelöscht: 7" in ausgabe.getvalue()
