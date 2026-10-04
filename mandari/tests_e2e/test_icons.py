# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lucide-Icons im Browser: Teilmenge im Haupt-Bundle, vollständiger Satz nur bei Bedarf (frontend/js/icons.ts).

Das Haupt-Bundle enthält nur die Icons, deren Namen im Projekt vorkommen (frontend/vite/lucide-icons.ts).
Ein unbekannter Name lädt den vollständigen Satz einmal nach und wird dann gezeichnet.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.django_db(transaction=True)

NACHLADEN = "icons-alle"


def test_bekannte_icons_ohne_nachladen_unbekannte_mit(page: Any, goto: Any) -> None:
    nachgeladen: list[str] = []
    page.on("request", lambda anfrage: nachgeladen.append(anfrage.url) if NACHLADEN in anfrage.url else None)

    goto("/accounts/login/")
    page.wait_for_load_state("networkidle")
    assert page.locator("svg.lucide").count() > 0
    assert not nachgeladen, "Die Icons der Anmeldeseite stehen im Haupt-Bundle"

    # Ein Name, den kein Template nennt: erst leeres SVG, nach dem Nachladen das Icon
    page.evaluate(
        """() => {
            const box = document.createElement('div')
            box.id = 'icon-probe'
            box.innerHTML = '<i data-lucide="zodiac-leo" class="w-4 h-4"></i>'
            document.body.appendChild(box)
        }"""
    )
    page.wait_for_function(
        "() => document.querySelector('#icon-probe svg.lucide-zodiac-leo')?.childElementCount > 0", timeout=10_000
    )
    assert len(nachgeladen) == 1
