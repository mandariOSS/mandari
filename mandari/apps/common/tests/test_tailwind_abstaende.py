# SPDX-License-Identifier: AGPL-3.0-or-later
"""
space-y-* und divide-* wie in Tailwind 3 (Issue #1461, static/css/tailwind3-abstaende.css).

Tailwind 4 setzt den Abstand unten an jedes Kind außer dem letzten; die Vorlagen sind auf Tailwind 3 abgestimmt
(oben, ab dem zweiten sichtbaren Kind). Kommt eine neue Klasse dieser Art in eine Vorlage, erzeugt Tailwind sie in
der neuen Form; dieser Test fällt dann auf, bis sie in tailwind3-abstaende.css und in ``@source not inline`` von
input.css steht (oder die Vorlage flex/grid mit gap-* nutzt).
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings

STYLES = Path(settings.BASE_DIR) / "static" / "css" / "styles.css"


def test_styles_ohne_abstaende_nach_tailwind4() -> None:
    css = STYLES.read_text(encoding="utf-8")

    neu = re.findall(r":where\(([^{]*?)>:not\(:last-child\)\)", css)

    assert not neu, f"In tailwind3-abstaende.css und @source not inline (input.css) ergänzen: {neu}"
    assert ".space-y-8>:not([hidden])~:not([hidden])" in css
