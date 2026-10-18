# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_list_page``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 1 Dokumente)."""

from apps.work.motions.tests import test_list_page as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals(), ohne=("test_list_page_provides_json_config_and_no_inline_script",))
