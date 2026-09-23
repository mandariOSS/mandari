# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Common middleware for Mandari.

Includes error handling for database connection issues.
"""

import logging
from collections.abc import Callable

from django.db import OperationalError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.template.loader import render_to_string

from apps.common.db_connections import is_pool_exhausted

logger = logging.getLogger(__name__)

#: Fehlertexte, an denen eine nicht erreichbare Datenbank zu erkennen ist (kein Abfragefehler).
CONNECTION_ERRORS = (
    "connection refused",
    "could not connect",
    "connection failed",
    "server closed the connection",
    "connection timed out",
    "no connection to the server",
    "terminating connection",
    "connection reset",
)

#: Minimale Ersatzseite, falls selbst das Rendern der Vorlage scheitert.
_NOTSEITE = (
    '<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"><title>Vorübergehend nicht erreichbar</title>'
    "</head><body><h1>Vorübergehend nicht erreichbar</h1>"
    "<p>Bitte versuchen Sie es in wenigen Augenblicken erneut.</p></body></html>"
)


def is_database_unavailable(exc: BaseException | None) -> bool:
    """True, wenn *exc* heißt: gerade gibt es keine Datenbankverbindung.

    Dazu gehört ein erschöpfter Verbindungspool (Issue #344) ebenso wie eine nicht
    erreichbare Datenbank. Ein fehlerhaftes SQL gehört nicht dazu.
    """
    if exc is None:
        return False
    if is_pool_exhausted(exc):
        return True
    if isinstance(exc, OperationalError):
        text = str(exc).lower()
        return any(fehler in text for fehler in CONNECTION_ERRORS)
    return False


def database_unavailable_response(request: HttpRequest, *, pool: bool = False) -> HttpResponse:
    """503 ohne einen einzigen Datenbankzugriff.

    Die Wartungsseite wird bewusst *ohne* Request-Kontext gerendert: Die
    Kontextprozessoren (Anmeldestatus, Navigation) bräuchten selbst eine Verbindung — und
    genau die gibt es gerade nicht. Mit Kontext wartete jede Fehlerseite ein zweites Mal
    auf den leeren Pool und verlängerte die Warteschlange.
    """
    # Ein voller Pool ist meist nach Sekunden wieder frei, eine ausgefallene Datenbank nicht.
    headers = {"Retry-After": "30" if pool else "300"}
    if request.path.startswith("/api/"):
        return JsonResponse(
            {"error": "service_unavailable", "detail": "Vorübergehend überlastet, bitte später erneut versuchen."},
            status=503,
            headers=headers,
        )
    inhalt: str
    try:
        inhalt = render_to_string("errors/maintenance.html")
    except Exception:  # noqa: BLE001 - die Notseite darf nie selbst zum Fehler werden
        logger.exception("Wartungsseite ließ sich nicht rendern, liefere Notseite")
        inhalt = _NOTSEITE
    return HttpResponse(inhalt, status=503, headers=headers)


class DatabaseErrorMiddleware:
    """Liefert bei fehlender Datenbankverbindung eine Wartungsseite mit 503 statt 500.

    Nützlich bei Wartung, Deployments, Failover — und bei einem erschöpften
    Verbindungspool (Issue #344).

    Django wandelt Ausnahmen der View schon *innerhalb* des Middleware-Stapels in eine
    Antwort um; ein ``try/except`` um ``get_response`` sähe sie nie. Deshalb greift diese
    Middleware über ``process_exception``. Ausnahmen aus anderen Middlewares fängt der
    500-Handler (``mandari.urls.handler_500``) mit derselben Erkennung ab.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_exception(self, request: HttpRequest, exception: Exception) -> HttpResponse | None:
        if not is_database_unavailable(exception):
            return None
        pool = is_pool_exhausted(exception)
        logger.error(
            "%s: %s %s — Antwort 503",
            "Datenbank-Pool erschöpft" if pool else f"Datenbank nicht erreichbar ({exception})",
            request.method,
            request.path,
        )
        return database_unavailable_response(request, pool=pool)
