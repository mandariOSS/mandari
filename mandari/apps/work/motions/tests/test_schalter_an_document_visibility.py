# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_document_visibility``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 1 Dokumente)."""

from apps.work.motions.tests import test_document_visibility as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
