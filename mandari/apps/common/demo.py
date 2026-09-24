# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Demo-Instanz (Issue #99).

Die Demo läuft als eigene Installation mit eigener Datenbank und nur synthetischen Daten
(``setup_demo_environment``); die Zugangsdaten stehen öffentlich auf der Website. Der Schalter
``DEMO_INSTANCE`` macht aus einer normalen Installation diese Demo:

- Kein Mailversand: jedes Backend wird zum Speicher-Backend (``mail_backends.build_backend``).
- Keine Aufrufe an frei eintragbare Adressen: KI-Funktionen sind abgeschaltet.
- Besucher dürfen das gemeinsame Konto nicht kapern: Passwort, zweiter Faktor,
  Sicherheitsschlüssel, Registrierung, „Passwort vergessen“ und Kontolöschung sind gesperrt.
- Ein Hinweis auf jeder Seite sagt, dass alles erfunden ist und nachts zurückgesetzt wird.

Außerhalb der Demo-Instanz ist nichts davon aktiv.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect

#: Seiten, deren POST in der Demo gesperrt ist (``namespace:name``).
GESPERRT_IN_DER_DEMO = frozenset(
    {
        "work:security",  # Passwort, zweiter Faktor, Sitzungen
        "work:profile_data",  # Datenauskunft und Kontolöschung
        "accounts:two_factor_enroll",
        "accounts:security_keys",
        "accounts:webauthn_register_options",
        "accounts:webauthn_register",
        "accounts:register",
        "accounts:self_register",
        "accounts:self_register_confirm",
        "accounts:password_reset",
        "accounts:password_reset_confirm",
    }
)

HINWEIS_GESPERRT = (
    "In der Demo-Umgebung nicht möglich: Alle Besucher teilen sich dieses Konto. "
    "Passwort, zweiter Faktor und Konto lassen sich deshalb nicht ändern."
)


def ist_demo() -> bool:
    return bool(getattr(settings, "DEMO_INSTANCE", False))


class DemoInstanceMiddleware:
    """Weist in der Demo-Instanz POST-Anfragen an gesperrte Seiten mit einem Hinweis ab."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_view(
        self, request: HttpRequest, view_func: Any, view_args: Any, view_kwargs: Any
    ) -> HttpResponse | None:
        if not ist_demo() or request.method != "POST":
            return None
        match = getattr(request, "resolver_match", None)
        if match is None or match.view_name not in GESPERRT_IN_DER_DEMO:
            return None
        messages.warning(request, HINWEIS_GESPERRT)
        return HttpResponseRedirect(request.get_full_path())


def demo_context(request: HttpRequest) -> dict[str, bool]:
    """Kontextprozessor: ``demo_instance`` für den Hinweis in den Basis-Templates."""
    return {"demo_instance": ist_demo()}


def demo_passwort() -> str | None:
    """Festes Passwort für die Demo-Konten – nur in der Demo-Instanz und nur, wenn gesetzt.

    Die öffentliche Demo braucht Zugangsdaten, die auf der Website stehen können
    (``DEMO_PASSWORD``). Außerhalb der Demo-Instanz gibt es nie ein festes Passwort: Dort
    erzeugen ``setup_demo_environment`` und ``setup_demo_praesentation`` bei jedem Lauf neue.
    """
    import os

    passwort = os.environ.get("DEMO_PASSWORD", "").strip()
    if not ist_demo() or not passwort:
        return None
    return passwort
