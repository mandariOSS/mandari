# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugangsschutz: Netzbeschränkung für den Django-Admin und Durchsetzung des
zweiten Faktors für bereits angemeldete Sitzungen.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, HttpResponseBase, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from .services import TwoFactorService
from .two_factor_policy import (
    POLICY_CACHE_SESSION_KEY,
    admin_networks,
    client_ip,
    ip_in_networks,
    security_key_required,
    two_factor_required,
)
from .webauthn_service import has_credentials

logger = logging.getLogger(__name__)

ADMIN_PREFIXES = ("/admin/",)

# Pfade, die ohne erfüllte Pflicht erreichbar bleiben (Einrichtung, Abmelden, Assets)
TWO_FACTOR_EXEMPT_PREFIXES = (
    "/accounts/",
    "/static/",
    "/media/",
    "/health",
    "/favicon.ico",
    "/manifest",
    "/sw.js",
)
POLICY_CACHE_SECONDS = 300

# Offene Pflicht → Ziel der Umleitung
REQUIREMENT_TARGETS = {
    "totp": "accounts:two_factor_enroll",
    "security_key": "accounts:security_keys",
}

ADMIN_DENIED_HTML = (
    "<!doctype html><html lang='de'><head><meta charset='utf-8'><title>Kein Zugriff</title></head>"
    "<body style='font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem'>"
    "<h1>Kein Zugriff</h1><p>Der Administrationsbereich ist nur aus freigegebenen Netzen erreichbar "
    "(z. B. über das VPN).</p></body></html>"
)

GetResponse = Callable[[HttpRequest], HttpResponseBase]


class AdminNetworkMiddleware:
    """Django-Admin nur aus ``ADMIN_ALLOWED_NETWORKS`` (leer = keine Beschränkung)."""

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if request.path.startswith(ADMIN_PREFIXES):
            networks = admin_networks()
            if networks:
                ip = client_ip(request)
                if not ip_in_networks(ip, networks):
                    logger.warning(
                        "Admin-Zugriff aus nicht freigegebenem Netz abgewiesen",
                        extra={"client_ip": ip, "path": request.path},
                    )
                    return HttpResponseForbidden(ADMIN_DENIED_HTML)
        return self.get_response(request)


class TwoFactorEnforcementMiddleware:
    """Angemeldete Konten mit offener Pflicht (Authenticator-App bzw. Sicherheitsschlüssel) umleiten."""

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and not request.path.startswith(TWO_FACTOR_EXEMPT_PREFIXES):
            requirement = self._open_requirement(request, user)
            if requirement:
                return self._redirect_response(request, reverse(REQUIREMENT_TARGETS[requirement]))
        return self.get_response(request)

    @staticmethod
    def _open_requirement(request: HttpRequest, user: object) -> str:
        """``"totp"``, ``"security_key"`` oder ``""``; je Session fünf Minuten zwischengespeichert."""
        user_id = str(getattr(user, "pk", ""))
        now = int(time.time())
        cached = request.session.get(POLICY_CACHE_SESSION_KEY)
        if (
            isinstance(cached, dict)
            and cached.get("user") == user_id
            and now - int(cached.get("at", 0)) < POLICY_CACHE_SECONDS
        ):
            return str(cached.get("need", ""))
        need = ""
        if two_factor_required(user) and not TwoFactorService().is_2fa_enabled(user):
            need = "totp"
        elif security_key_required(user) and not has_credentials(user):
            need = "security_key"
        request.session[POLICY_CACHE_SESSION_KEY] = {"user": user_id, "at": now, "need": need}
        return need

    @staticmethod
    def _redirect_response(request: HttpRequest, target: str) -> HttpResponseBase:
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        accept = request.headers.get("Accept", "")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or accept.startswith("application/json"):
            return JsonResponse({"error": "two_factor_setup_required", "redirect": target}, status=403)
        return redirect(f"{target}?{urlencode({'next': request.get_full_path()})}")
