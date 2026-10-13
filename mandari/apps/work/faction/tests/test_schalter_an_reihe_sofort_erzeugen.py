# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_reihe_sofort_erzeugen``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 3 Einstellungen)."""

from apps.work.faction.tests import test_reihe_sofort_erzeugen as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
