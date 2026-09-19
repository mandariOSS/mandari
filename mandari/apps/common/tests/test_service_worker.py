# SPDX-License-Identifier: AGPL-3.0-or-later
"""Service Worker: Die Vorlage ist gültiges JavaScript (CodeQL), die Auslieferung rendert alle Tags."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.test import Client

VORLAGE = Path(__file__).resolve().parents[3] / "templates" / "pwa" / "sw.js"


def test_template_tags_nur_in_kommentaren_oder_strings() -> None:
    for nummer, zeile in enumerate(VORLAGE.read_text(encoding="utf-8").splitlines(), start=1):
        if "{%" not in zeile and "{{" not in zeile:
            continue
        code = zeile.strip()
        ohne_strings = re.sub(r"'[^']*'|\"[^\"]*\"", "''", code)
        assert code.startswith("//") or ("{%" not in ohne_strings and "{{" not in ohne_strings), (
            f"sw.js Zeile {nummer}: Template-Tag außerhalb von Kommentar oder String"
        )


@pytest.mark.django_db
def test_auslieferung_ohne_template_reste() -> None:
    antwort = Client().get("/sw.js")
    assert antwort.status_code == 200
    assert antwort["Content-Type"].startswith("text/javascript")
    inhalt = antwort.content.decode()
    assert "{%" not in inhalt and "{{" not in inhalt
    assert re.search(r"const CACHE_VERSION = '[^']+';", inhalt)
