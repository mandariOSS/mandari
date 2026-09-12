# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sicherheitsschlüssel und Passkeys: Verwaltung im eigenen Konto und Anmeldung
im zweiten Schritt (siehe webauthn_service.py).
"""

from __future__ import annotations

import json
from typing import Any, cast

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from . import webauthn_service
from .models import User, WebAuthnCredential
from .services import TwoFactorService
from .two_factor_policy import POLICY_CACHE_SESSION_KEY
from .views import MAX_2FA_FAILURES, PENDING_2FA_SESSION_KEY, LoginTwoFactorView, LoginView, complete_login

EXPIRED_MESSAGE = "Die Anmeldung ist abgelaufen. Bitte melde dich erneut an."


def _json_body(request: HttpRequest) -> dict[str, Any]:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _credential(body: dict[str, Any]) -> dict[str, Any]:
    credential = body.get("credential")
    return credential if isinstance(credential, dict) else {}


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _options_response(options_json: str) -> HttpResponse:
    return HttpResponse(options_json, content_type="application/json")


class SecurityKeysView(LoginRequiredMixin, View):
    """Sicherheitsschlüssel und Passkeys des eigenen Kontos verwalten."""

    template_name = "accounts/security_keys.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        user = cast(User, request.user)
        next_url = request.GET.get("next", "")
        if not url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            next_url = ""
        context = {
            "credentials": WebAuthnCredential.objects.filter(user=user),
            "totp_enabled": TwoFactorService().is_2fa_enabled(user),
            "next": next_url,
        }
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest) -> HttpResponse:
        user = cast(User, request.user)
        if request.POST.get("action") == "remove":
            credential: WebAuthnCredential | None
            try:
                credential = WebAuthnCredential.objects.filter(
                    user=user, pk=request.POST.get("credential_id", "")
                ).first()
            except (ValueError, ValidationError):
                credential = None
            if credential is None:
                messages.error(request, "Sicherheitsschlüssel nicht gefunden.")
            elif not user.check_password(request.POST.get("password", "")):
                messages.error(request, "Das Passwort ist nicht korrekt.")
            else:
                credential.delete()
                request.session.pop(POLICY_CACHE_SESSION_KEY, None)
                messages.success(request, f"„{credential.name}“ wurde entfernt.")
        return redirect("accounts:security_keys")


class RegistrationOptionsView(LoginRequiredMixin, View):
    """Optionen zum Registrieren eines Schlüssels (nur mit eingerichteter Authenticator-App)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        user = cast(User, request.user)
        if not TwoFactorService().is_2fa_enabled(user):
            return _error("Richte zuerst die Authenticator-App ein – sie dient als Rückfall.", status=409)
        try:
            return _options_response(webauthn_service.registration_options(request, user))
        except webauthn_service.WebAuthnError as exc:
            return _error(str(exc))


class RegistrationVerifyView(LoginRequiredMixin, View):
    """Signierte Registrierung prüfen und speichern."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        user = cast(User, request.user)
        body = _json_body(request)
        try:
            credential = webauthn_service.register(request, user, _credential(body), str(body.get("name", "")))
        except webauthn_service.WebAuthnError as exc:
            return _error(str(exc))
        request.session.pop(POLICY_CACHE_SESSION_KEY, None)
        messages.success(request, f"„{credential.name}“ wurde hinzugefügt.")
        return JsonResponse({"ok": True})


class LoginOptionsView(View):
    """Optionen für die Anmeldung mit Schlüssel – nur im zweiten Anmeldeschritt."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        _data, user = LoginTwoFactorView()._pending(request)  # type: ignore[no-untyped-call]
        if user is None:
            return _error(EXPIRED_MESSAGE, status=401)
        try:
            return _options_response(webauthn_service.authentication_options(request, user))
        except webauthn_service.WebAuthnError as exc:
            return _error(str(exc))


class LoginVerifyView(View):
    """Anmeldung mit Schlüssel abschließen (gleiche Sperren und Protokollierung wie der Code-Schritt)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        step = LoginTwoFactorView()
        data, user = step._pending(request)  # type: ignore[no-untyped-call]
        if user is None:
            return _error(EXPIRED_MESSAGE, status=401)
        if step._recent_failures(user) >= MAX_2FA_FAILURES:
            request.session.pop(PENDING_2FA_SESSION_KEY, None)
            return _error("Zu viele fehlgeschlagene Versuche. Bitte warte 15 Minuten.", status=429)
        try:
            webauthn_service.authenticate(request, user, _credential(_json_body(request)))
        except webauthn_service.WebAuthnError as exc:
            step._log(request, user, success=False)
            return _error(str(exc))
        request.session.pop(PENDING_2FA_SESSION_KEY, None)
        step._log(request, user, success=True)
        complete_login(request, user, bool(data.get("remember")))
        success_url = LoginView().get_success_url(request, data.get("next") or None)  # type: ignore[no-untyped-call]
        return JsonResponse({"redirect": success_url})
