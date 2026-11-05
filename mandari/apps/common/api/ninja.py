# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RFC-9457-Fehler für Schnittstellen unter django-ninja.

``install_problem_handlers(api, realm=…, unauthenticated=…)`` registriert die Handler: ``ProblemError`` wird
zu seinem Problem, jeder andere ``HttpError`` zu einem Problem ohne Art (``about:blank``), eine fehlende
Anmeldung zu 401 mit ``WWW-Authenticate`` und eine ungültige Anfrage zu 422 (``validierung``, die Verstöße
unter ``errors``). ``instance`` ist die Adresse der Anfrage samt Query-String.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError, ValidationError

from .problems import Problem, ProblemError, problem_response

#: Meldung bei ungültigen Feldern einer Anfrage
INVALID_REQUEST = "Die Anfrage enthält ungültige oder fehlende Felder."


def install_problem_handlers(api: NinjaAPI, *, realm: str, unauthenticated: str) -> None:
    """
    Registriert die RFC-9457-Handler für alle Fehlerklassen der API.

    ``realm`` steht im Header ``WWW-Authenticate`` einer 401, ``unauthenticated`` ist deren Meldung.
    """

    @api.exception_handler(ProblemError)
    def handle_problem(request: HttpRequest, exc: ProblemError) -> HttpResponse:
        return problem_response(exc.problem, instance=request.get_full_path(), headers=exc.headers)

    @api.exception_handler(HttpError)
    def handle_http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
        problem = Problem(status=exc.status_code, kind=None, detail=str(exc.message))
        return problem_response(problem, instance=request.get_full_path())

    @api.exception_handler(AuthenticationError)
    def handle_auth_error(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
        problem = Problem(status=401, kind="nicht-authentifiziert", detail=unauthenticated)
        return problem_response(
            problem, instance=request.get_full_path(), headers={"WWW-Authenticate": f'Bearer realm="{realm}"'}
        )

    @api.exception_handler(ValidationError)
    def handle_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
        problem = Problem(status=422, kind="validierung", detail=INVALID_REQUEST, extensions={"errors": exc.errors})
        return problem_response(problem, instance=request.get_full_path())
