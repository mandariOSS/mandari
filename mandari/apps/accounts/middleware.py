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
    two_factor_required,
)

logger = logging.getLogger(__name__)

ADMIN_PREFIXES = ("/admin/",)

# Pfade, die ohne eingerichteten zweiten Faktor erreichbar bleiben (Einrichtung, Abmelden, Assets)
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
    """Angemeldete Konten mit 2FA-Pflicht ohne eingerichteten Faktor zur Einrichtung leiten."""

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not request.path.startswith(TWO_FACTOR_EXEMPT_PREFIXES)
            and self._needs_enrollment(request, user)
        ):
            return self._enrollment_response(request)
        return self.get_response(request)

    @staticmethod
    def _needs_enrollment(request: HttpRequest, user: object) -> bool:
        """Richtlinie prüfen; Ergebnis je Session fünf Minuten zwischenspeichern."""
        user_id = str(getattr(user, "pk", ""))
        now = int(time.time())
        cached = request.session.get(POLICY_CACHE_SESSION_KEY)
        if (
            isinstance(cached, dict)
            and cached.get("user") == user_id
            and now - int(cached.get("at", 0)) < POLICY_CACHE_SECONDS
        ):
            return bool(cached.get("enroll"))
        enroll = two_factor_required(user) and not TwoFactorService().is_2fa_enabled(user)
        request.session[POLICY_CACHE_SESSION_KEY] = {"user": user_id, "at": now, "enroll": enroll}
        return enroll

    @staticmethod
    def _enrollment_response(request: HttpRequest) -> HttpResponseBase:
        target = reverse("accounts:two_factor_enroll")
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        accept = request.headers.get("Accept", "")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or accept.startswith("application/json"):
            return JsonResponse({"error": "two_factor_setup_required", "redirect": target}, status=403)
        return redirect(f"{target}?{urlencode({'next': request.get_full_path()})}")
