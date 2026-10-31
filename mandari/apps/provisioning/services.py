# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Bausteine der Provisioning-API (v1 und v2).

Anlegen einer Work-Organisation mit Einladung der künftigen Admin-Person und Versand dieser
Einladung. Beide API-Versionen nutzen denselben Weg, damit Rollen, Gültigkeit und Mail gleich bleiben.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.tenants.models import Organization, Role, UserInvitation

if TYPE_CHECKING:
    from apps.accounts.models import User

logger = logging.getLogger(__name__)

ADMIN_INVITATION_VALID_DAYS = 14
ADMIN_INVITATION_MESSAGE = "Willkommen bei mandari work! Über diese Einladung richtest du dein Konto ein."


def find_system_inviter() -> User | None:
    """Ältester aktiver Superuser als Absender der Einladung (``None``, wenn es keinen gibt)."""
    user_model: Any = get_user_model()
    inviter: User | None = user_model.objects.filter(is_superuser=True, is_active=True).order_by("date_joined").first()
    return inviter


def create_organization_with_admin_invitation(
    *, admin_email: str, inviter: User, **organization_fields: Any
) -> tuple[Organization, UserInvitation]:
    """
    Organisation anlegen und die Admin-Person einladen (14 Tage gültig, Administrator-Rolle).

    Muss in einer Transaktion laufen; die Mail verschickt erst :func:`send_admin_invitation` nach dem
    Commit. Die Standard-Rollen entstehen per post_save-Signal (``tenants/signals.py``).
    """
    organization = Organization.objects.create(**organization_fields)
    admin_role = Role.objects.filter(organization=organization, is_admin=True).order_by("priority").first()
    invitation = UserInvitation.create_for_organization(
        organization=organization,
        email=admin_email,
        invited_by=inviter,
        roles=[admin_role] if admin_role else None,
        message=ADMIN_INVITATION_MESSAGE,
        valid_days=ADMIN_INVITATION_VALID_DAYS,
    )
    return organization, invitation


def invitation_accept_url(invitation: UserInvitation) -> str:
    """Absolute Annahme-URL (gleiches Muster wie die Work-Einladung)."""
    base_url = str(getattr(django_settings, "SITE_URL", "https://mandari.de")).rstrip("/")
    return f"{base_url}{reverse('work:accept_invitation', kwargs={'token': invitation.token_for_link()})}"


def send_admin_invitation(organization: Organization, invitation: UserInvitation, accept_url: str) -> bool:
    """
    Einladungs-Mail an den künftigen Org-Admin im gemeinsamen Mail-Layout über den
    mandari-Standardversand (Site-Einstellungen). False bei Versandfehler (#239).
    """
    from apps.common import mail

    try:
        return mail.send_template(
            kind="plattform.einladung",
            subject=f"Deine neue mandari work Organisation: {organization.name}",
            template_name="emails/provisioning/admin_invitation",
            context={"organization": organization, "invitation": invitation, "accept_url": accept_url},
            to=[invitation.email],
            fail_silently=False,
        )
    except Exception as e:
        # Einladung bleibt in der DB; die Antwort meldet invitation_sent=False. Der Link geht nur
        # per Mail, nie über die Provisionierungs-Antwort.
        logger.warning(f"[Provisioning] Einladungs-Mail an {invitation.email} fehlgeschlagen: {e}")
        return False
