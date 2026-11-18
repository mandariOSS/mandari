# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Zahlen unter ``/api/stats/`` und ``/api/stats/bodies/`` aus einem vorberechneten Stand (Issue #1478).

Bisher zählte jeder Abruf nach Ablauf des Caches selbst über die größten Tabellen des Portals: in Produktion 3 bis 5 s,
unter Last über 10 s. Jetzt schreibt der Zeitplan ``oeffentliche_zahlen_fortschreiben`` die Zahlen je Kommune fort, die
Abrufe lesen nur diesen Stand. Geprüft wird: dieselben Zahlen wie bisher (ohne Gelöschtes, ohne nicht gelistete
Kommunen), kein Zählen im Abruf, der Rückweg ohne Stand und der Zeitplan.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlPaper, OParlPerson, OParlSource
from insight_core.services import portal_stats

pytestmark = pytest.mark.django_db

GROSSE_TABELLEN = ("oparl_papers", "oparl_meetings", "oparl_persons", "oparl_organizations")


@pytest.fixture(autouse=True)
def leerer_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def _kommune(nummer: int, name: str, *, is_listed: bool = True) -> OParlBody:
    quelle = OParlSource.objects.create(name=f"Quelle {nummer}", url=f"https://example.org/oparl/{nummer}")
    return OParlBody.objects.create(
        source=quelle, external_id=f"body-{nummer}", name=name, slug=f"kommune-{nummer}", is_listed=is_listed
    )


def _bestand(body: OParlBody, *, vorgaenge: int, sitzungen: int, gremien: int, personen: int) -> None:
    kennung = body.external_id
    for nummer in range(vorgaenge):
        OParlPaper.objects.create(body=body, external_id=f"{kennung}-paper-{nummer}", name=f"Vorlage {nummer}")
    for nummer in range(sitzungen):
        OParlMeeting.objects.create(body=body, external_id=f"{kennung}-meeting-{nummer}", name=f"Sitzung {nummer}")
    for nummer in range(gremien):
        OParlOrganization.objects.create(body=body, external_id=f"{kennung}-org-{nummer}", name=f"Gremium {nummer}")
    for nummer in range(personen):
        OParlPerson.objects.create(body=body, external_id=f"{kennung}-person-{nummer}", name=f"Person {nummer}")


@pytest.fixture
def bestand() -> dict[str, OParlBody]:
    """Zwei gelistete Kommunen (eine ohne Daten), eine nicht gelistete, dazu ein gelöschter Vorgang."""
    aachen = _kommune(1, "Stadt Aachen")
    _bestand(aachen, vorgaenge=3, sitzungen=2, gremien=1, personen=2)
    OParlPaper.objects.create(body=aachen, external_id="body-1-geloescht", name="Gelöscht", deleted=True)
    leer = _kommune(2, "Gemeinde Zell")
    demo = _kommune(3, "Demo-Kommune", is_listed=False)
    _bestand(demo, vorgaenge=5, sitzungen=4, gremien=3, personen=2)
    return {"aachen": aachen, "leer": leer, "demo": demo}


def _abruf(pfad: str) -> tuple[dict[str, Any], list[str]]:
    """Antwort als JSON und die SQL-Texte des Abrufs."""
    with CaptureQueriesContext(connection) as erfasst:
        antwort = Client().get(pfad)
    assert antwort.status_code == 200
    daten: dict[str, Any] = antwort.json()
    return daten, [q["sql"] for q in erfasst.captured_queries]


def _zaehlt(sql: list[str]) -> list[str]:
    return [text for text in sql if any(tabelle in text for tabelle in GROSSE_TABELLEN)]


def test_zahlen_ohne_geloeschtes_und_ohne_nicht_gelistete_kommunen(bestand: dict[str, OParlBody]) -> None:
    daten, _sql = _abruf("/api/stats/")

    assert daten == {"bodies": 2, "organizations": 1, "persons": 2, "meetings": 2, "papers": 3}


def test_abruf_zaehlt_nicht_selbst_wenn_ein_stand_da_ist(bestand: dict[str, OParlBody]) -> None:
    portal_stats.api_stand(refresh=True)

    daten, sql = _abruf("/api/stats/")
    kommunen, sql_kommunen = _abruf("/api/stats/bodies/")

    assert daten["papers"] == 3 and len(kommunen["bodies"]) == 2
    assert _zaehlt(sql) == [] and _zaehlt(sql_kommunen) == [], "nur der Stand und die Liste der Kommunen"
    assert len(sql_kommunen) == 1, "eine Abfrage für die Kommunen, keine je Kommune"


def test_ohne_stand_zaehlt_der_erste_abruf_einmal_und_legt_ihn_ab(bestand: dict[str, OParlBody]) -> None:
    assert cache.get(portal_stats.API_STAND_KEY) is None

    daten, sql = _abruf("/api/stats/")

    assert daten["papers"] == 3
    assert len(_zaehlt(sql)) == 4, "je Tabelle eine Abfrage"
    stand = cache.get(portal_stats.API_STAND_KEY)
    assert stand["je_kommune"]["papers"] == {str(bestand["aachen"].pk): 3, str(bestand["demo"].pk): 5}
    # Der nächste Abruf einer anderen Adresse liest ihn
    assert _zaehlt(_abruf("/api/stats/bodies/")[1]) == []


def test_stand_gilt_bis_der_zeitplan_ihn_fortschreibt(bestand: dict[str, OParlBody]) -> None:
    from insight_core import schedules

    portal_stats.api_stand(refresh=True)
    OParlPaper.objects.create(body=bestand["aachen"], external_id="neu", name="Neue Vorlage")

    assert portal_stats.api_gesamt(portal_stats.api_stand(), set())["papers"] == 8, "noch der abgelegte Stand"

    assert schedules.oeffentliche_zahlen_fortschreiben.call() == 2, "zwei Kommunen mit Vorgängen"
    stand = portal_stats.api_stand()
    assert portal_stats.api_gesamt(stand, set())["papers"] == 9
    assert portal_stats.api_gesamt(stand, {str(bestand["demo"].pk)})["papers"] == 4


def test_unbrauchbarer_stand_wird_neu_gerechnet(bestand: dict[str, OParlBody]) -> None:
    cache.set(portal_stats.API_STAND_KEY, "kein Stand", 60)

    stand = portal_stats.api_stand()

    assert stand["je_kommune"]["meetings"][str(bestand["aachen"].pk)] == 2
    assert cache.get(portal_stats.API_STAND_KEY) == stand


def test_kommunen_mit_ihren_zahlen(bestand: dict[str, OParlBody]) -> None:
    daten, _sql = _abruf("/api/stats/bodies/")

    assert [k["name"] for k in daten["bodies"]] == ["Gemeinde Zell", "Stadt Aachen"], "nach Namen, ohne Demo-Kommune"
    zell, aachen = daten["bodies"]
    assert (
        aachen["organizations_count"],
        aachen["persons_count"],
        aachen["meetings_count"],
        aachen["papers_count"],
    ) == (
        1,
        2,
        2,
        3,
    )
    assert (zell["organizations_count"], zell["persons_count"], zell["meetings_count"], zell["papers_count"]) == (
        0,
        0,
        0,
        0,
    )
    assert aachen["id"] == str(bestand["aachen"].pk) and aachen["slug"] == "kommune-1"


def test_zeitplan() -> None:
    from apps.events.schedule import Catchup, Every, autodiscover, registry

    autodiscover()
    eintrag = registry.get("insight_core.schedules.oeffentliche_zahlen_fortschreiben")

    assert eintrag is not None, "Zeitplan im Worker registriert"
    assert eintrag.trigger == Every(timedelta(minutes=10)) and eintrag.catchup == Catchup.NACHHOLEN
    assert eintrag.task.queue_name == "default", "der Worker der Compose-Vorlage bedient default"
    assert eintrag.task.call() == 0, "ohne Bestand"
    assert cache.get(portal_stats.API_STAND_KEY)["je_kommune"]["papers"] == {}
