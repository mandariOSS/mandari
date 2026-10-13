# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wie ``test_board_page``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 4).

Ausgelassen: ``test_board_page_provides_json_config_and_no_inline_script`` prüft die Teilvorlagen der bisherigen Seite
(work/tasks/partials/_board_filters.html, _kanban_column.html, _panel_shell.html). Dieselben Prüfungen für die neue
Seite (Konfiguration, Komponenten, kein Inline-Skript, Teilvorlagen) stehen in
``apps/work/tests/test_neues_design_aufgaben.py::test_board_konfiguration_komponenten_und_kein_inline_skript``.
"""

from apps.work.tasks.tests import test_board_page as quelle
from apps.work.tests.schalter_an import uebernehmen

uebernehmen(quelle, globals(), ohne=("test_board_page_provides_json_config_and_no_inline_script",))
