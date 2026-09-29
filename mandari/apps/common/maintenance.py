# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wartungsmodus (Issue #588).

Der Schalter „Wartungsmodus“ in den Systemeinstellungen sperrt Bürgerportal, Work, Session und
die APIs: Jede Anfrage erhält 503 mit der Wartungsnachricht und ``Retry-After``. Erreichbar
bleiben

- die Administration (``/admin/``) und die Anmeldung dorthin (``/accounts/login/…``,
  Abmelden), damit sich der Modus wieder abschalten lässt,
- Gesundheitsprüfungen und Metriken, damit Orchestrierung und Überwachung die Instanz nicht
  für kaputt halten und neu starten,
- statische Dateien für die Wartungsseite selbst.

Angemeldete Konten mit Mitarbeiterstatus (``is_staff``) sehen die Anwendung weiter, um den
Stand vor dem Freischalten zu prüfen.

Die Einstellung ``MAINTENANCE_MODE_ENFORCEMENT`` schaltet die Prüfung ab (nur in den Tests).

Der Schalter wird über ``SiteSettings.get_settings()`` gelesen (Cache, 5 Minuten; das Speichern
im Admin leert ihn). Ist er nicht lesbar, bleibt die Anwendung offen – ein Fehler beim Lesen
einer Einstellung darf die Instanz nicht sperren. Fehlt die Datenbank ganz, antwortet die
Middleware wie ``DatabaseErrorMiddleware`` sofort mit 503, statt die View ein zweites Mal auf
die Verbindung warten zu lassen.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseBase, JsonResponse

from apps.common.db_connections import is_pool_exhausted
from apps.common.middleware import database_unavailable_response, is_database_unavailable, render_maintenance_page

logger = logging.getLogger(__name__)

#: Pfade, die auch im Wartungsmodus für alle erreichbar bleiben.
EXEMPT_PREFIXES = (
    "/admin/",
    "/accounts/login/",
    "/accounts/logout/",
    "/accounts/logged-out/",
    "/health/",
    "/metrics/",
    "/static/",
    "/favicon.ico",
)

#: Pfade mit maschinenlesbarer Antwort (JSON statt Wartungsseite).
API_PREFIXES = ("/api/", "/oparl/")

#: Empfohlene Wartezeit bis zum nächsten Versuch (Sekunden).
RETRY_AFTER_SECONDS = 300

#: Ersatztext, falls in den Einstellungen keine Wartungsnachricht steht.
STANDARD_NACHRICHT = "Die Website wird gerade gewartet. Bitte versuchen Sie es später erneut."

GetResponse = Callable[[HttpRequest], HttpResponseBase]


class _DatenbankFehltError(Exception):
    """Der Schalter ist nicht lesbar, weil die Datenbank fehlt (``pool`` = erschöpfter Pool)."""

    def __init__(self, pool: bool) -> None:
        super().__init__()
        self.pool = pool


def wartungsnachricht() -> str | None:
    """Die Wartungsnachricht bei aktivem Wartungsmodus, sonst ``None``.

    Löst ``_DatenbankFehltError`` aus, wenn die Datenbank nicht erreichbar ist. Jeder andere Fehler
    beim Lesen gilt als „kein Wartungsmodus“.
    """
    from apps.common.models import SiteSettings

    try:
        einstellungen = SiteSettings.get_settings()
    except Exception as exc:  # noqa: BLE001 - der Schalter darf die Instanz nie selbst sperren
        if is_database_unavailable(exc):
            raise _DatenbankFehltError(pool=is_pool_exhausted(exc)) from exc
        logger.warning("Wartungsmodus nicht lesbar (%s), Anwendung bleibt erreichbar", type(exc).__name__)
        return None
    if not einstellungen.maintenance_mode:
        return None
    return einstellungen.maintenance_message.strip() or STANDARD_NACHRICHT


def wartungsantwort(request: HttpRequest, nachricht: str) -> HttpResponse:
    """503 mit Wartungsnachricht: JSON für die APIs, sonst die Wartungsseite."""
    headers = {"Retry-After": str(RETRY_AFTER_SECONDS), "Cache-Control": "no-store"}
    if request.path_info.startswith(API_PREFIXES):
        return JsonResponse({"error": "maintenance", "detail": nachricht}, status=503, headers=headers)
    if request.headers.get("HX-Request") == "true":
        # HTMX tauscht Fehlerantworten nicht ein; neu laden zeigt die Wartungsseite.
        headers["HX-Refresh"] = "true"
    # Ohne Request-Kontext: Die Kontextprozessoren bräuchten Datenbank und Navigation.
    inhalt = render_maintenance_page({"maintenance_message": nachricht})
    return HttpResponse(inhalt, status=503, headers=headers)


def _ist_mitarbeiter(request: HttpRequest) -> bool:
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and getattr(user, "is_staff", False))


class MaintenanceModeMiddleware:
    """Sperrt die Anwendung bei aktivem Wartungsmodus mit 503 (Ausnahmen siehe Modul)."""

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if not settings.MAINTENANCE_MODE_ENFORCEMENT or request.path_info.startswith(EXEMPT_PREFIXES):
            return self.get_response(request)
        try:
            nachricht = wartungsnachricht()
        except _DatenbankFehltError as fehlt:
            return database_unavailable_response(request, pool=fehlt.pool)
        if nachricht is None or _ist_mitarbeiter(request):
            return self.get_response(request)
        return wartungsantwort(request, nachricht)
