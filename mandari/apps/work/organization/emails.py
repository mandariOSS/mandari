# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugangs-Mails einer Organisation: Einladungen, Gastzugänge, Selbstregistrierung und Freischaltung.

Versand über den Mail-Dienst (``apps.common.mail``) auf dem Weg, den die Organisation in den
E-Mail-Einstellungen gewählt hat: eigenes SMTP, sonst der mandari-Standardversand. Mailart
``work.zugang.<vorlage>``.
Links, mit denen sich ein Passwort setzen oder eine Adresse bestätigen lässt (Einladung, Bestätigung der
Selbstregistrierung, Gastzugang für neue Konten), gehen immer über mandari (``via_organization=False``):
Den Server einer Organisation trägt deren Verwaltung selbst ein, wer ihn betreibt, liest die Links mit.
Antworten landen bei der Kontaktadresse der Organisation, sofern hinterlegt. Jede Mail geht an genau eine Adresse, damit
Empfänger einander nicht sehen. Ein Versandfehler bricht den fachlichen Ablauf nie ab – er wird
protokolliert und als ``False`` gemeldet.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from django.conf import settings
from django.urls import reverse

from apps.accounts.orphaned_accounts import ABGELEHNT_TAGE
from apps.accounts.two_factor_policy import two_factor_required
from apps.common import mail
from apps.common.email import render_email

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.tenants.models import Membership, Organization

logger = logging.getLogger(__name__)

TEMPLATE_DIR = "work/organization/email"

AccessVariant = Literal["welcome", "approved", "reactivated"]

ACCESS_SUBJECTS: dict[str, str] = {
    "welcome": "Willkommen bei {name}",
    "approved": "Ihr Zugang zu {name} ist freigeschaltet",
    "reactivated": "Ihr Zugang zu {name} ist wieder aktiv",
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
        return mail.send(
            kind=f"work.zugang.{template.removesuffix('.html')}",
            subject=subject,
            body=text_body,
            html_body=html_body,
            to=[to],
            reply_to=reply_to,
            organization=organization,
            via_organization=via_organization,
            fail_silently=True,
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
    """Bestätigungslink über mandari: Erst nach dem Klick entsteht eine Mitgliedschaft oder Anfrage."""
    confirm_url = absolute_url(
        reverse("accounts:self_register_confirm", kwargs={"org_slug": organization.slug, "token": token})
    )
    return send_organization_mail(
        organization,
        template="registration_confirm.html",
        subject=f"Bitte bestätigen Sie Ihre Registrierung bei {organization.name}",
        to=user.email,
        context={"user": user, "confirm_url": confirm_url, "valid_hours": valid_hours},
        # Der Link bestätigt die Adresse – nie über fremde Mailserver
        via_organization=False,
    )


def send_registration_received(membership: Membership) -> bool:
    """Eingangsbestätigung an die Person, deren Anfrage auf Freischaltung wartet."""
    organization = membership.organization
    return send_organization_mail(
        organization,
        template="registration_received.html",
        subject=f"Ihre Registrierung bei {organization.name} ist eingegangen",
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
        subject=f"Ihre Registrierungsanfrage bei {organization.name}",
        to=user.email,
        context={"user": user, "reason": reason, "retention_days": ABGELEHNT_TAGE},
    )


# ---------------------------------------------------------------------------
# Auslauf „Nur Lesen, Löschung nach 30 Tagen“ (Issue #1237)
# ---------------------------------------------------------------------------
#
# Zugangsrelevant wie die Links oben: immer über mandari (``via_organization=False``), nie über ein eigenes SMTP der
# Organisation. Sie-Form wie die übrigen Zugangs-Mails.


def expiry_url(organization: Organization) -> str:
    """Auslauf-Seite der Organisation (Hinweis, Löschdatum, Download der eigenen Daten)."""
    return absolute_url(reverse("work:membership_expiry", kwargs={"org_slug": organization.slug}))


def send_deletion_scheduled(membership: Membership, deletion_date: str) -> bool:
    """Beim Setzen: Löschdatum und Weg zum Download der eigenen Daten."""
    organization = membership.organization
    return send_organization_mail(
        organization,
        template="deletion_scheduled.html",
        subject=f"Ihr Zugang zu {organization.name}: Löschung am {deletion_date}",
        to=membership.user.email,
        context={"user": membership.user, "deletion_date": deletion_date, "expiry_url": expiry_url(organization)},
        via_organization=False,
    )


def send_deletion_reminder(membership: Membership, deletion_date: str, days_left: int) -> bool:
    """Erinnerung vor der Löschung (7 und 3 Tage vorher)."""
    organization = membership.organization
    return send_organization_mail(
        organization,
        template="deletion_reminder.html",
        subject=f"Erinnerung: Ihr Zugang zu {organization.name} wird am {deletion_date} gelöscht",
        to=membership.user.email,
        context={
            "user": membership.user,
            "deletion_date": deletion_date,
            "days_left": days_left,
            "expiry_url": expiry_url(organization),
        },
        via_organization=False,
    )


def send_deletion_done(
    organization: Organization,
    *,
    email: str,
    name: str,
    documents_deleted: int,
    account_deleted: bool,
    backup_days: int,
) -> bool:
    """Bestätigung nach der Löschung; Adresse und Name sind vorher gesichert (das Konto kann schon weg sein)."""
    return send_organization_mail(
        organization,
        template="deletion_done.html",
        subject=f"Ihre Daten bei {organization.name} sind gelöscht",
        to=email,
        context={
            "name": name,
            "documents_deleted": documents_deleted,
            "account_deleted": account_deleted,
            "backup_days": backup_days,
        },
        via_organization=False,
    )
