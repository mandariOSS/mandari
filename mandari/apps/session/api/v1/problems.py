# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fehlerformat nach RFC 9457 („Problem Details for HTTP APIs“).

Jede Fehlerantwort der API v1 ist ``application/problem+json`` mit ``type``, ``title``, ``status``,
``detail``, ``instance`` und der Erweiterung ``request_id`` (Korrelation mit den Logs, siehe
``apps.common.observability``). Validierungsfehler tragen zusätzlich ``errors`` (Pydantic-Details).

Format und Handler sind die gemeinsamen Bausteine aller Schnittstellen (``apps.common.api``); hier stehen
nur Bereich und Meldung einer fehlenden Anmeldung.
"""

from __future__ import annotations

from ninja import NinjaAPI

from apps.common.api.ninja import install_problem_handlers as _install
from apps.common.api.problems import ProblemError as Problem

__all__ = ["Problem", "install_problem_handlers"]

REALM = "mandari Session-API"
UNAUTHENTICATED = "Gültiges API-Token erforderlich (Authorization: Bearer <token>) oder angemeldete Sitzung."


def install_problem_handlers(api: NinjaAPI) -> None:
    """Registriert die RFC-9457-Handler für alle Fehlerklassen der API."""
    _install(api, realm=REALM, unauthenticated=UNAUTHENTICATED)
