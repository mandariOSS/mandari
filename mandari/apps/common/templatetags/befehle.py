# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Template-Tags für Formulare, die einen Befehl auslösen (Befehlsschicht, Issue #1588).

``{% idempotenzschluessel %}`` setzt ein verstecktes Feld mit einem neuen Idempotenzschlüssel. Schickt jemand das
Formular doppelt ab (Doppelklick, Neuladen nach dem Senden), wirkt der Befehl nur einmal: Die zweite Anfrage erhält
dieselbe Quittung (``hub.commands.oberflaeche.ausfuehren``). Nur für Formulare, nach denen die Seite neu lädt; ein
Formular, das ohne Neuladen mehrfach sendet, braucht je Senden einen neuen Schlüssel und lässt das Feld weg.
"""

from __future__ import annotations

import uuid

from django import template
from django.utils.html import format_html
from django.utils.safestring import SafeString

from apps.common.api.idempotency import FORM_FIELD

register = template.Library()


@register.simple_tag
def idempotenzschluessel() -> SafeString:
    """Verstecktes Feld mit einem neuen Idempotenzschlüssel für das Formular."""
    return format_html('<input type="hidden" name="{}" value="{}">', FORM_FIELD, uuid.uuid4())
