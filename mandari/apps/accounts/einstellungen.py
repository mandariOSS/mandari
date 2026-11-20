# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``User.settings`` ändern, ohne gleichzeitige Änderungen derselben Person zu verlieren (Issue #1472).

Das Feld ist ein JSON-Objekt mit Angaben aus mehreren Bereichen (gesehene Neuigkeiten, zurückgestellte Hinweise,
Sichtbarkeit im Profil). Wer es liest, eine Kopie ändert und ganz speichert, überschreibt, was eine zweite Anfrage
derselben Person dazwischen geschrieben hat. Deshalb: Zeile sperren, frisch lesen, ändern, speichern.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction


def einstellungen_aendern(user: Any, aendern: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """
    ``aendern`` auf den gesperrten, frisch gelesenen Stand von ``user.settings`` anwenden und speichern.

    ``aendern`` bekommt eine Kopie und ändert sie an Ort und Stelle. Danach trägt auch ``user`` den gespeicherten
    Stand. Gibt den gespeicherten Stand zurück.
    """
    with transaction.atomic():
        # Nicht type(user): In Ansichten ist request.user ein SimpleLazyObject
        gesperrt = get_user_model().objects.select_for_update().only("pk", "settings").get(pk=user.pk)
        einstellungen = dict(gesperrt.settings) if isinstance(gesperrt.settings, dict) else {}
        aendern(einstellungen)
        gesperrt.settings = einstellungen
        gesperrt.save(update_fields=["settings"])
    user.settings = einstellungen
    return einstellungen


def einstellung_setzen(user: Any, schluessel: str, wert: Any) -> None:
    """Einen Schlüssel in ``user.settings`` setzen; die übrigen bleiben, wie sie in der Datenbank stehen."""

    def setzen(einstellungen: dict[str, Any]) -> None:
        einstellungen[schluessel] = wert

    einstellungen_aendern(user, setzen)
