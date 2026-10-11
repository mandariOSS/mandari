# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_aenderungsantrag_sichtbarkeit``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 1 Dokumente)."""

from apps.work.motions.tests import test_aenderungsantrag_sichtbarkeit as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
