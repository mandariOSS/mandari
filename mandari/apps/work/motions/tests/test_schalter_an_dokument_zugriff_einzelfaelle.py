# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_dokument_zugriff_einzelfaelle``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 1 Dokumente)."""

from apps.work.motions.tests import test_dokument_zugriff_einzelfaelle as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
