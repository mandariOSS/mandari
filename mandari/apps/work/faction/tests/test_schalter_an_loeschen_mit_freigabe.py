# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_loeschen_mit_freigabe``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 3 Einstellungen)."""

from apps.work.faction.tests import test_loeschen_mit_freigabe as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
