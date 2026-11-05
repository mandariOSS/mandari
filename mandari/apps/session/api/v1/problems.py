# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fehlerformat nach RFC 9457 („Problem Details for HTTP APIs“).

Jede Fehlerantwort der API v1 ist ``application/problem+json`` mit ``type``, ``title``, ``status``,
``detail``, ``instance`` und der Erweiterung ``request_id`` (Korrelation mit den Logs, siehe
``apps.common.observability``). Validierungsfehler tragen zusätzlich ``errors`` (Pydantic-Details).

Format und Handler sind die gemeinsamen Bausteine aller Schnittstellen (``apps.common.api``); hier stehen
Bereich und Meldung einer fehlenden Anmeldung und die Titel je Status. Die API v1 behält ihre Tabelle aus
#163: Die gemeinsame kennt mehr Status (etwa 501 und 503 aus dem Befehl ``submission.submit``), und eine
veröffentlichte Version ändert ihre Antworten nicht. Andere Status heißen weiter „Fehler“.
"""

from __future__ import annotations

from typing import Final

from ninja import NinjaAPI

from apps.common.api.ninja import install_problem_handlers as _install
from apps.common.api.problems import ProblemError as Problem

__all__ = ["TITLES", "Problem", "install_problem_handlers"]

REALM = "mandari Session-API"
UNAUTHENTICATED = "Gültiges API-Token erforderlich (Authorization: Bearer <token>) oder angemeldete Sitzung."

TITLES: Final[dict[int, str]] = {
    400: "Ungültige Anfrage",
    401: "Nicht authentifiziert",
    403: "Keine Berechtigung",
    404: "Nicht gefunden",
    405: "Methode nicht erlaubt",
    409: "Konflikt",
    422: "Validierung fehlgeschlagen",
    429: "Zu viele Anfragen",
    500: "Interner Fehler",
}


def install_problem_handlers(api: NinjaAPI) -> None:
    """Registriert die RFC-9457-Handler für alle Fehlerklassen der API."""
    _install(api, realm=REALM, unauthenticated=UNAUTHENTICATED, titles=TITLES)
