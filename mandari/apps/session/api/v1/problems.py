# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fehlerformat nach RFC 9457 („Problem Details for HTTP APIs“).

Jede Fehlerantwort der API v1 ist ``application/problem+json`` mit ``type``, ``title``, ``status``,
``detail``, ``instance`` und der Erweiterung ``request_id`` (Korrelation mit den Logs, siehe
``apps.common.observability``). Validierungsfehler tragen zusätzlich ``errors`` (Pydantic-Details).
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError, ValidationError

from apps.common.observability import current_request_id

PROBLEM_TYPE_BASE = "https://docs.mandari.de/api/probleme/"

TITLES: dict[int, str] = {
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


class Problem(HttpError):  # noqa: N818 – RFC-9457-Begriff „Problem“, kein Error-Suffix
    """Fachlicher Fehler mit optionalem ``type``-Slug und Zusatzfeldern."""

    def __init__(self, status: int, detail: str, *, kind: str | None = None, **extensions: Any) -> None:
        super().__init__(status, detail)
        self.kind = kind
        self.extensions = extensions


def problem_response(
    request: HttpRequest,
    status: int,
    detail: str,
    *,
    kind: str | None = None,
    headers: dict[str, str] | None = None,
    **extensions: Any,
) -> HttpResponse:
    payload: dict[str, Any] = {
        "type": f"{PROBLEM_TYPE_BASE}{kind}" if kind else "about:blank",
        "title": TITLES.get(status, "Fehler"),
        "status": status,
        "detail": detail,
        "instance": request.get_full_path(),
        "request_id": current_request_id() or None,
    }
    payload.update(extensions)
    response = JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})
    response["Content-Type"] = "application/problem+json; charset=utf-8"
    for name, value in (headers or {}).items():
        response[name] = value
    return response


def install_problem_handlers(api: NinjaAPI) -> None:
    """Registriert die RFC-9457-Handler für alle Fehlerklassen der API."""

    @api.exception_handler(Problem)
    def handle_problem(request: HttpRequest, exc: Problem) -> HttpResponse:
        headers = exc.extensions.pop("headers", None)
        return problem_response(
            request, exc.status_code, str(exc.message), kind=exc.kind, headers=headers, **exc.extensions
        )

    @api.exception_handler(HttpError)
    def handle_http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
        return problem_response(request, exc.status_code, str(exc.message))

    @api.exception_handler(AuthenticationError)
    def handle_auth_error(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
        return problem_response(
            request,
            401,
            "Gültiges API-Token erforderlich (Authorization: Bearer <token>) oder angemeldete Sitzung.",
            kind="nicht-authentifiziert",
            headers={"WWW-Authenticate": 'Bearer realm="mandari Session-API"'},
        )

    @api.exception_handler(ValidationError)
    def handle_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
        return problem_response(
            request,
            422,
            "Die Anfrage enthält ungültige oder fehlende Felder.",
            kind="validierung",
            errors=exc.errors,
        )
