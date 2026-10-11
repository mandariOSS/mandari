# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_freigabe_grenzen``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 1 Dokumente)."""

from apps.work.motions.tests import test_freigabe_grenzen as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
