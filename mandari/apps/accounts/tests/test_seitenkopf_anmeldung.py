# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Seitenkopf der Anmeldeseite für Suchmaschinen (Issue #939).

Die Bing Webmaster Tools meldeten „Anmelden | mandari“ (18 Zeichen) und „mandari - Anmeldung“ (19 Zeichen) als zu
kurz. Die Anmeldeseite trägt einen aussagekräftigen Titel, eine Beschreibung und eine kanonische Adresse; die übrigen
Kontoseiten stehen nicht im Suchindex.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any

import pytest
from django.test import override_settings
from django.urls import reverse


def _kopf(html: str, muster: str) -> str:
    treffer = re.search(muster, html, re.S)
    return unescape(treffer.group(1).strip()) if treffer else ""


@pytest.mark.django_db
@override_settings(SITE_URL="https://mandari.example/", ALLOWED_HOSTS=["testserver", "anderer-host.example"])
def test_anmeldeseite_mit_titel_beschreibung_und_kanonischer_adresse(client: Any) -> None:
    antwort = client.get(reverse("accounts:login"), {"next": "/work/"}, HTTP_HOST="anderer-host.example")

    assert antwort.status_code == 200
    html = antwort.content.decode()
    titel = _kopf(html, r"<title>(.*?)</title>")
    beschreibung = _kopf(html, r'<meta name="description" content="([^"]*)"')
    assert titel == "Anmelden bei Work und Session | mandari"
    assert 30 <= len(titel) <= 70
    assert 120 <= len(beschreibung) <= 160, len(beschreibung)
    assert "Work" in beschreibung and "Session" in beschreibung
    # Aus SITE_URL, ohne Parameter und unabhängig vom Host der Anfrage
    assert _kopf(html, r'<link rel="canonical" href="([^"]*)"') == "https://mandari.example/accounts/login/"
    assert _kopf(html, r'<meta name="robots" content="([^"]*)"') == "index, follow"


@pytest.mark.django_db
def test_uebrige_kontoseiten_stehen_nicht_im_suchindex(client: Any) -> None:
    antwort = client.get(reverse("accounts:password_reset"))

    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert _kopf(html, r'<meta name="robots" content="([^"]*)"') == "noindex, follow"
    assert 'rel="canonical"' not in html
