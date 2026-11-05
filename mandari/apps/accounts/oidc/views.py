# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Endpunkte des OpenID-Connect-Anbieters (Issue #1206).

Die Ansichten von django-oauth-toolkit, ergänzt um:

- Schalter ``OIDC_PROVIDER_ENABLED``: aus → jeder Endpunkt antwortet mit 404, auch die Discovery
- ``authorize``: läuft über die Anmeldung von mandari. Wer eine offene Pflicht zum zweiten Faktor oder zum
  Sicherheitsschlüssel hat, gilt nicht als angemeldet und muss sie erst erfüllen. ``prompt=login`` und ein
  überschrittenes ``max_age`` führen zur erneuten Anmeldung; bis dahin gibt es für die Anfrage keinen Code
  (:mod:`.reauth`). ``prompt=none`` meldet nur Anwendungen ohne Zustimmungsseite still an. ``approval_prompt``
  der Anfrage gilt nicht: Fremde Anwendungen fragen jedes Mal (``REQUEST_APPROVAL_PROMPT``).
- ``token``: Ratenbegrenzung je Adresse; Gerätefreigabe (RFC 8628) gibt es nicht
- ``revoke`` (RFC 7009): Ratenbegrenzung je Adresse wie am Token-Endpunkt (#1215)
- ``introspect`` (RFC 7662): nur für die Anwendung selbst, mit Client-Authentifizierung und nur über eigene Tokens,
  Ratenbegrenzung je Adresse
- Discovery: nennt nur, was mandari tatsächlich anbietet
- „Verbundene Anwendungen“: Kontoseite mit Widerruf
"""

from __future__ import annotations

import calendar
import json
from collections.abc import Callable
from functools import wraps
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from oauth2_provider import views as dot_views
from oauth2_provider.exceptions import OAuthToolkitError
from oauth2_provider.settings import oauth2_settings
from oauth2_provider.views.mixins import OAuthLibMixin
from oauthlib.common import Request as OAuthlibRequest

from apps.common.next_url import safe_next_url

from .. import security_audit
from ..middleware import REQUIREMENT_TARGETS, open_requirement
from ..views import REAUTH_PARAM
from . import provider_enabled, reauth, selectors, services, throttle
from .validator import signed_in_as

ViewFunction = Callable[..., HttpResponseBase]

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def only_when_enabled(view: ViewFunction) -> ViewFunction:
    """Endpunkt nur bei eingeschaltetem Anbieter, sonst 404 (wie Provisioning v2: keine Auskunft, ob es ihn gibt).

    ``wraps`` übernimmt die Kennzeichen der Ansicht, etwa ``csrf_exempt`` der Token-Endpunkte.
    """

    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not provider_enabled():
            raise Http404
        return view(request, *args, **kwargs)

    return wrapper


def _prompt(request: HttpRequest) -> set[str]:
    return set(request.GET.get("prompt", "").split())


def _login_too_old(request: HttpRequest) -> bool:
    """Verlangt die Anwendung mit ``max_age`` eine jüngere Anmeldung, als die Person hat?"""
    raw = request.GET.get("max_age", "").strip()
    if not raw.isdigit():
        return False
    last_login = getattr(request.user, "last_login", None)
    if last_login is None:
        return True
    return bool((timezone.now() - last_login).total_seconds() > int(raw))


def _request_ids(request: HttpRequest) -> tuple[str, str]:
    """Client-ID und ``state`` der Anfrage; beim Formular der Zustimmungsseite die Werte, aus denen der Code entsteht."""
    data = request.POST if request.method == "POST" else request.GET
    return str(data.get("client_id", "")), str(data.get("state", ""))


def _reauth_pending(request: HttpRequest) -> bool:
    return reauth.pending(request, *_request_ids(request))


def _ignore_approval_prompt(request: HttpRequest) -> None:
    """``approval_prompt`` der Anfrage nicht beachten, sonst wählte die Anwendung selbst ``auto`` statt Zustimmung."""
    if "approval_prompt" in request.GET:
        query = request.GET.copy()
        del query["approval_prompt"]
        request.GET = QueryDict(query.urlencode())


class AuthorizeView(dot_views.AuthorizationView):  # type: ignore[misc]  # Bibliothek ohne Typen
    """Autorisierungs-Endpunkt mit der Anmeldung von mandari und einer schlichten Zustimmungsseite."""

    template_name = "accounts/oidc/zustimmung.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not provider_enabled():
            raise Http404
        user = request.user
        signed_in = None
        if user.is_authenticated:
            requirement = open_requirement(user)
            if requirement:
                # Offene Pflicht zum zweiten Faktor bzw. Sicherheitsschlüssel: gilt nicht als angemeldet
                if "none" in _prompt(request):
                    return cast(HttpResponseBase, self.handle_no_permission())
                target = reverse(REQUIREMENT_TARGETS[requirement])
                return redirect(f"{target}?{urlencode({'next': request.get_full_path()})}")
            if not _login_too_old(request) and not _reauth_pending(request):
                signed_in = user
        with signed_in_as(signed_in):
            return cast(HttpResponseBase, super().dispatch(request, *args, **kwargs))

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        _ignore_approval_prompt(request)
        prompt = _prompt(request)
        if "none" not in prompt and "login" not in prompt and (_login_too_old(request) or _reauth_pending(request)):
            # Erst prüfen, ob die Anfrage zu einer bekannten Anwendung und Redirect-URI passt
            try:
                self.validate_authorization_request(request)
            except OAuthToolkitError as error:
                return cast(HttpResponseBase, self.error_response(error, application=None))
            return self.handle_prompt_login()
        return cast(HttpResponseBase, super().get(request, *args, **kwargs))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        # Formular der Zustimmungsseite: Steht für die Anfrage noch eine erneute Anmeldung aus, erst diese
        if _reauth_pending(request):
            return self.handle_prompt_login()
        return cast(HttpResponseBase, super().post(request, *args, **kwargs))

    def handle_prompt_login(self) -> HttpResponseBase:
        """
        Erneut anmelden (auch mit zweitem Faktor), danach weiter ohne ``prompt`` und ``max_age``.

        Bis zur nächsten Anmeldung in dieser Sitzung gibt es für die Anfrage keinen Code (:mod:`.reauth`): Wer das
        Ziel der Weiterleitung ohne Anmeldung aufruft, landet wieder hier.
        """
        if self.request.user.is_authenticated:
            reauth.require(self.request, *_request_ids(self.request))
        query = self.request.GET.copy()
        query.pop("prompt", None)
        query.pop("max_age", None)
        next_url = f"{self.request.path}?{query.urlencode()}"
        return redirect(f"{settings.LOGIN_URL}?{urlencode({REAUTH_PARAM: '1', 'next': next_url})}")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = cast(dict[str, Any], super().get_context_data(**kwargs))
        context["redirect_host"] = urlsplit(str(context.get("redirect_uri") or "")).netloc
        return context


def _rate_limited(request: HttpRequest, endpoint: str) -> HttpResponse | None:
    """429-Antwort, wenn die Adresse ihr Kontingent an ``endpoint`` überschreitet (:mod:`.throttle`), sonst None."""
    if not throttle.exceeded(request, endpoint):
        return None
    security_audit.record_throttled(request, step=endpoint, limit=security_audit.LIMIT_ADDRESS, window_minutes=1)
    response = JsonResponse(
        {"error": "rate_limited", "error_description": "Zu viele Anfragen. Bitte später erneut versuchen."},
        status=429,
    )
    response["Retry-After"] = str(throttle.seconds_until_next_window())
    response["Cache-Control"] = "no-store"
    return response


class TokenView(dot_views.TokenView):  # type: ignore[misc]  # Bibliothek ohne Typen
    """Token-Endpunkt mit Ratenbegrenzung je Adresse."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        limited = _rate_limited(request, throttle.STEP_TOKEN_ENDPOINT)
        if limited is not None:
            return limited
        if request.POST.get("grant_type") == DEVICE_CODE_GRANT:
            return JsonResponse({"error": "unsupported_grant_type"}, status=400)
        return cast(HttpResponseBase, super().post(request, *args, **kwargs))


class RevokeView(dot_views.RevokeTokenView):  # type: ignore[misc]  # Bibliothek ohne Typen
    """Widerruf (RFC 7009) mit Ratenbegrenzung je Adresse wie am Token-Endpunkt."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        limited = _rate_limited(request, throttle.STEP_REVOKE_ENDPOINT)
        if limited is not None:
            return limited
        return cast(HttpResponseBase, super().post(request, *args, **kwargs))


@method_decorator(csrf_exempt, name="dispatch")
class IntrospectView(OAuthLibMixin, View):  # type: ignore[misc]  # Bibliothek ohne Typen
    """
    Introspektion (RFC 7662) für die Anwendung selbst.

    Die Anwendung weist sich mit Client-ID und Secret aus und erfährt nur etwas über ihre eigenen Access-Tokens;
    alles andere ist ``{"active": false}``. Die Person erscheint nur als ``sub`` (Kennung wie im ID-Token).
    """

    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        limited = _rate_limited(request, throttle.STEP_INTROSPECT_ENDPOINT)
        if limited is not None:
            return limited
        core = self.get_oauthlib_core()
        uri, http_method, body, headers = core._extract_params(request)
        oauth_request = OAuthlibRequest(uri, http_method, body, headers)
        validator = core.server.request_validator
        if not validator.authenticate_client(oauth_request) or oauth_request.client is None:
            response = JsonResponse({"error": "invalid_client"}, status=401)
            response["WWW-Authenticate"] = 'Basic realm="mandari"'
            return response
        client = oauth_request.client
        raw = request.POST.get("token", "")
        if not raw:
            return JsonResponse({"error": "invalid_request", "error_description": "token fehlt."}, status=400)
        token = selectors.own_access_token(client, raw)
        inactive = JsonResponse({"active": False})
        if token is None or not token.is_valid() or not client.is_usable(None):
            return inactive
        if token.user is not None and not token.user.is_active:
            return inactive
        data: dict[str, Any] = {
            "active": True,
            "token_type": "Bearer",
            "scope": " ".join(sorted(set(str(token.scope).split()) & set(client.scope_list))),
            "client_id": client.client_id,
            "exp": int(calendar.timegm(token.expires.utctimetuple())),
            "iat": int(calendar.timegm(token.created.utctimetuple())),
            "iss": str(oauth2_settings.OIDC_ISS_ENDPOINT),
        }
        if token.user is not None:
            data["sub"] = str(token.user.pk)
        response = JsonResponse(data)
        response["Cache-Control"] = "no-store"
        return response


class DiscoveryView(dot_views.ConnectDiscoveryInfoView):  # type: ignore[misc]  # Bibliothek ohne Typen
    """OpenID Provider Metadata: nur Abläufe, Verfahren und Endpunkte, die mandari anbietet."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        response = cast(HttpResponse, super().get(request, *args, **kwargs))
        data = json.loads(response.content)
        data.update(
            {
                "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "code_challenge_methods_supported": ["S256"],
                "revocation_endpoint": self._get_endpoint_url(request, "revoke-token", required=True),
                "revocation_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
                "introspection_endpoint": self._get_endpoint_url(request, "introspect", required=True),
                "introspection_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "request_parameter_supported": False,
                "request_uri_parameter_supported": False,
            }
        )
        data.pop("client_id_metadata_document_supported", None)
        result = JsonResponse(data)
        result["Access-Control-Allow-Origin"] = "*"
        result["Cache-Control"] = "public, max-age=3600"
        return result


# ---------------------------------------------------------------------------
# Kontoseite „Verbundene Anwendungen“
# ---------------------------------------------------------------------------


class ConnectedApplicationsView(LoginRequiredMixin, View):
    """Anwendungen, die im Namen der Person handeln dürfen, mit Widerruf je Anwendung."""

    template_name = "accounts/oidc/verbundene_anwendungen.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not provider_enabled():
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get(self, request: HttpRequest) -> HttpResponse:
        context = {
            "applications": selectors.connected_applications(request.user),
            # Rückweg zur Konto- oder Profilseite, von der die Person kam (wie bei den Sicherheitsschlüsseln)
            "next": safe_next_url(request, request.GET.get("next")),
        }
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest) -> HttpResponse:
        application_id = request.POST.get("application_id", "")
        if application_id.isdigit() and services.revoke_for_user(request, request.user, int(application_id)):
            messages.success(request, "Der Zugang wurde widerrufen. Die Anwendung muss dich erneut fragen.")
        else:
            messages.error(request, "Für diese Anwendung gab es keinen Zugang zu widerrufen.")
        target = reverse("accounts:connected_applications")
        next_url = safe_next_url(request, request.POST.get("next"))
        return redirect(f"{target}?{urlencode({'next': next_url})}" if next_url else target)


authorize = only_when_enabled(AuthorizeView.as_view())
token = only_when_enabled(TokenView.as_view())
revoke = only_when_enabled(RevokeView.as_view())
introspect = only_when_enabled(IntrospectView.as_view())
userinfo = only_when_enabled(dot_views.UserInfoView.as_view())
jwks = only_when_enabled(dot_views.JwksInfoView.as_view())
discovery = only_when_enabled(DiscoveryView.as_view())
