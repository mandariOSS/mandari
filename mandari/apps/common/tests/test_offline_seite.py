# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Offline-Seite der PWA: eigenständig, Inline-Code nur mit Nonce (#172).

Die Seite wird vom Service Worker vorgecacht und muss ohne weitere Anfragen rendern; Skript und
Style bleiben deshalb inline (Template unter ``pwa/``, Allowlist des Frontend-Ratchets). Beide
tragen den Nonce, der auch im CSP-Kopf der Antwort steht.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


def test_offline_seite_mit_nonce() -> None:
    antwort = Client().get("/offline/")
    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert "Du bist offline" in html
    assert "/static/" not in html

    kopf = antwort.headers.get("Content-Security-Policy-Report-Only", "")
    treffer = re.search(r"'nonce-([^']+)'", kopf)
    assert treffer, kopf
    nonce = treffer.group(1)
    assert f'<style nonce="{nonce}">' in html
    assert f'<script nonce="{nonce}">' in html
