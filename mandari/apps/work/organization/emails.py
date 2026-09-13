# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugangs-Mails einer Organisation: Einladungen, Gastzugänge, Selbstregistrierung und Freischaltung.

Versand über den Weg, den die Organisation in den E-Mail-Einstellungen gewählt hat
(:func:`apps.common.org_email.send_org_email`): eigenes SMTP, sonst der mandari-Standardversand.
Links, mit denen sich ein Passwort setzen lässt, gehen immer über mandari. Antworten landen bei der
Kontaktadresse der Organisation, sofern hinterlegt. Jede Mail geht an genau eine Adresse, damit
Empfänger einander nicht sehen. Ein Versandfehler bricht den fachlichen Ablauf nie ab – er wird
protokolliert und als ``False`` gemeldet.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from django.conf import settings
from django.urls import reverse

from apps.accounts.two_factor_policy import two_factor_required
from apps.common.email import render_email, send_email
from apps.common.org_email import send_org_email

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.tenants.models import Membership, Organization

logger = logging.getLogger(__name__)

TEMPLATE_DIR = "work/organization/email"

AccessVariant = Literal["welcome", "approved", "reactivated"]

ACCESS_SUBJECTS: dict[str, str] = {
    "welcome": "Willkommen bei {name}",
    "approved": "Dein Zugang zu {name} ist freigeschaltet",
    "reactivated": "Dein Zugang zu {name} ist wieder aktiv",
}


def absolute_url(path: str) -> str:
    """Absolute URL auf Basis von SITE_URL – nie der Request-Host (Schutz vor Host-Header-Manipulation)."""
    return str(getattr(settings, "SITE_URL", "")).rstrip("/") + path


def send_organization_mail(
    organization: Organization,
    *,
    template: str,
    subject: str,
    to: str,
    context: Mapping[str, Any],
    reply_to: list[str] | None = None,
    via_organization: bool = True,
) -> bool:
    """Mail aus ``work/organization/email/<template>`` an eine Adresse senden; ``True`` bei Erfolg."""
    if not to:
        return False
    if reply_to is None and organization.contact_email:
        reply_to = [organization.contact_email]
    try:
        html_body, text_body = render_email(f"{TEMPLATE_DIR}/{template}", {"organization": organization, **context})
        if via_organization:
            return send_org_email(
                organization,
                subject=subject,
                body=text_body,
                html_body=html_body,
                to=[to],
                reply_to=reply_to,
                fail_silently=True,
            )
        return send_email(
            subject=subject, body=text_body, to=[to], html_body=html_body, reply_to=reply_to, fail_silently=True
        )
    except Exception:  # noqa: BLE001 – der Versand darf den fachlichen Ablauf nicht abbrechen
        logger.exception("Mail %s der Organisation %s konnte nicht versendet werden", template, organization.slug)
        return False


def organization_start_url(membership: Membership) -> str:
    """Einstieg nach der Anmeldung: Dashboard, für Gäste die freigegebenen Dokumente."""
    name = "work:guest_documents" if membership.is_guest else "work:dashboard"
    return absolute_url(reverse(name, kwargs={"org_slug": membership.organization.slug}))


# ---------------------------------------------------------------------------
# Selbstregistrierung
# ---------------------------------------------------------------------------


def send_registration_confirmation(organization: Organization, user: User, token: str, valid_hours: int) -> bool:
    """Bestätigungslink: Erst nach dem Klick entsteht eine Mitgliedschaft oder Anfrage."""
    confirm_url = absolute_url(
        reverse("accounts:self_register_confirm", kwargs={"org_slug": organization.slug, "token": token})
    )
    return send_organization_mail(
        organization,
        template="registration_confirm.html",
        subject=f"Bitte bestätige deine Registrierung bei {organization.name}",
        to=user.email,
        context={"user": user, "confirm_url": confirm_url, "valid_hours": valid_hours},
    )


def send_registration_received(membership: Membership) -> bool:
    """Eingangsbestätigung an die Person, deren Anfrage auf Freischaltung wartet."""
    organization = membership.organization
    return send_organization_mail(
        organization,
        template="registration_received.html",
        subject=f"Deine Registrierung bei {organization.name} ist eingegangen",
        to=membership.user.email,
        context={"user": membership.user, "membership": membership},
    )


def send_registration_request(membership: Membership, recipients: Iterable[User]) -> int:
    """Neue Anfrage an alle, die freischalten dürfen (je eine Mail); liefert die Zahl versendeter Mails."""
    organization = membership.organization
    applicant = membership.user
    context = {
        "applicant": applicant,
        "applicant_name": applicant.get_display_name(),
        "membership": membership,
        "default_role": organization.registration_default_role,
        "review_url": absolute_url(reverse("work:members", kwargs={"org_slug": organization.slug})),
    }
    sent = 0
    for recipient in recipients:
        if send_organization_mail(
            organization,
            template="registration_request.html",
            subject=f"Neue Registrierungsanfrage für {organization.name}",
            to=recipient.email,
            context={**context, "recipient": recipient},
            reply_to=[applicant.email],
        ):
            sent += 1
    return sent


def send_access_granted(membership: Membership, variant: AccessVariant) -> bool:
    """Zugang aktiv: sofort nach Bestätigung, nach Freischaltung oder nach Reaktivierung."""
    organization = membership.organization
    user = membership.user
    return send_organization_mail(
        organization,
        template="access_granted.html",
        subject=ACCESS_SUBJECTS[variant].format(name=organization.name),
        to=user.email,
        context={
            "user": user,
            "variant": variant,
            "start_url": organization_start_url(membership),
            "login_url": absolute_url(reverse("accounts:login")),
            "needs_two_factor": two_factor_required(user),
        },
    )


def send_registration_rejected(organization: Organization, user: User, reason: str) -> bool:
    """Ablehnung, optional mit Begründung der freigebenden Person."""
    return send_organization_mail(
        organization,
        template="registration_rejected.html",
        subject=f"Deine Registrierungsanfrage bei {organization.name}",
        to=user.email,
        context={"user": user, "reason": reason},
    )
