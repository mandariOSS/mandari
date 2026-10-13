# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_standard_tagesordnung``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 3 Einstellungen)."""

from apps.work.faction.tests import test_standard_tagesordnung as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
