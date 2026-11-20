# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungen, deren Gremium die Quelle nennt, aber nicht öffentlich ausgibt (Issue #1441).

Manche Quellen sperren einzelne Gremien (HTTP 401 auf das Objekt, nicht in der Gremienliste). Die Sitzung verweist
dann auf ein Gremium, das im Bestand fehlt. Hieß sie nur „Sitzung“, stand dieses Wort überall als Gremienname da.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from insight_core.models import (
    GREMIUM_NICHT_VEROEFFENTLICHT,
    OParlBody,
    OParlMeeting,
    OParlOrganization,
    OParlSource,
)

pytestmark = pytest.mark.django_db

RIS = "https://ris.example.org"


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
    return OParlBody.objects.create(source=source, external_id=f"{RIS}/body/1", name="Musterstadt")


def sitzung(body: OParlBody, nr: int, name: str | None, gremien: list[str]) -> OParlMeeting:
    return OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/{nr}",
        body=body,
        name=name,
        start=datetime(2026, 12, 10, 17, 0, tzinfo=UTC),
        raw_json={"organization": gremien} if gremien else {},
    )


def test_anzeigename_einer_sitzung_mit_nicht_veroeffentlichtem_gremium(body: OParlBody) -> None:
    gesperrt = sitzung(body, 1, "Sitzung", [f"{RIS}/organization/901"])
    assert (
        gesperrt.get_display_name() == GREMIUM_NICHT_VEROEFFENTLICHT == "Sitzung eines nicht veröffentlichten Gremiums"
    )
    assert sitzung(body, 2, None, [f"{RIS}/organization/902"]).get_display_name() == GREMIUM_NICHT_VEROEFFENTLICHT

    # Ein eigener Name der Sitzung geht vor
    assert sitzung(body, 3, "Aufsichtsrat Stadtwerke", [f"{RIS}/organization/903"]).get_display_name() == (
        "Aufsichtsrat Stadtwerke"
    )
    # Ohne Verweis der Quelle bleibt es bei „Sitzung“
    assert sitzung(body, 4, "Sitzung", []).get_display_name() == "Sitzung"


def test_gremium_im_bestand_gilt_als_veroeffentlicht(body: OParlBody) -> None:
    rat = OParlOrganization.objects.create(external_id=f"{RIS}/organization/1", body=body, name="Rat")
    # Über die Zuordnung
    zugeordnet = sitzung(body, 5, "Sitzung", [rat.external_id])
    zugeordnet.organizations.add(rat)
    assert zugeordnet.get_display_name() == "Rat"
    # Nur über den Verweis in den Rohdaten
    assert sitzung(body, 6, "Sitzung", [rat.external_id]).get_display_name() == "Rat"
    # Im Bestand, aber ohne Namen: kein gesperrtes Gremium
    namenlos = OParlOrganization.objects.create(external_id=f"{RIS}/organization/2", body=body, name="")
    assert sitzung(body, 7, "Sitzung", [namenlos.external_id]).get_display_name() == "Sitzung"


def test_name_aus_dem_kalender_der_kommune(body: OParlBody) -> None:
    """Der Abruf liest den Namen eines gesperrten Gremiums aus dem öffentlichen Sitzungskalender (Münster)."""
    gesperrt = sitzung(body, 8, "Sitzung", [f"{RIS}/organization/152"])
    gesperrt.raw_json["mandari:gremiumWeb"] = {
        "name": "Interfraktionelle Arbeitsgruppe Internationales",
        "seite": f"{RIS}/si0040.php?__cjahr=2026&__cmonat=12",
        "abgerufen": "2026-11-19T17:00:00+00:00",
    }
    gesperrt.save(update_fields=["raw_json"])
    assert gesperrt.get_display_name() == "Interfraktionelle Arbeitsgruppe Internationales"
    # Unlesbare Angabe: wieder der Platzhalter
    gesperrt.raw_json["mandari:gremiumWeb"] = "kaputt"
    assert gesperrt.get_display_name() == GREMIUM_NICHT_VEROEFFENTLICHT
    # Ein Gremium im Bestand geht weiter vor
    rat = OParlOrganization.objects.create(external_id=f"{RIS}/organization/3", body=body, name="Rat")
    zugeordnet = sitzung(body, 9, "Sitzung", [rat.external_id])
    zugeordnet.raw_json["mandari:gremiumWeb"] = {"name": "Falscher Name"}
    assert zugeordnet.get_display_name() == "Rat"
