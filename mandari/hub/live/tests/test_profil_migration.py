# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenmigration ``hub_live.0002_top_zuordnung`` (Teil von #47): Profile aus der alten Vorlage bekommen die neuen
Ausschnitte (Titel ab 0.545), angepasste Profile bleiben unverändert; idempotent, mit Rückweg.
"""

from __future__ import annotations

import copy
import importlib
from typing import Any

import pytest
from django.apps import apps

from hub.live.models import BroadcastSource
from hub.live.profil import lade_profil, vorlage
from hub.live.tests.conftest import Welt, kennung
from insight_core.models import OParlOrganization

pytestmark = pytest.mark.django_db

migration = importlib.import_module("hub.live.migrations.0002_top_zuordnung")


def _alt() -> dict[str, Any]:
    """Profil, wie die Vorlage es vor der Änderung gespeichert hat."""
    daten = vorlage("balken_unten_dreizeilig")
    daten["felder"]["top"]["box"] = [0.36, 0.87, 0.555, 0.94]
    daten["felder"]["titel"]["box"] = [0.555, 0.82, 1.0, 0.955]
    daten["fraktionen"] = ["Fraktion A"]
    return daten


def _boxen(quelle: BroadcastSource) -> tuple[list[float], list[float]]:
    quelle.refresh_from_db()
    felder = quelle.overlay_profile["felder"]
    return felder["top"]["box"], felder["titel"]["box"]


def test_profile_aus_der_alten_vorlage_werden_nachgezogen(welt: Welt) -> None:
    alt = welt.quelle
    alt.overlay_profile = _alt()
    alt.save()
    eigen_profil = copy.deepcopy(_alt())
    eigen_profil["felder"]["titel"]["box"] = [0.5, 0.82, 1.0, 0.955]
    gremium = OParlOrganization.objects.create(external_id=kennung("organizations"), body=welt.body, name="Ausschuss")
    eigen = BroadcastSource.objects.create(
        body=welt.body, organization=gremium, provider="3q", identifier="x", overlay_profile=eigen_profil
    )

    migration.vorwaerts(apps, None)
    assert _boxen(alt) == ([0.36, 0.87, 0.545, 0.94], [0.545, 0.82, 1.0, 0.955])
    assert alt.overlay_profile["fraktionen"] == ["Fraktion A"], "übrige Angaben bleiben"
    assert "zeichen" not in alt.overlay_profile["felder"]["top"], "ältere Images lesen das Profil weiter"
    lade_profil(alt.overlay_profile)
    assert _boxen(eigen)[1] == [0.5, 0.82, 1.0, 0.955] and eigen.overlay_profile == eigen_profil, "angepasst: bleibt"

    migration.vorwaerts(apps, None)
    assert _boxen(alt)[1] == [0.545, 0.82, 1.0, 0.955], "idempotent"

    migration.rueckwaerts(apps, None)
    assert _boxen(alt) == ([0.36, 0.87, 0.555, 0.94], [0.555, 0.82, 1.0, 0.955])
    assert _boxen(eigen)[1] == [0.5, 0.82, 1.0, 0.955]
