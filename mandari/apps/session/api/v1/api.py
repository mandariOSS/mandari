# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``NinjaAPI``-Instanz der Session-API v1.

Eingehängt in ``mandari/urls.py`` unter ``/api/v1/session/``. OpenAPI-Dokument unter
``/api/v1/session/openapi.json``, Swagger UI unter ``/api/v1/session/docs`` (lokale Assets aus dem
Paket ``ninja``, kein CDN).
"""

from __future__ import annotations

from ninja import NinjaAPI

from .endpoints import router
from .problems import install_problem_handlers

DESCRIPTION = """
Erweiterte, authentifizierte Schnittstelle des Session-RIS je Mandant (Kommune).

- Öffentliche Daten sind anonym abrufbar; nicht-öffentliche Daten erfordern eine angemeldete Sitzung
  mit passenden Rechten oder ein API-Token mit den entsprechenden Flags.
- Anträge einreichen: `POST /{tenant_slug}/applications/submit/` mit `Authorization: Bearer <token>`.
- Fehler kommen als `application/problem+json` (RFC 9457) mit `request_id` zur Korrelation.
- Rein öffentliche OParl-1.1-Daten liefert weiterhin `/session/<slug>/api/oparl/`.
"""

api = NinjaAPI(
    title="mandari Session-API",
    version="1",
    description=DESCRIPTION.strip(),
    urls_namespace="session_api_v1",
)
install_problem_handlers(api)
api.add_router("", router)
