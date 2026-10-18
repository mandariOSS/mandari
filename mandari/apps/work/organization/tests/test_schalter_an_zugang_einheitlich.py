# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_zugang_einheitlich``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 4)."""

from apps.work.organization.tests import test_zugang_einheitlich as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
