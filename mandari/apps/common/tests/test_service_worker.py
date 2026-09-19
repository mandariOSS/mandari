# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Service Worker: Die Datei ist reines JavaScript ohne Template-Syntax (CodeQL konnte die frühere
Django-Vorlage nicht parsen); die Auslieferung setzt Cache-Version, Offline-URL und Kern-Assets ein.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from django.test import Client

QUELLE = Path(__file__).resolve().parents[3] / "templates" / "pwa" / "sw.js"


def test_quelle_ohne_template_syntax() -> None:
    text = QUELLE.read_text(encoding="utf-8")
    assert "{%" not in text and "{{" not in text
    assert text.count("// __SW_CONFIG__") == 1


@pytest.mark.django_db
def test_auslieferung_mit_konfiguration() -> None:
    antwort = Client().get("/sw.js")
    assert antwort.status_code == 200
    assert antwort["Content-Type"].startswith("text/javascript")
    inhalt = antwort.content.decode()
    assert "__SW_CONFIG__" not in inhalt
    treffer = re.search(r"^const SW_CONFIG = (\{.*\});$", inhalt, re.MULTILINE)
    assert treffer, "Konfigurationszeile fehlt"
    konfiguration = json.loads(treffer.group(1))
    assert konfiguration["cacheVersion"] and konfiguration["offlineUrl"].startswith("/")
    assert len(konfiguration["precache"]) == 3
    assert all(pfad.startswith("/static/") for pfad in konfiguration["precache"])
