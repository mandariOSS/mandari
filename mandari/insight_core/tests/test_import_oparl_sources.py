# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``import_oparl_sources``: die eine Liste bekannter Quellen (Issue #555).

Neu angelegte Quellen sind immer inaktiv. Vorhandene bleiben unverändert: Ob eine Quelle abgeschaltet ist und welche
Einstellungen sie hat, steht in der Datenbank (etwa bei gesperrten oder gedrosselten Servern), kein Import darf das
ändern.
"""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from insight_core.management.commands.import_oparl_sources import OPARL_SOURCES
from insight_core.models import OParlSource

pytestmark = pytest.mark.django_db

ABGESCHALTET = "https://buergerinfo.stadt-koeln.de/oparl/system"
GEDROSSELT = "https://www.bonn.sitzung-online.de/public/oparl/system"
ADRESSEN = [url for _, url in OPARL_SOURCES]

#: Bis Issue #555 nur in der Liste des Ingestors (``ingestor/src/sources.py``)
AUS_DEM_INGESTOR = {
    "https://bochum.ratsinfomanagement.net/webservice/oparl/v1.1/system",
    "https://hamm.ratsinfomanagement.net/webservice/oparl/v1.1/system",
    "https://ris.kleve.de/webservice/oparl/v1.1/system",
    "https://ris.moers.de/webservice/oparl/v1.1/system",
    "https://bezreg-koeln.ratsinfomanagement.net/webservice/oparl/v1.1/system",
}


def _importieren(*argumente: str) -> str:
    ausgabe = StringIO()
    call_command("import_oparl_sources", *argumente, stdout=ausgabe)
    return ausgabe.getvalue()


def _vorhandene_quellen() -> tuple[OParlSource, OParlSource]:
    return (
        OParlSource.objects.create(
            name="Name aus der Datenbank",
            url=ABGESCHALTET,
            is_active=False,
            sync_config={"file_downloads": False, "full_sync_window": {"start": 1, "hours": 5}},
        ),
        OParlSource.objects.create(
            name="Stadt Bonn",
            url=GEDROSSELT,
            is_active=True,
            sync_config={"request_interval": 1.0, "file_downloads": False},
        ),
    )


def _zeilen(*adressen: str) -> dict[str, dict[str, Any]]:
    return {zeile["url"]: zeile for zeile in OParlSource.objects.filter(url__in=adressen).values()}


def test_liste_enthaelt_jede_adresse_einmal() -> None:
    assert len(ADRESSEN) == len(set(ADRESSEN)) == 111
    assert set(ADRESSEN) >= AUS_DEM_INGESTOR


def test_neue_quellen_sind_inaktiv() -> None:
    ausgabe = _importieren()

    angelegt = OParlSource.objects.filter(url__in=ADRESSEN)
    assert angelegt.count() == len(ADRESSEN)
    assert not angelegt.filter(is_active=True).exists()
    assert "is_active=False" in ausgabe


def test_import_aendert_vorhandene_quellen_nicht() -> None:
    _vorhandene_quellen()
    vorher = _zeilen(ABGESCHALTET, GEDROSSELT)

    _importieren()

    assert _zeilen(ABGESCHALTET, GEDROSSELT) == vorher


def test_update_aendert_nur_den_namen() -> None:
    _vorhandene_quellen()
    vorher = _zeilen(ABGESCHALTET, GEDROSSELT)

    _importieren("--update")

    nachher = _zeilen(ABGESCHALTET, GEDROSSELT)
    assert nachher[ABGESCHALTET]["name"] == "Stadt Köln"
    for adresse, zeile in vorher.items():
        ohne_namen = {feld: wert for feld, wert in zeile.items() if feld not in {"name", "updated_at"}}
        assert {feld: nachher[adresse][feld] for feld in ohne_namen} == ohne_namen


def test_activate_gibt_es_nicht() -> None:
    with pytest.raises(CommandError):
        call_command("import_oparl_sources", "--activate", stdout=StringIO())

    assert not OParlSource.objects.filter(url__in=ADRESSEN).exists()
