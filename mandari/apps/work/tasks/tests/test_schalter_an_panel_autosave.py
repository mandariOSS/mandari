# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_panel_autosave``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 4)."""

from apps.work.tasks.tests import test_panel_autosave as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
