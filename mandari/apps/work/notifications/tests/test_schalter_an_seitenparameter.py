# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_seitenparameter``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 4)."""

from apps.work.notifications.tests import test_seitenparameter as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
