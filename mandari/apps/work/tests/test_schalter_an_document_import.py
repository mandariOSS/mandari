# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_document_import``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 1 Dokumente)."""

from apps.work.tests import test_document_import as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals())
