# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_formulare_ohne_inline_skript``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 3 Einstellungen)."""

from apps.work.motions.tests import test_formulare_ohne_inline_skript as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
