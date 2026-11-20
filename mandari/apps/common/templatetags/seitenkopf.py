# SPDX-License-Identifier: AGPL-3.0-or-later
"""Angaben für den Seitenkopf öffentlicher Seiten außerhalb von Insight (Anmeldung)."""

from __future__ import annotations

from django import template
from django.conf import settings
from django.urls import reverse

register = template.Library()


@register.simple_tag
def kanonische_adresse(name: str) -> str:
    """Kanonische Adresse einer benannten Seite, aus ``SITE_URL`` und nie aus dem Host-Header.

    Die Anmeldeseite ist unter vielen Adressen erreichbar (``?next=…``); Suchmaschinen führen sie so als eine Seite.
    """
    return str(settings.SITE_URL).rstrip("/") + reverse(name)
