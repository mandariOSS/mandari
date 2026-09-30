# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abo verwalten: Koordinaten überleben das Speichern (#172).

Die Verwaltungsseite gab die gespeicherten Koordinaten lokalisiert aus ("51,9606649"). Das
Formular schickte sie so zurück, und das DecimalField scheiterte beim Speichern mit einem
Serverfehler – schon wenn nur das Stichwort geändert wurde. Jetzt stehen die Startwerte
unlokalisiert als Datenattribute an der Alpine-Komponente, und der Server liest Koordinaten
tolerant (Dezimalkomma erlaubt, Unlesbares heißt: kein Ort).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from insight_core.models import InsightSubscriber, OParlBody, OParlSource
from insight_core.views.subscriptions import _coordinate

pytestmark = pytest.mark.django_db

LAT = Decimal("51.9606649")
LON = Decimal("7.6261347")


@pytest.fixture
def abonnent() -> InsightSubscriber:
    source = OParlSource.objects.create(name="Beispiel-RIS", url="https://ris.beispiel.example/oparl/system")
    body = OParlBody.objects.create(
        external_id="https://ris.beispiel.example/oparl/body/1", source=source, name="Beispielstadt", slug="beispiel"
    )
    return InsightSubscriber.objects.create(
        email="leser@example.org",
        body=body,
        confirmed=True,
        neighborhood_active=True,
        neighborhood_name="Prinzipalmarkt",
        neighborhood_lat=LAT,
        neighborhood_lon=LON,
        neighborhood_radius=1000,
    )


def test_koordinate_lesen() -> None:
    assert _coordinate("51.9606649", 90) == LAT
    assert _coordinate(" 51,9606649 ", 90) == LAT
    assert _coordinate("", 90) is None
    assert _coordinate("abc", 90) is None
    assert _coordinate("NaN", 90) is None
    assert _coordinate("91", 90) is None
    assert _coordinate("-180", 180) == Decimal("-180")


class TestVerwalten:
    @pytest.fixture(autouse=True)
    def _abos_an(self, settings: Any) -> None:
        settings.INSIGHT_SUBSCRIPTIONS_ENABLED = True

    def test_startwerte_unlokalisiert_ohne_inline_skript(self, abonnent: InsightSubscriber) -> None:
        html = Client().get(f"/insight/abo/verwalten/{abonnent.token}/").content.decode()
        assert 'x-data="neighborhoodSubscription"' in html
        assert 'data-lat="51.9606649"' in html
        assert 'data-lon="7.6261347"' in html
        assert 'data-radius="1000"' in html
        assert "function manageApp" not in html

    def test_lokalisierte_koordinaten_werden_gespeichert(self, abonnent: InsightSubscriber) -> None:
        antwort = Client().post(
            f"/insight/abo/verwalten/{abonnent.token}/",
            {
                "neighborhood_name": "Prinzipalmarkt",
                "neighborhood_lat": "51,9606649",
                "neighborhood_lon": "7,6261347",
                "neighborhood_radius": "1000",
                "keyword": "Radweg",
            },
        )
        assert antwort.status_code == 200
        abonnent.refresh_from_db()
        assert abonnent.neighborhood_active is True
        assert abonnent.neighborhood_lat == LAT
        assert abonnent.neighborhood_lon == LON
        assert abonnent.keyword == "Radweg"

    def test_unlesbare_koordinaten_heissen_kein_ort(self, abonnent: InsightSubscriber) -> None:
        antwort = Client().post(
            f"/insight/abo/verwalten/{abonnent.token}/",
            {"neighborhood_lat": "kaputt", "neighborhood_lon": "7.6", "keyword": "Radweg"},
        )
        assert antwort.status_code == 200
        abonnent.refresh_from_db()
        assert abonnent.neighborhood_active is False
        assert abonnent.neighborhood_lat is None
