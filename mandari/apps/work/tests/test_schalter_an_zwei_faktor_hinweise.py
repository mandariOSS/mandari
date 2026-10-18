# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_zwei_faktor_hinweise``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 4)."""

from apps.work.tests import test_zwei_faktor_hinweise as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
