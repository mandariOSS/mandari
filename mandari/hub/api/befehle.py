# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehle als Operationen der gemeinsamen Schnittstelle (Issue #1588, ``docs/adr/20261128-api-first-ohne-ausnahme.md``).

    POST /api/mandari/v1/<bereich>/<mandant>/befehle/<befehl>/v<version>

Jeder Befehl, den sein Eigentümer mit ``Exposure`` registriert (``hub.commands.exposure``), ist hier ohne weiteren
Code erreichbar und steht in der Beschreibung (``hub.api.openapi.befehle``). Die View liest nur die Anfrage; prüfen
und schreiben tut der Handler des Eigentümers über denselben Dispatcher wie bei einem Aufruf aus der eigenen
Oberfläche (``hub.commands.oberflaeche.ausfuehren``).

Reihenfolge der Prüfungen und Probleme (RFC 9457):

1. Befehl in dieser Version für diesen Mandantenbereich freigegeben, sonst 404 (``befehl-unbekannt``); nur POST,
   sonst 405.
2. **Anmeldung**, sonst 401 mit ``WWW-Authenticate``:

   - Access-Token des OpenID-Connect-Anbieters im Namen einer Person mit einem der Scopes der Freigabe. Tokens von
     Diensten 403, fehlender Scope 403 (``insufficient_scope``), andere Tokens 401. Ein ungültiges Token fällt nie auf
     die Browser-Sitzung zurück.
   - ohne ``Authorization``: die Browser-Sitzung der eigenen Oberfläche mit CSRF-Token (``X-CSRFToken``), sonst 403.

   Auslöser ist immer die Person (``user:<id>``); was sie im Mandanten darf, prüft der Handler.
3. Höchstens ``REQUESTS_PER_MINUTE`` Befehle je Person und Minute, sonst 429 mit ``Retry-After``; ebenso, wenn die
   Anwendung ihre Anfragen je Minute verbraucht hat (``rate_limit_per_minute``, Issue #1591).
4. Mandant aus dem Kurznamen der Adresse (``TenantArea``), aktiv, sonst 404 (``mandant-unbekannt``). Eine Anwendung,
   die eine Kommune oder Fraktion selbst registriert hat, gilt nur für diesen Mandanten, sonst 403 (Issue #1591).
5. ``Idempotency-Key`` und Inhalt (``hub.commands.http.command_from_request``), dann der Dispatcher mit seinen
   Prüfungen (Vertrag 422, Rechte 403, Objekt 404, fachliche Konflikte 409).

Erfolg: ``201`` mit der Quittung, eine Wiederholung mit demselben Schlüssel erhält dieselbe.
"""

from __future__ import annotations

from typing import Final

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.middleware.csrf import CsrfViewMiddleware
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from apps.accounts.oidc import access as oidc_access
from apps.accounts.oidc import bindings
from hub.commands import CommandError, Exposure, Problem, get_dispatcher, tenant_area
from hub.commands.channel import OBERFLAECHE, SCHNITTSTELLE, Channel, channel
from hub.commands.dispatcher import UNKNOWN_COMMAND
from hub.commands.http import POST_ONLY, command_from_request, problem_for, receipt_response

app_name = "befehle"

REALM: Final = "mandari"
#: Höchstzahl von Befehlen je Person und Minute über diesen Weg
REQUESTS_PER_MINUTE: Final = 120

#: Feste Meldungen (nie mit Werten aus der Anfrage)
LOGIN_REQUIRED: Final = (
    "Anmeldung erforderlich: Access-Token im Namen einer Person (Authorization: Bearer) oder Sitzung der Oberfläche."
)
TOKEN_FOREIGN: Final = "Die Schnittstelle nimmt nur Access-Tokens des OpenID-Connect-Anbieters an."
TOKEN_INVALID: Final = "Das Zugriffstoken ist ungültig, abgelaufen oder widerrufen."
PERSON_REQUIRED: Final = "Befehle nehmen nur Tokens im Namen einer Person an, keine Dienst-Tokens."
SCOPE_MISSING: Final = "Dem Zugriffstoken fehlt ein Scope, der diesen Befehl erlaubt."
CSRF_FAILED: Final = "CSRF-Prüfung fehlgeschlagen: Anfragen mit der Sitzung der Oberfläche brauchen das CSRF-Token."
RATE_LIMITED: Final = "Zu viele Anfragen. Bitte in einer Minute erneut versuchen."
TENANT_UNKNOWN: Final = "Der Mandant ist nicht bekannt oder deaktiviert."
APPLICATION_RATE_LIMITED: Final = "Die Anwendung hat ihre Anfragen dieser Minute verbraucht."
FOREIGN_AREA: Final = "Die Anwendung ist für einen anderen Mandanten registriert."

#: Art in ``tenant_ref`` -> Art des Bereichs, der eine Anwendung registriert hat (``apps.accounts.oidc.bindings``)
_OWNER_KINDS: Final[dict[str, str]] = {"session": bindings.TENANT, "org": bindings.ORGANIZATION}


class _RefusedError(CommandError):
    """Absage mit eigenen Headern (``WWW-Authenticate``, ``Retry-After``)."""

    def __init__(self, status: int, kind: str, detail: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(Problem(status=status, kind=kind, detail=detail))
        self.headers = headers or {}


def _challenge(exposure: Exposure, error: str | None = None) -> dict[str, str]:
    value = f'Bearer realm="{REALM}", scope="{" ".join(exposure.scopes)}"'
    if error:
        value += f', error="{error}"'
    return {"WWW-Authenticate": value}


def command_view(request: HttpRequest, area: str, tenant: str, name: str, version: int) -> HttpResponse:
    """Nimmt einen freigegebenen Befehl an (siehe Moduldokumentation)."""
    try:
        exposure = get_dispatcher().exposure(name, version)
        if exposure is None or exposure.area != area or tenant_area(area) is None:
            raise CommandError.of(404, "befehl-unbekannt", UNKNOWN_COMMAND)
        if request.method != "POST":
            raise CommandError.of(405, "methode-nicht-erlaubt", POST_ONLY, allow="POST")
        actor, way, owner = _authenticate(request, exposure)
        _throttle(actor)
        tenant_ref = _tenant_ref(area, tenant)
        _check_owner(owner, tenant_ref)
        command = command_from_request(request, name, version, tenant_ref=tenant_ref, actor_ref=actor)
        with channel(way):
            receipt = get_dispatcher().dispatch(command)
    except _RefusedError as exc:
        return problem_for(request, exc.problem, realm=REALM, headers=exc.headers)
    except CommandError as exc:
        return problem_for(request, exc.problem, realm=REALM)
    return receipt_response(receipt)


def _authenticate(request: HttpRequest, exposure: Exposure) -> tuple[str, Channel, str]:
    """
    Auslöser ``user:<id>``, Weg und Bereich der Anwendung (``OAuthAccess.owner``, leer bei der Oberfläche); Access-Token
    vor Browser-Sitzung (siehe Moduldokumentation).
    """
    header = str(request.META.get("HTTP_AUTHORIZATION", ""))
    if header:
        if not header.startswith("Bearer "):
            raise _RefusedError(401, "nicht-authentifiziert", LOGIN_REQUIRED, _challenge(exposure))
        raw = header[7:].strip()
        if not oidc_access.looks_like_access_token(raw):
            raise _RefusedError(401, "nicht-authentifiziert", TOKEN_FOREIGN, _challenge(exposure, "invalid_token"))
        try:
            access = oidc_access.authenticate(raw, resource_uri=request.build_absolute_uri(request.path))
        except oidc_access.RateLimited as limited:
            raise _RefusedError(
                429, "ratenlimit", APPLICATION_RATE_LIMITED, {"Retry-After": str(limited.retry_after)}
            ) from None
        except oidc_access.InvalidAccessToken:
            raise _RefusedError(
                401, "nicht-authentifiziert", TOKEN_INVALID, _challenge(exposure, "invalid_token")
            ) from None
        if access.user is None:
            raise _RefusedError(403, "keine-berechtigung", PERSON_REQUIRED)
        if not access.scopes & set(exposure.scopes):
            raise _RefusedError(403, "keine-berechtigung", SCOPE_MISSING, _challenge(exposure, "insufficient_scope"))
        return f"user:{access.user.pk}", Channel(SCHNITTSTELLE, client_id=access.client_id), access.owner
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise _RefusedError(401, "nicht-authentifiziert", LOGIN_REQUIRED, _challenge(exposure))
    if not _csrf_ok(request):
        raise _RefusedError(403, "csrf", CSRF_FAILED)
    return f"user:{user.pk}", Channel(OBERFLAECHE), ""


def _check_owner(owner: str, tenant_ref: str) -> None:
    """Anwendung einer Kommune oder Fraktion nur für ihren eigenen Mandanten (``bindings.allows``, Issue #1591)."""
    kind, _separator, key = tenant_ref.partition(":")
    owner_kind = _OWNER_KINDS.get(kind)
    if owner_kind is not None and not bindings.allows(owner, owner_kind, key):
        raise _RefusedError(403, "keine-berechtigung", FOREIGN_AREA)


def _csrf_ok(request: HttpRequest) -> bool:
    """CSRF-Prüfung wie bei jeder Ansicht der Oberfläche (die Adresse selbst ist ``csrf_exempt`` für Tokens)."""
    middleware = CsrfViewMiddleware(lambda _request: HttpResponseForbidden())
    request.csrf_processing_done = False  # type: ignore[attr-defined]
    middleware.process_request(request)
    return middleware.process_view(request, None, (), {}) is None  # type: ignore[arg-type]


def _throttle(actor: str) -> None:
    """Höchstens ``REQUESTS_PER_MINUTE`` Befehle je Person und Minute; sonst 429 mit ``Retry-After``."""
    key = f"befehle-api:{actor}:rate"
    if cache.add(key, 1, timeout=60):
        return
    try:
        count = int(cache.incr(key))
    except ValueError:  # Schlüssel zwischenzeitlich abgelaufen
        cache.add(key, 1, timeout=60)
        return
    if count > REQUESTS_PER_MINUTE:
        raise _RefusedError(429, "ratenlimit", RATE_LIMITED, {"Retry-After": "60"})


def _tenant_ref(area: str, slug: str) -> str:
    tenants = tenant_area(area)
    tenant_ref = tenants.tenant_ref(slug) if tenants is not None else None
    if tenant_ref is None:
        raise CommandError.of(404, "mandant-unbekannt", TENANT_UNKNOWN)
    return tenant_ref


urlpatterns = [
    path(
        "<slug:area>/<slug:tenant>/befehle/<str:name>/v<int:version>",
        csrf_exempt(command_view),
        name="befehl",
    ),
]
