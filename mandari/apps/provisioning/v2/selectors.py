# SPDX-License-Identifier: AGPL-3.0-or-later
"""Provisioning-API v2: Ist-Zustand von Work-Organisationen lesen (Issue #1132)."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db.models import Count, Q
from django.urls import reverse

from apps.tenants.models import (
    PROVISIONING_STATUS_ACTIVE,
    PROVISIONING_STATUS_ENDED,
    PROVISIONING_STATUS_SUSPENDED,
    Organization,
)

_ACTIVE_MEMBERS = Q(memberships__is_active=True, memberships__is_guest=False)
_ACTIVE_GUESTS = Q(memberships__is_active=True, memberships__is_guest=True)


def actual_status(organization: Organization) -> str:
    """
    Status aus dem tatsächlichen Zustand: aktiv, solange die Organisation aktiv ist; sonst „beendet“,
    wenn das zuletzt so gesetzt wurde, und andernfalls „gesperrt“.

    So zeigt die Antwort auch Änderungen von Hand (etwa im Django-Admin), die ein Abgleich korrigieren kann.
    """
    if organization.is_active:
        return PROVISIONING_STATUS_ACTIVE
    if organization.provisioning_status == PROVISIONING_STATUS_ENDED:
        return PROVISIONING_STATUS_ENDED
    return PROVISIONING_STATUS_SUSPENDED


def work_url(organization: Organization) -> str:
    """Absolute Adresse des Arbeitsbereichs auf Basis von SITE_URL (nie der Host der Anfrage)."""
    base_url = str(getattr(settings, "SITE_URL", "")).rstrip("/")
    return f"{base_url}{reverse('work:dashboard', kwargs={'org_slug': organization.slug})}"


def find_by_ref(ref: str) -> Organization | None:
    return Organization.objects.select_related("body", "party_group").filter(provisioning_ref=ref).first()


def work_state(
    organization: Organization, *, member_count: int | None = None, guest_count: int | None = None
) -> dict[str, Any]:
    """Ist-Zustand einer Organisation als Antwort der API (Zählungen ohne Angabe per Abfrage)."""
    body = organization.body
    party = organization.party_group
    changed_at = organization.provisioning_status_changed_at
    return {
        "ref": organization.provisioning_ref,
        "slug": organization.slug,
        "name": organization.name,
        "status": actual_status(organization),
        "is_active": organization.is_active,
        "status_changed_at": changed_at.isoformat() if changed_at else None,
        "member_limit": organization.member_limit,
        "guest_limit": organization.guest_limit,
        # Reguläre Mitglieder zählen gegen member_limit, Gäste getrennt gegen guest_limit
        "member_count": organization.get_active_member_count() if member_count is None else member_count,
        "guest_count": organization.get_active_guest_count() if guest_count is None else guest_count,
        "work_url": work_url(organization),
        "billing_reference": organization.billing_reference,
        "plan": organization.plan,
        "body": {"id": str(body.pk), "ags": body.ags, "name": body.get_display_name()} if body else None,
        "party_group": {"id": str(party.pk), "name": party.name} if party else None,
        "created_at": organization.created_at.isoformat(),
        "updated_at": organization.updated_at.isoformat(),
    }


def list_work_states() -> list[dict[str, Any]]:
    """Alle Organisationen, auch ohne Referenz (``ref: null``), für den vollständigen Abgleich."""
    organizations = (
        Organization.objects.select_related("body", "party_group")
        .annotate(
            provisioning_member_count=Count("memberships", filter=_ACTIVE_MEMBERS),
            provisioning_guest_count=Count("memberships", filter=_ACTIVE_GUESTS),
        )
        .order_by("name", "slug")
    )
    return [
        work_state(
            organization,
            member_count=int(getattr(organization, "provisioning_member_count", 0)),
            guest_count=int(getattr(organization, "provisioning_guest_count", 0)),
        )
        for organization in organizations
    ]
