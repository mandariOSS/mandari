# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Richtlinie für den zweiten Faktor und Netzbeschränkung des Django-Admins.

Ein zweiter Faktor ist Pflicht für
- Superuser und Staff (Plattform-Administration),
- Work: Mitglieder mit Administrator-Rolle oder einer Rolle mit „2FA erforderlich"
  sowie alle Mitglieder einer Organisation mit „2FA für alle Mitglieder",
- Session: Nutzer mit Administrator-, Benutzer- oder Einstellungsrechten sowie alle
  Nutzer eines Mandanten mit „2FA für alle Nutzer".

Durchgesetzt wird nur bei ``TWO_FACTOR_ENFORCEMENT`` (Produktion); gemeinsam
genutzte Demo-Zugänge (``TWO_FACTOR_EXEMPT_EMAIL_DOMAINS``) sind ausgenommen.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.db.models import Q
from django.http import HttpRequest

# Zwischengespeichertes Richtlinien-Ergebnis in der Session (Middleware)
POLICY_CACHE_SESSION_KEY = "auth_2fa_policy"

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _enforcement_enabled() -> bool:
    return bool(getattr(settings, "TWO_FACTOR_ENFORCEMENT", True))


def _is_exempt(user: Any) -> bool:
    domains = {str(d).lower().lstrip("@") for d in getattr(settings, "TWO_FACTOR_EXEMPT_EMAIL_DOMAINS", ()) if d}
    email = str(getattr(user, "email", "") or "").lower()
    return bool(domains) and email.rpartition("@")[2] in domains


def two_factor_reasons(user: Any) -> list[str]:
    """Gründe, aus denen das Konto einen zweiten Faktor braucht (leer = freiwillig)."""
    if not getattr(user, "is_authenticated", False) or not _enforcement_enabled() or _is_exempt(user):
        return []

    reasons: list[str] = []
    if user.is_superuser or user.is_staff:
        reasons.append("Plattform-Administration")

    from apps.tenants.models import Membership

    memberships = (
        Membership.objects.filter(user=user, is_active=True, organization__is_active=True)
        .filter(Q(roles__is_admin=True) | Q(roles__require_2fa=True) | Q(organization__require_2fa=True))
        .select_related("organization")
        .distinct()
    )
    reasons.extend(f"Organisation {membership.organization.name}" for membership in memberships)

    from apps.session.models import SessionUser

    session_users = (
        SessionUser.objects.filter(user=user, is_active=True, tenant__is_active=True)
        .filter(
            Q(roles__is_admin=True)
            | Q(roles__can_manage_users=True)
            | Q(roles__can_manage_settings=True)
            | Q(tenant__require_2fa=True)
        )
        .select_related("tenant")
        .distinct()
    )
    reasons.extend(f"Verwaltung {session_user.tenant.name}" for session_user in session_users)

    return list(dict.fromkeys(reasons))


def two_factor_required(user: Any) -> bool:
    """True, wenn das Konto einen zweiten Faktor verwenden muss."""
    return bool(two_factor_reasons(user))


def client_ip(request: HttpRequest) -> str:
    """Client-Adresse.

    Der vorgelagerte Reverse-Proxy (Caddy) ersetzt ``X-Forwarded-For`` durch die
    echte Adresse; vom Client mitgeschickte Werte kommen nicht an.
    """
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR", ""))
    if forwarded:
        return forwarded.split(",")[0].strip()
    return str(request.META.get("REMOTE_ADDR", ""))


@lru_cache(maxsize=8)
def _parse_networks(raw: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(item, strict=False) for item in raw)


def admin_networks() -> tuple[IPNetwork, ...]:
    """Freigegebene Netze für den Django-Admin (leer = keine Beschränkung)."""
    return _parse_networks(tuple(getattr(settings, "ADMIN_ALLOWED_NETWORKS", ()) or ()))


def ip_in_networks(ip: str, networks: tuple[IPNetwork, ...]) -> bool:
    """True, wenn die Adresse in einem der Netze liegt (IPv4-mapped IPv6 wird aufgelöst)."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address.version == network.version and address in network for network in networks)
