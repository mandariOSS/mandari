# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kennzahlen der Portal-Startseite kommen aus dem Cache.

Ohne Cache zählte jede Anfrage auf ``/insight/`` über die größten Tabellen des
Portals — auf dem Produktivbestand rund 180 ms Datenbankzeit pro Aufruf.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from insight_core.models import OParlBody, OParlPaper, OParlSource
from insight_core.services import portal_stats


@pytest.fixture(autouse=True)
def leerer_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def kommune(db: None) -> OParlBody:
    source = OParlSource.objects.create(name="Testquelle", url="https://example.org/oparl")
    body = OParlBody.objects.create(source=source, external_id="body-1", name="Stadt Beispiel", slug="beispiel")
    for nummer in range(3):
        OParlPaper.objects.create(body=body, external_id=f"paper-{nummer}", name=f"Vorlage {nummer}")
    return body


def zaehl_queries[T](funktion: Callable[[], T]) -> tuple[T, int]:
    """Führt ``funktion`` aus und zählt dabei die Datenbankabfragen."""
    with CaptureQueriesContext(connection) as ctx:
        ergebnis = funktion()
    return ergebnis, len(ctx.captured_queries)


def test_kennzahlen_einer_kommune_kommen_beim_zweiten_aufruf_ohne_datenbank(kommune: OParlBody) -> None:
    erste, queries_erste = zaehl_queries(lambda: portal_stats.body_stats(kommune))
    zweite, queries_zweite = zaehl_queries(lambda: portal_stats.body_stats(kommune))

    assert erste["papers"] == 3
    assert zweite == erste
    assert queries_erste > 0
    assert queries_zweite == 0


def test_gesamtzahlen_kommen_beim_zweiten_aufruf_ohne_datenbank(kommune: OParlBody) -> None:
    erste, _ = zaehl_queries(portal_stats.overview_stats)
    zweite, queries_zweite = zaehl_queries(portal_stats.overview_stats)

    assert erste["papers"] == 3
    assert zweite == erste
    assert queries_zweite == 0


def test_zahlen_je_kommune_sind_nach_kommune_getrennt(kommune: OParlBody) -> None:
    zweite_kommune = OParlBody.objects.create(
        source=kommune.source, external_id="body-2", name="Gemeinde Zweit", slug="zweit"
    )

    assert portal_stats.body_stats(kommune)["papers"] == 3
    assert portal_stats.body_stats(zweite_kommune)["papers"] == 0


def test_nach_einem_sync_stimmen_die_zahlen_sofort_wieder(kommune: OParlBody) -> None:
    assert portal_stats.body_stats(kommune)["papers"] == 3

    OParlPaper.objects.create(body=kommune, external_id="paper-neu", name="Neue Vorlage")
    assert portal_stats.body_stats(kommune)["papers"] == 3, "bis zum Sync darf der Cache alt sein"

    portal_stats.invalidate_portal_stats()
    assert portal_stats.body_stats(kommune)["papers"] == 4


def test_kommunenliste_traegt_die_kennzahlen_aus_dem_cache(kommune: OParlBody) -> None:
    counts = portal_stats.counts_by_body()
    assert counts["papers"][str(kommune.id)] == 3

    _, queries = zaehl_queries(portal_stats.counts_by_body)
    assert queries == 0


def test_startseite_zaehlt_nur_beim_ersten_aufruf(client: Client, kommune: OParlBody) -> None:
    # Zwei Kommunen: die Startseite zeigt dann die Auswahlseite mit allen Kennzahlen.
    OParlBody.objects.create(source=kommune.source, external_id="body-3", name="Gemeinde Dritt", slug="dritt")

    erste_antwort = client.get("/insight/")
    assert erste_antwort.status_code == 200

    with CaptureQueriesContext(connection) as ctx:
        zweite_antwort = client.get("/insight/")

    assert zweite_antwort.status_code == 200
    zaehlungen = [q["sql"] for q in ctx.captured_queries if "COUNT" in q["sql"].upper() and "oparl_papers" in q["sql"]]
    assert zaehlungen == [], "die großen Tabellen dürfen beim zweiten Aufruf nicht erneut gezählt werden"
