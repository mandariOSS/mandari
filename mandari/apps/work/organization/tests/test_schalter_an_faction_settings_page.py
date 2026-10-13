# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wie ``test_faction_settings_page``, mit eingeschaltetem neuen Erscheinungsbild (Issue #951, Teilpaket 3 Einstellungen)."""

from apps.work.organization.tests import test_faction_settings_page as quelle
from apps.work.tests.schalter_an import uebernehmen

# Prüft Partials und Markup der bisherigen Vorlage; die neue prüft test_einstellungen_3c.test_fraktion_vorschau_ohne_inline_skript
uebernehmen(quelle, globals(), ohne=("test_faction_settings_page_uses_alpine_preview_without_inline_script",))
