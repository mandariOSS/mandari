# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zuordnung Work-Nutzer ↔ Session-Person für die Rückmeldung im Portal (Issue #225).

Session (Verwaltung) und Work (Fraktionen) sind getrennte Welten. Eine Person im Sitzungsdienst
gilt nur dann als Nutzerin bzw. Nutzer von mandari Work, wenn ALLE Bedingungen erfüllt sind:

1. Die Fraktion hat eine aktive Verbindung zur Verwaltung (AdministrationConnection) und das
   zugehörige Token der Verwaltung ist noch gültig – beide Seiten haben der Verbindung also
   zugestimmt, und die Verwaltung kann sie jederzeit beenden.
2. Das Work-Konto ist aktiv, kein Gastzugang, und seine E-Mail-Adresse ist bestätigt
   (``email_verified``).
3. Genau eine aktive Person des Mandanten trägt dieselbe E-Mail-Adresse (ohne Beachtung der
   Groß-/Kleinschreibung). Mehrdeutige Adressen ordnen nichts zu.

Die Zuordnung verleiht nicht mehr, als das Postfach ohnehin kann: Wer die bestätigte Adresse
kontrolliert, erhält auch den Rückmeldelink per Mail.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from apps.session.models import SessionPerson, SessionTenant

if TYPE_CHECKING:
    from apps.tenants.models import Organization


def _token_valid(connection: Any) -> bool:
    token = connection.get_token()
    return token is not None and bool(token.is_valid())


def tenant_for_organization(organization: Organization) -> SessionTenant | None:
    """Verwaltung, mit der die Fraktion aktiv verbunden ist (sonst None)."""
    from apps.work.motions.models import AdministrationConnection

    connection = (
        AdministrationConnection.objects.filter(organization=organization, is_active=True, tenant__is_active=True)
        .select_related("tenant")
        .first()
    )
    if connection is None or not _token_valid(connection):
        return None
    tenant: SessionTenant = connection.tenant
    return tenant


def person_for_user(user: Any, organization: Organization) -> SessionPerson | None:
    """Session-Person des angemeldeten Work-Nutzers – nur bei eindeutiger, sicherer Zuordnung."""
    if not getattr(user, "is_authenticated", False) or not user.is_active or not user.email_verified:
        return None
    email = str(user.email or "").strip()
    if not email:
        return None
    tenant = tenant_for_organization(organization)
    if tenant is None:
        return None
    persons = list(
        SessionPerson.objects.filter(tenant=tenant, is_active=True, email__iexact=email).select_related("tenant")[:2]
    )
    return persons[0] if len(persons) == 1 else None


def portal_person_ids(tenant: SessionTenant) -> set[Any]:
    """
    IDs aller Personen des Mandanten, die ihre Ladungen in mandari Work abrufen können.

    Grundlage für den Zustellweg „Portal“: Ohne Portalzugang fällt die Ladung auf E-Mail zurück.
    """
    from apps.tenants.models import Membership
    from apps.work.motions.models import AdministrationConnection

    connections = list(AdministrationConnection.objects.filter(tenant=tenant, is_active=True))
    organization_ids = [c.organization_id for c in connections if _token_valid(c)]
    if not organization_ids:
        return set()
    verified = {
        str(email).lower()
        for email in Membership.objects.filter(
            organization_id__in=organization_ids,
            is_active=True,
            is_guest=False,
            user__is_active=True,
            user__email_verified=True,
        ).values_list("user__email", flat=True)
    }
    if not verified:
        return set()
    persons = list(
        SessionPerson.objects.filter(tenant=tenant, is_active=True).exclude(email="").values_list("id", "email")
    )
    counts = Counter(str(email).lower() for _pk, email in persons)
    return {pk for pk, email in persons if counts[str(email).lower()] == 1 and str(email).lower() in verified}
