# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auswahllisten im dunklen Modus lesbar (Issue #1568, static/css/tailwind-grundregeln.css).

Ohne ``color-scheme`` öffnet der Browser die Liste eines ``<select>`` im dunklen Modus hell, die Einträge erben aber
die helle Schrift des Felds: weiß auf weiß, nur der markierte Eintrag ist zu sehen. Der Test hält fest, dass das
gebaute Stylesheet (Work, Session, Insight, Konto) das Farbschema setzt und den Einträgen eigene Farben gibt.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings

STYLES = Path(settings.BASE_DIR) / "static" / "css" / "styles.css"


def test_farbschema_und_auswahllisten() -> None:
    css = STYLES.read_text(encoding="utf-8")

    assert ":root{color-scheme:light}" in css
    assert ".dark{color-scheme:dark}" in css
    assert "select option,select optgroup{background-color:var(--color-white);color:var(--color-gray-900)}" in css
    assert ".dark select option,.dark select optgroup{background-color:var(--color-gray-800)" in css
