# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_reihe_automatik_einstellungen``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 3 Einstellungen)."""

from apps.work.organization.tests import test_reihe_automatik_einstellungen as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
