# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_hilfe``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 4)."""

from apps.common.tests import test_hilfe as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
