# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bestehende Testmodule mit eingeschaltetem neuen Erscheinungsbild wiederholen (Issue #951).

Seiten, die im neuen Rahmen eine eigene Vorlage bekommen, müssen dieselben Prüfungen bestehen wie bisher: Rechte,
Sichtbarkeit, Formulare, Texte. Ein Wiederholungsmodul übernimmt Tests und Fixtures eines bestehenden Moduls und
ersetzt die Fixture ``org`` durch eine Organisation mit eingeschaltetem Schalter (wie in Produktion seit 06.10.2026):

    from apps.work.motions.tests import test_einreichung_per_email as quelle
    from apps.work.tests.schalter_an import uebernehmen

    uebernehmen(quelle, globals())

Tests, die bewusst Markup der bisherigen Vorlage prüfen, lassen sich mit ``ohne=("test_…",)`` auslassen; die neue
Vorlage prüft dann ein eigener Test.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
from _pytest.fixtures import FixtureFunctionDefinition


@pytest.fixture
def org(org: Any) -> Any:
    """Organisation aus der conftest mit eingeschaltetem neuen Erscheinungsbild."""
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


def _ist_fixture(wert: Any) -> bool:
    return isinstance(wert, FixtureFunctionDefinition)


def uebernehmen(quelle: ModuleType, ziel: dict[str, Any], ohne: tuple[str, ...] = ()) -> None:
    """Tests, Testklassen, Fixtures (auch autouse) und Markierungen aus ``quelle`` in den Namensraum ``ziel``."""
    for name, wert in vars(quelle).items():
        if name in ohne or name == "org":
            continue
        if name == "pytestmark" or name.startswith(("test_", "Test")) or _ist_fixture(wert):
            ziel[name] = wert
    for name in ohne:
        assert name in vars(quelle), f"{quelle.__name__} hat keinen Test {name}"
    ziel["org"] = org
