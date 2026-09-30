# SPDX-License-Identifier: AGPL-3.0-or-later
"""
HTTP-Weg für Befehle: ``POST …/<befehl>/v<version>`` (``docs/adr/20260929-befehle-synchron.md``).

- Anmeldung: Die Installation übergibt ``authenticate``; es bildet die Anfrage (z. B. ein Token mit
  Berechtigungsumfang) auf einen ``Principal`` ab: Mandant, Auslöser und erlaubte Befehle. Ohne
  gültige Anmeldung 401, ohne Berechtigung 403. Mandant und Auslöser stammen immer aus der Anmeldung;
  ein abweichender Header ``Mandari-Tenant`` ergibt 403.
- Pflicht-Header ``Idempotency-Key`` (IETF-Entwurf, als String mit oder ohne Anführungszeichen),
  sonst 400. Inhalt als JSON-Objekt, höchstens 64 Ebenen tief verschachtelt (sonst 400).
- Erfolg: ``201`` mit der Quittung als JSON, bei Wiederholung dieselbe. Fehler:
  ``application/problem+json`` nach RFC 9457, gleich den Problemen des ``InProcessClient``.

Die Adressen bindet die Installation ein, sobald ein Eigentümer Befehle über HTTP anbietet (etwa das
Profil Einreichung):

    path("api/v1/befehle/", include(command_urlpatterns(authenticate=token_principal)))
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, Final

from django.core.exceptions import RequestDataTooBig
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import URLPattern, path
from django.views.decorators.csrf import csrf_exempt

from apps.common.observability import current_request_id

from .dispatcher import KEY_MISSING, Dispatcher, get_dispatcher
from .problems import CommandError, Problem
from .types import ACTOR_REF_RE, TENANT_REF_RE, TOO_DEEP, Command, exceeds_depth

logger = logging.getLogger("hub.commands")

IDEMPOTENCY_HEADER: Final = "Idempotency-Key"
TENANT_HEADER: Final = "Mandari-Tenant"
CORRELATION_HEADER: Final = "X-Correlation-ID"
ALL_COMMANDS: Final = "*"


@dataclass(frozen=True)
class Principal:
    """Angemeldeter Aufrufer: Mandant, Auslöser und erlaubte Befehle (``*`` für alle)."""

    tenant_ref: str
    actor_ref: str | None
    commands: Collection[str]

    def __post_init__(self) -> None:
        if not TENANT_REF_RE.fullmatch(self.tenant_ref):
            raise ValueError("tenant_ref muss session:<uuid>, org:<uuid> oder source:<uuid> sein")
        if self.actor_ref is not None and not ACTOR_REF_RE.fullmatch(self.actor_ref):
            raise ValueError("actor_ref muss user:<uuid> oder system:<auftrag> sein")

    def may(self, name: str) -> bool:
        return ALL_COMMANDS in self.commands or name in self.commands


Authenticator = Callable[[HttpRequest], Principal | None]


def command_urlpatterns(
    authenticate: Authenticator, dispatcher: Dispatcher | None = None, *, realm: str = "mandari"
) -> list[URLPattern]:
    """Adressen des HTTP-Wegs; ``dispatcher`` Standard: der Dispatcher der Installation."""
    view = csrf_exempt(CommandView(authenticate, dispatcher, realm=realm))
    return [path("<str:name>/v<int:version>", view, name="command")]


class CommandView:
    """Nimmt einen Befehl über HTTP an und gibt ihn an den Dispatcher weiter."""

    def __init__(self, authenticate: Authenticator, dispatcher: Dispatcher | None, *, realm: str) -> None:
        self.authenticate = authenticate
        self.dispatcher = dispatcher
        self.realm = realm

    def __call__(self, request: HttpRequest, name: str, version: int) -> HttpResponse:
        try:
            command = self._command(request, name, version)
            receipt = (self.dispatcher or get_dispatcher()).dispatch(command)
        except CommandError as exc:
            return self._problem(request, exc.problem)
        response = JsonResponse(receipt.to_dict(), status=201, json_dumps_params={"ensure_ascii": False})
        response["Cache-Control"] = "no-store"
        return response

    def _command(self, request: HttpRequest, name: str, version: int) -> Command:
        if request.method != "POST":
            raise CommandError.of(405, "methode-nicht-erlaubt", "Befehle werden per POST gesendet.", allow="POST")
        principal = self.authenticate(request)
        if principal is None:
            raise CommandError.of(
                401, "nicht-authentifiziert", "Gültige Anmeldung erforderlich (Authorization: Bearer)."
            )
        key = _idempotency_key(request.headers.get(IDEMPOTENCY_HEADER))
        if key is None:
            raise CommandError.of(400, "idempotenzschluessel-fehlt", KEY_MISSING)
        tenant = request.headers.get(TENANT_HEADER)
        if (tenant and tenant != principal.tenant_ref) or not principal.may(name):
            raise CommandError.of(403, "keine-berechtigung", "Diese Anmeldung darf den Befehl hier nicht senden.")
        return Command(
            name=name,
            version=version,
            body=_json_object(request),
            idempotency_key=key,
            tenant_ref=principal.tenant_ref,
            actor_ref=principal.actor_ref,
            correlation_id=_uuid_or_new(request.headers.get(CORRELATION_HEADER)),
        )

    def _problem(self, request: HttpRequest, problem: Problem) -> HttpResponse:
        data = problem.to_dict(instance=request.path)
        allow = data.pop("allow", None)
        data["request_id"] = current_request_id() or None
        response = JsonResponse(data, status=problem.status, json_dumps_params={"ensure_ascii": False})
        response["Content-Type"] = "application/problem+json; charset=utf-8"
        response["Cache-Control"] = "no-store"
        if problem.status == 401:
            response["WWW-Authenticate"] = f'Bearer realm="{self.realm}"'
        if allow:
            response["Allow"] = str(allow)
        return response


def _idempotency_key(raw: str | None) -> str | None:
    """Wert des Headers; ein String nach RFC 8941 kommt in Anführungszeichen, ein Token ohne."""
    if raw is None:
        return None
    value = raw.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value or None


def _json_object(request: HttpRequest) -> dict[str, Any]:
    content_type = request.content_type or ""
    if content_type != "application/json":
        raise CommandError.of(415, "format-nicht-unterstuetzt", "Der Inhalt muss application/json sein.")
    try:
        data = json.loads(request.body or b"null")
    except RequestDataTooBig:
        raise CommandError.of(413, "anfrage-zu-gross", "Der Inhalt ist zu groß.") from None
    except RecursionError:
        # Ab welcher Tiefe der JSON-Leser aufgibt, hängt von Plattform und Python-Version ab.
        raise CommandError.of(400, "ungueltiges-json", TOO_DEEP) from None
    except (ValueError, UnicodeDecodeError):
        raise CommandError.of(400, "ungueltiges-json", "Der Inhalt ist kein gültiges JSON.") from None
    if not isinstance(data, dict):
        raise CommandError.of(400, "ungueltiges-json", "Der Inhalt muss ein JSON-Objekt sein.")
    if exceeds_depth(data):
        raise CommandError.of(400, "ungueltiges-json", TOO_DEEP)
    return data


def _uuid_or_new(raw: str | None) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw)) if raw else uuid.uuid4()
    except ValueError:
        return uuid.uuid4()
