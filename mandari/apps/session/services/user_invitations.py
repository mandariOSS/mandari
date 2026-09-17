# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einladungen in den Sitzungsdienst: Versand im gemeinsamen Mail-Layout und
erneutes Senden mit verlängerter Gültigkeit (Issue #239).

Session-Mandanten haben kein eigenes SMTP; die Mail geht über den
mandari-Standardversand (Konfiguration aus den Site-Einstellungen). Der
Absendername nennt den Mandanten, die Absenderadresse bleibt die der Plattform.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from email.utils import parseaddr

from django.conf import settings as django_settings
from django.urls import reverse
from django.utils import timezone

from apps.common.email import get_from_email, send_template_email
from apps.session.models import SessionInvitation

logger = logging.getLogger(__name__)

INVITATION_VALID_DAYS = 7
TEMPLATE = "emails/session/user_invitation"


def accept_url(invitation: SessionInvitation) -> str:
    base_url = str(getattr(django_settings, "SITE_URL", "https://mandari.de")).rstrip("/")
    return f"{base_url}{reverse('session:invitation_accept', kwargs={'token': invitation.token})}"


def sender_for(tenant_name: str) -> str:
    """Absender „<Mandant> über mandari <adresse>“; die Adresse bleibt die der Plattform."""
    name, address = parseaddr(get_from_email())
    anzeigename = f"{tenant_name} über mandari".replace('"', "").strip()
    return f'"{anzeigename}" <{address}>' if address else get_from_email()


def send_user_invitation(invitation: SessionInvitation) -> bool:
    """Einladungsmail versenden; ``False`` bei Versandfehler (wird protokolliert)."""
    inviter = invitation.invited_by
    inviter_name = ""
    if inviter is not None:
        inviter_name = inviter.user.get_display_name() if getattr(inviter, "user", None) else str(inviter)
    context = {
        "tenant": invitation.tenant,
        "invitation": invitation,
        "inviter_name": inviter_name,
        "role_names": [role.name for role in invitation.roles.all()],
        "accept_url": accept_url(invitation),
    }
    try:
        return send_template_email(
            subject=f"Einladung zum Sitzungsdienst {invitation.tenant.name}",
            template_name=TEMPLATE,
            context=context,
            to=[invitation.email],
            from_email=sender_for(invitation.tenant.name),
            fail_silently=False,
        )
    except Exception:
        logger.exception("Einladungs-E-Mail an %s konnte nicht versendet werden.", invitation.email)
        return False


def resend_user_invitation(invitation: SessionInvitation, *, valid_days: int = INVITATION_VALID_DAYS) -> bool:
    """Offene Einladung erneut senden; die Gültigkeit läuft ab jetzt neu."""
    invitation.expires_at = timezone.now() + timedelta(days=valid_days)
    invitation.save(update_fields=["expires_at"])
    return send_user_invitation(invitation)
