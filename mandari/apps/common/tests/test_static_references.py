# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Jede statische Datei, die ein Template per ``{% static '…' %}`` referenziert, muss existieren.

Hintergrund: In Produktion (Manifest-Storage) wirft ein fehlender Eintrag beim Rendern einen
500er; lokal (DEBUG) fällt das nicht auf. Nach dem Entfernen der Vendor-Kopien (#169) blieben
zwei Verweise stehen, die erst im CI aufgefallen sind.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders

TEMPLATE_DIRS = [Path(__file__).resolve().parents[3] / "templates"]
STATIC_RE = re.compile(r"{%\s*static\s+['\"]([^'\"{}]+)['\"]\s*%}")
#: Verweise, die zur Laufzeit erzeugt werden (Vite-Manifest, Sammelpfade)
IGNORED_PREFIXES = ("dist/",)


def _references() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for base in TEMPLATE_DIRS:
        for path in base.rglob("*.html"):
            for match in STATIC_RE.finditer(path.read_text(encoding="utf-8", errors="ignore")):
                target = match.group(1)
                if not target.startswith(IGNORED_PREFIXES):
                    found.append((str(path.relative_to(base)).replace("\\", "/"), target))
    return sorted(set(found))


@pytest.mark.parametrize(("template", "target"), _references(), ids=lambda v: v if "/" in v and "." in v else None)
def test_static_reference_exists(template: str, target: str) -> None:
    assert finders.find(target), f"{template} verweist auf fehlende statische Datei {target!r}"
