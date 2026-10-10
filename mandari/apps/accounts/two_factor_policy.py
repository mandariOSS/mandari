# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Richtlinie für den zweiten Faktor und Netzbeschränkung des Django-Admins.

Ein zweiter Faktor ist Pflicht für
- Superuser und Staff (Plattform-Administration),
- Work: die Administration und alle Mitglieder, die Mitglieder, Rollen, Einstellungen oder API-Zugänge
  verwalten dürfen (``WORK_VERWALTUNGSRECHTE``, wirksam aus Rollen und Einzelrechten abzüglich verweigerter
  Rechte), Mitglieder mit einer Rolle mit „2FA erforderlich" sowie alle Mitglieder einer Organisation mit
  „2FA für alle Mitglieder",
- Session: Nutzer mit Administrator-, Benutzer-, Einstellungs- oder Protokollrechten, Mitglieder einer
  Leitstelle (Mandantengruppe, Issue #317) sowie alle
  Nutzer eines Mandanten mit „2FA für alle Nutzer".

Allen übrigen Konten ohne zweiten Faktor empfiehlt Work ihn auf Start (``two_factor_recommended``).

Durchgesetzt wird nur bei ``TWO_FACTOR_ENFORCEMENT`` (Produktion); gemeinsam
genutzte Demo-Zugänge (``TWO_FACTOR_EXEMPT_EMAIL_DOMAINS``) sind ausgenommen.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Any, cast

from django.conf import settings
from django.db.models import Q
from django.http import HttpRequest

# Zwischengespeichertes Richtlinien-Ergebnis in der Session (Middleware)
POLICY_CACHE_SESSION_KEY = "auth_2fa_policy"

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

#: Work: Rechte, mit denen jemand Mitglieder, Rollen, Einstellungen oder API-Zugänge der Organisation verwaltet.
#: Wer eines davon wirksam hat, braucht einen zweiten Faktor. Gastzugänge (``guests.*``) zählen nicht dazu.
WORK_VERWALTUNGSRECHTE: tuple[str, ...] = (
    "members.invite",
    "members.edit",
    "members.remove",
    "members.manage_roles",
    "organization.edit",
    "organization.manage_roles",
    "organization.admin",
    "organization.api_tokens",
)


def _enforcement_enabled() -> bool:
    return bool(getattr(settings, "TWO_FACTOR_ENFORCEMENT", True))


def _is_exempt(user: Any) -> bool:
    # Plattform-Administration ist nie ausgenommen, gleich unter welcher Adresse
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return False
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
        .select_related("organization")
        .prefetch_related("roles__permissions", "individual_permissions", "denied_permissions")
    )
    reasons.extend(
        f"Organisation {membership.organization.name}" for membership in memberships if _work_pflicht(membership)
    )

    from apps.session.models import SessionUser

    session_users = (
        SessionUser.objects.filter(user=user, is_active=True, tenant__is_active=True)
        .filter(
            Q(roles__is_admin=True)
            | Q(roles__can_manage_users=True)
            | Q(roles__can_manage_settings=True)
            # Protokollzugriff (Issue #221): Lese- und Anmeldeprotokolle sind besonders schutzbedürftig
            | Q(roles__can_view_audit_log=True)
            | Q(roles__can_export_audit_log=True)
            | Q(tenant__require_2fa=True)
        )
        .select_related("tenant")
        .distinct()
    )
    reasons.extend(f"Verwaltung {session_user.tenant.name}" for session_user in session_users)

    # Leitstelle einer Mandantengruppe (Issue #317): mandantenübergreifende Sicht
    from apps.session.models import SessionTenantGroupMembership

    leitstellen = SessionTenantGroupMembership.objects.filter(
        user=user, is_active=True, group__is_active=True
    ).select_related("group")
    reasons.extend(f"Leitstelle {membership.group.name}" for membership in leitstellen)

    return list(dict.fromkeys(reasons))


def verwaltet_organisation(membership: Any) -> bool:
    """True, wenn die Mitgliedschaft zur Administration gehört oder eines der ``WORK_VERWALTUNGSRECHTE`` hat.

    Maßgeblich sind die wirksamen Rechte wie bei jeder Berechtigungsprüfung in Work: Rechte der Rollen und
    Einzelrechte, abzüglich verweigerter Rechte (``PermissionChecker``). Eine Administrator-Rolle zählt immer.
    """
    from apps.common.permissions import PermissionChecker

    checker = cast(Any, PermissionChecker)(membership)
    return bool(checker.is_admin() or checker.has_any_permission(list(WORK_VERWALTUNGSRECHTE)))


def _work_pflicht(membership: Any) -> bool:
    if membership.organization.require_2fa:
        return True
    if any(role.require_2fa for role in membership.roles.all()):
        return True
    return verwaltet_organisation(membership)


def two_factor_required(user: Any) -> bool:
    """True, wenn das Konto einen zweiten Faktor verwenden muss."""
    return bool(two_factor_reasons(user))


def two_factor_recommended(user: Any) -> bool:
    """True, wenn Work dem Konto einen zweiten Faktor empfiehlt: angemeldet, nicht ausgenommen, noch keiner da.

    Gilt für alle Konten ohne zweiten Faktor, auch für Pflichtkonten (die Pflicht setzt die Middleware durch).
    """
    if not getattr(user, "is_authenticated", False) or not _enforcement_enabled() or _is_exempt(user):
        return False
    from .services import TwoFactorService

    return not TwoFactorService().is_2fa_enabled(user)


def security_key_required(user: Any) -> bool:
    """Plattform-Administration nur mit Sicherheitsschlüssel (Schalter, erst nach Ausgabe der Schlüssel)."""
    return (
        bool(getattr(settings, "TWO_FACTOR_REQUIRE_SECURITY_KEY_FOR_SUPERUSERS", False))
        and bool(getattr(user, "is_authenticated", False))
        and _enforcement_enabled()
        and not _is_exempt(user)
        and bool(user.is_superuser)
    )


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
