# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schreibende Anwendungsfälle für Organisation, Mitglieder, Team und Profil (Issue #160).

Views parsen die Anfrage, prüfen Berechtigungen und rufen hier hinein. Fachliche
Fehler werden als ``ServiceError`` gemeldet (mit Meldungsstufe für ``django.contrib.messages``);
Erfolgsfälle liefern die Meldung oder das geänderte Objekt zurück. Pfade mit
mehreren Schreibzugriffen laufen in ``transaction.atomic``. E-Mail-Versand läuft
über ``apps.common.email`` (``fail_silently``), Benachrichtigungen über den
NotificationHub.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from django.conf import settings as django_settings
from django.contrib.messages import constants as message_levels
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, URLValidator
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common.email import send_email
from apps.tenants.models import (
    AdministrationContact,
    CouncilParty,
    Membership,
    Organization,
    Permission,
    Role,
    UserInvitation,
)

from . import selectors
from .models import DataExport, MemberAbsence, MemberChangeRequest

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.core.files.uploadedfile import UploadedFile

    from apps.work.faction.models import FactionMeetingSchedule
    from apps.work.notifications.models import NotificationPreference

logger = logging.getLogger(__name__)

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
GUEST_SHARE_LEVELS = [("view", "Lesen"), ("comment", "Kommentieren"), ("edit", "Bearbeiten")]
INVITATION_VALID_DAYS = 7


class ServiceError(Exception):
    """Fachlicher Fehler mit Meldung für die Oberfläche (``level`` wie ``messages.ERROR``)."""

    def __init__(self, message: str, level: int = message_levels.ERROR) -> None:
        super().__init__(message)
        self.level = level


def _hub() -> Any:
    """NotificationHub spät importieren (Zyklus work.notifications) und untypisiert durchreichen."""
    from apps.work.notifications.services import NotificationHub

    return NotificationHub


def _notification_types() -> Any:
    from apps.work.notifications.models import NotificationType

    return NotificationType


def display_name(user: User) -> str:
    """Anzeigename für Meldungen: voller Name, ersatzweise E-Mail."""
    return user.get_full_name() or user.email


def _site_url(default: str = "") -> str:
    return str(getattr(django_settings, "SITE_URL", default)).rstrip("/")


def _save_organization(organization: Organization, **kwargs: Any) -> None:
    """``Organization.save`` ist im Modell untypisiert überschrieben (Slug-/Schlüsselpflege)."""
    cast(Any, organization).save(**kwargs)


# ---------------------------------------------------------------------------
# Einladungen
# ---------------------------------------------------------------------------


def send_invitation_email(organization: Organization, invitation: UserInvitation) -> bool:
    """Einladungs-Mail mit Annahme-Link (SITE_URL, nicht Request-Host) versenden."""
    base_url = _site_url("https://volt.mandari.de")
    accept_url = base_url + reverse("work:accept_invitation", kwargs={"token": invitation.token})
    inviter = display_name(invitation.invited_by) if invitation.invited_by else ""
    valid_until = invitation.expires_at.strftime("%d.%m.%Y um %H:%M Uhr")
    subject = f"Einladung zu {organization.name}"

    html_message = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2>Einladung zu {organization.name}</h2>
            <p>Hallo,</p>
            <p>Sie wurden von <strong>{inviter}</strong>
               eingeladen, der Organisation <strong>{organization.name}</strong> auf Mandari Work beizutreten.</p>

            {f"<p><em>Nachricht: {invitation.message}</em></p>" if invitation.message else ""}

            <p>
                <a href="{accept_url}"
                   style="display: inline-block; padding: 12px 24px; background-color: #4f46e5;
                          color: white; text-decoration: none; border-radius: 6px;">
                    Einladung annehmen
                </a>
            </p>

            <p style="color: #666; font-size: 14px;">
                Diese Einladung ist gültig bis zum {valid_until}.
            </p>

            <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
            <p style="color: #999; font-size: 12px;">
                Falls Sie diese Einladung nicht erwartet haben, können Sie diese E-Mail ignorieren.
            </p>
        </body>
        </html>
        """

    plain_message = f"""
Einladung zu {organization.name}

Hallo,

Sie wurden von {inviter} eingeladen,
der Organisation {organization.name} auf Mandari Work beizutreten.

{f"Nachricht: {invitation.message}" if invitation.message else ""}

Klicken Sie auf folgenden Link, um die Einladung anzunehmen:
{accept_url}

Diese Einladung ist gültig bis zum {valid_until}.

Falls Sie diese Einladung nicht erwartet haben, können Sie diese E-Mail ignorieren.
        """

    success = send_email(
        subject=subject,
        body=plain_message,
        to=[invitation.email],
        html_body=html_message,
        fail_silently=True,  # Die Einladung bleibt auch ohne Mail bestehen
    )
    if not success:
        logger.error(f"Failed to send invitation email to {invitation.email}")
    return success


@transaction.atomic
def invite_member(organization: Organization, inviter: User, email: str, role_ids: list[str], message: str) -> str:
    """
    Mitglied einladen: bestehende inaktive Mitgliedschaft reaktivieren, sonst Einladung anlegen und mailen.

    Wirft ``ServiceError`` (Warnung) bei bereits aktivem Mitglied oder offener Einladung.
    Liefert die Erfolgsmeldung.
    """
    existing_user = selectors.find_user_by_email(email)
    if existing_user:
        existing_membership = selectors.find_membership(organization, existing_user)
        if existing_membership:
            if existing_membership.is_active:
                raise ServiceError(f"{email} ist bereits Mitglied dieser Organisation.", message_levels.WARNING)
            existing_membership.is_active = True
            existing_membership.save()
            return f"{email} wurde reaktiviert."

    if selectors.find_pending_invitation(organization, email):
        raise ServiceError(f"Eine Einladung für {email} ist bereits ausstehend.", message_levels.WARNING)

    roles = selectors.roles_by_ids(organization, role_ids) if role_ids else None
    try:
        invitation = UserInvitation.create_for_organization(
            organization=organization,
            email=email,
            invited_by=inviter,
            roles=roles,
            message=message,
            valid_days=INVITATION_VALID_DAYS,
        )
        send_invitation_email(organization, invitation)
    except Exception as exc:  # noqa: BLE001 — Fehler verständlich anzeigen
        raise ServiceError(f"Fehler beim Erstellen der Einladung: {exc}") from exc
    return f"Einladung an {email} wurde versendet."


def resend_invitation(organization: Organization, invitation: UserInvitation) -> None:
    """Ablauf um sieben Tage verlängern und Einladung erneut mailen."""
    invitation.expires_at = timezone.now() + timedelta(days=INVITATION_VALID_DAYS)
    invitation.save()
    send_invitation_email(organization, invitation)


def cancel_invitation(invitation: UserInvitation) -> str:
    """Offene Einladung löschen; liefert die betroffene E-Mail-Adresse."""
    email = invitation.email
    invitation.delete()
    return email


@transaction.atomic
def accept_invitation(invitation: UserInvitation, user: User) -> str:
    """
    Einladung annehmen: Mitgliedschaft anlegen oder reaktivieren, Rollen übernehmen,
    ggf. Eigentümer setzen, Einladung als angenommen markieren. Liefert die Meldung.
    """
    organization = invitation.organization
    existing = selectors.find_membership(organization, user)
    if existing:
        if existing.is_active:
            message = "Sie sind bereits Mitglied dieser Organisation."
        else:
            existing.is_active = True
            existing.save()
            message = f"Willkommen zurück bei {organization.name}!"
    else:
        membership = Membership.objects.create(
            user=user,
            organization=organization,
            invited_by=invitation.invited_by,
            invitation_accepted_at=timezone.now(),
        )
        if invitation.roles.exists():
            membership.roles.set(invitation.roles.all())
        if not organization.owner:
            organization.owner = user
            _save_organization(organization)
        message = f"Willkommen bei {organization.name}!"

    invitation.accepted_at = timezone.now()
    invitation.accepted_by = user
    invitation.save()
    return message


# ---------------------------------------------------------------------------
# Gäste
# ---------------------------------------------------------------------------


@dataclass
class GuestInviteResult:
    """Ergebnis einer Gast-Einladung für die Erfolgsmeldung."""

    email: str
    shared_documents: int
    shared_folders: int

    @property
    def message(self) -> str:
        text = f"Gastzugang für {self.email} wurde eingerichtet."
        if self.shared_documents:
            text += f" {self.shared_documents} Dokument(e) freigegeben."
        if self.shared_folders:
            text += f" {self.shared_folders} Ordner freigegeben."
        return text


def send_guest_access_email(
    organization: Organization,
    user: User,
    inviter: User,
    *,
    note: str = "",
    user_created: bool = False,
    shared_docs: Any = (),
    shared_folders: Any = (),
    share_level: str = "view",
) -> bool:
    """
    Gast-Mail: Passwort-Setz-Link (bestehender Reset-Mechanismus) bzw. Direktlink
    zu den freigegebenen Dokumenten — auch zum erneuten Versand (Issue #75).
    """
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    base_url = _site_url("https://mandari.de")

    if user_created or not user.has_usable_password():
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        target_url = base_url + reverse("accounts:password_reset_confirm", kwargs={"uidb64": uidb64, "token": token})
        action_hint = "Über den folgenden Link legen Sie Ihr Passwort fest und aktivieren Ihren Zugang:"
    else:
        target_url = base_url + reverse("work:guest_documents", kwargs={"org_slug": organization.slug})
        action_hint = "Ihre freigegebenen Dokumente finden Sie hier:"

    level_label = dict(GUEST_SHARE_LEVELS).get(share_level, share_level)
    share_lines = ""
    if shared_docs or shared_folders:
        items = [f"- Dokument: {motion.title}" for motion in shared_docs]
        items += [f"- Ordner: {folder.name} (inkl. Unterordner)" for folder in shared_folders]
        share_lines = f"Für Sie freigegeben (Stufe: {level_label}):\n" + "\n".join(items) + "\n\n"

    subject = f"Gastzugang für {organization.name}"
    plain_message = (
        f"Hallo,\n\n"
        f"{display_name(inviter)} hat Ihnen einen Gastzugang zur Organisation "
        f"{organization.name} auf Mandari Work eingerichtet.\n\n"
        f"Als Gast sehen Sie ausschließlich die Dokumente, die für Sie freigegeben wurden.\n\n"
        f"{f'Nachricht: {note}' + chr(10) + chr(10) if note else ''}"
        f"{share_lines}"
        f"{action_hint}\n{target_url}\n\n"
        f"Falls Sie diese E-Mail nicht erwartet haben, können Sie sie ignorieren.\n"
    )

    success = send_email(subject=subject, body=plain_message, to=[user.email], fail_silently=True)
    if not success:
        logger.error(f"Failed to send guest invitation email to {user.email}")
    return success


@transaction.atomic
def invite_guest(
    organization: Organization,
    inviter: Membership,
    *,
    email: str,
    note: str,
    share_level: str,
    document_ids: list[str],
    folder_ids: list[str],
) -> GuestInviteResult:
    """
    Gast einladen: Konto anlegen (falls nötig), Gast-Mitgliedschaft ohne Rollen, optionale
    Dokument-/Ordner-Freigaben (rekursiv) und Zugangs-Mail. Prüft das Gast-Limit.
    """
    from apps.work.motions.models import FolderGuestShare, MotionShare

    if share_level not in dict(GUEST_SHARE_LEVELS):
        share_level = "view"
    if not organization.has_free_guest_slot():
        raise ServiceError(f"Gast-Limit erreicht ({organization.guest_limit}). Erweiterung als Addon im Kundenportal.")

    inviter_user = inviter.user
    user = selectors.find_user_by_email(email)
    user_created = False
    if user is None:
        # Neuer Account ohne Passwort — Passwort-Setz-Mail folgt
        user = cast(Any, User.objects).create_user(email=email, password=None)
        user_created = True
    else:
        existing = selectors.find_membership(organization, user)
        if existing:
            if existing.is_active:
                raise ServiceError(f"{email} ist bereits Mitglied dieser Organisation.", message_levels.WARNING)
            raise ServiceError(
                f"{email} hat bereits eine deaktivierte Mitgliedschaft. Reaktivieren Sie diese in der Mitgliederliste.",
                message_levels.WARNING,
            )

    guest_membership = Membership.objects.create(
        user=user, organization=organization, is_guest=True, invited_by=inviter_user
    )
    hub = _hub()

    shared_docs = []
    if document_ids:
        for motion in selectors.shareable_documents(organization, inviter).filter(id__in=document_ids):
            MotionShare.objects.get_or_create(
                motion=motion,
                scope="user",
                user=user,
                defaults={"level": share_level, "created_by": inviter_user, "message": note},
            )
            shared_docs.append(motion)
            # In-App-Hinweis; die E-Mail bündelt alle Freigaben (keine Mail-Flut)
            hub.notify_document_shared(motion, guest_membership, share_level, inviter, send_email=False)

    shared_folders = []
    if folder_ids:
        shareable = {str(folder.id): folder for folder, _depth in selectors.shareable_folders(organization, inviter)}
        for folder_id in folder_ids:
            folder = shareable.get(str(folder_id))
            if folder is None:
                continue
            FolderGuestShare.objects.update_or_create(
                folder=folder, user=user, defaults={"level": share_level, "created_by": inviter_user}
            )
            shared_folders.append(folder)
            hub.notify_folder_shared(folder, guest_membership, share_level, inviter, send_email=False)

    send_guest_access_email(
        organization,
        user,
        inviter_user,
        note=note,
        user_created=user_created,
        shared_docs=shared_docs,
        shared_folders=shared_folders,
        share_level=share_level,
    )
    logger.info(
        f"[Guests] Gastzugang {email} in '{organization.slug}' angelegt "
        f"(von {inviter_user.email}, {len(shared_docs)} Dokument- und {len(shared_folders)} Ordner-Freigaben)"
    )
    return GuestInviteResult(email=email, shared_documents=len(shared_docs), shared_folders=len(shared_folders))


def resend_guest_access(organization: Organization, member: Membership, inviter: User, note: str) -> None:
    """Zugangs-Mail eines Gastes erneut senden (frischer Passwort-Link bzw. Direktlink)."""
    send_guest_access_email(
        organization,
        member.user,
        inviter,
        note=note,
        user_created=not member.user.has_usable_password(),
        shared_docs=selectors.guest_shared_documents(organization, member.user),
        shared_folders=selectors.guest_shared_folders(organization, member.user),
    )
    logger.info(f"[Guests] Zugang erneut gesendet an {member.user.email} in '{organization.slug}'")


# ---------------------------------------------------------------------------
# Mitglieder-Detail
# ---------------------------------------------------------------------------


def update_member_committees(organization: Organization, member: Membership, committee_ids: list[str]) -> None:
    """Gremienzuordnung setzen (nur Gremien der verknüpften Kommune(n))."""
    bodies = selectors.organization_bodies(organization)
    if not bodies.exists():
        raise ServiceError("Keine Kommune verknüpft. Gremien können nicht zugewiesen werden.")
    member.oparl_committees.set(selectors.committees_by_ids(bodies, committee_ids))


def update_member_expertise(organization: Organization, member: Membership, topic_ids: list[str]) -> None:
    """Fachgebiete aus dem Themenkatalog der Organisation setzen."""
    member.expertise_topics.set(selectors.topics_by_ids(organization, topic_ids))


def update_member_roles(organization: Organization, member: Membership, actor: Membership, role_ids: list[str]) -> None:
    """
    Rollen setzen. Rechte-Eskalation verhindern: Nicht-Admins dürfen weder ihre eigenen
    Rollen ändern noch die Administrator-Rolle vergeben oder entziehen.
    """
    if member.is_guest:
        raise ServiceError("Gast-Zugänge können keine Rollen erhalten.")
    roles = selectors.roles_by_ids(organization, role_ids)
    if not selectors.permission_checker(actor).is_admin():
        if member.user == actor.user:
            raise ServiceError("Eigene Rollen können nur Administratoren ändern.")
        if selectors.contains_admin_role(member.roles.all()) or selectors.contains_admin_role(roles):
            raise ServiceError("Nur Administratoren können die Administrator-Rolle vergeben oder entziehen.")
    member.roles.set(roles)


@transaction.atomic
def update_member_permissions(
    member: Membership, actor: Membership, individual_codes: list[str], denied_codes: list[str]
) -> None:
    """
    Individuelle/verweigerte Berechtigungen setzen — administrative Operation
    (kann bis organization.admin gewähren), daher Administratoren vorbehalten.
    """
    if member.is_guest:
        raise ServiceError("Gast-Zugänge haben keine Berechtigungen.")
    if not selectors.permission_checker(actor).is_admin():
        raise ServiceError("Individuelle Berechtigungen können nur Administratoren ändern.")
    member.individual_permissions.set(selectors.permissions_by_codenames(individual_codes))
    member.denied_permissions.set(selectors.permissions_by_codenames(denied_codes))


def deactivate_member(organization: Organization, member: Membership, actor_user: User) -> None:
    """Mitglied deaktivieren (Soft-Delete); Eigentümer und man selbst sind ausgenommen."""
    if member.user == organization.owner:
        raise ServiceError("Der Eigentümer kann nicht deaktiviert werden.")
    if member.user == actor_user:
        raise ServiceError("Sie können sich nicht selbst deaktivieren.")
    member.is_active = False
    member.save()


def reactivate_member(organization: Organization, member: Membership) -> None:
    """Mitglied reaktivieren; das Gast-Limit gilt auch hier (sonst per Deaktivieren/Reaktivieren umgehbar)."""
    if member.is_guest and not member.is_active and not organization.has_free_guest_slot():
        raise ServiceError(f"Gast-Limit erreicht ({organization.guest_limit}). Erweiterung als Addon im Kundenportal.")
    member.is_active = True
    member.save()


def remove_member(organization: Organization, member: Membership, actor_user: User) -> str:
    """Mitglied endgültig entfernen; liefert den Anzeigenamen für die Meldung."""
    if member.user == organization.owner:
        raise ServiceError("Der Eigentümer kann nicht entfernt werden.")
    if member.user == actor_user:
        raise ServiceError("Sie können sich nicht selbst entfernen.")
    name = display_name(member.user)
    member.delete()
    return name


def transfer_ownership(organization: Organization, member: Membership, actor_user: User) -> None:
    """Eigentümerschaft übertragen — nur durch den aktuellen Eigentümer."""
    if organization.owner != actor_user:
        raise ServiceError("Nur der aktuelle Eigentümer kann die Eigentümerschaft übertragen.")
    organization.owner = member.user
    _save_organization(organization)


def link_oparl_person(organization: Organization, member: Membership, person_id: str | None) -> str:
    """RIS-Person (innerhalb der Körperschaften) verknüpfen; liefert deren Anzeigenamen."""
    bodies = selectors.organization_bodies(organization)
    if not bodies.exists() or not person_id:
        raise ServiceError("Keine Kommune verknüpft oder ungültige Person.")
    oparl_person = selectors.get_oparl_person_or_404(bodies, person_id)
    member.oparl_person = oparl_person
    member.save()
    return str(oparl_person.display_name)


def unlink_oparl_person(member: Membership) -> None:
    """RIS-Verknüpfung entfernen."""
    member.oparl_person = None
    member.save()


def apply_committee_suggestions(organization: Organization, member: Membership) -> int:
    """Gremien aus den aktiven RIS-Mitgliedschaften der verknüpften Person übernehmen; liefert die Anzahl."""
    bodies = selectors.organization_bodies(organization)
    if not bodies.exists() or not member.oparl_person:
        raise ServiceError("Keine RIS-Person verknüpft oder keine Kommune zugeordnet.")
    suggested = selectors.suggested_committees(member.oparl_person, bodies, timezone.now().date())
    member.oparl_committees.set(suggested)
    return len(suggested)


def update_sworn_in(member: Membership, actor: Membership, is_sworn_in: bool) -> None:
    """
    Vereidigungsstatus setzen. Selbst-Vereidigung verhindern: der Status schaltet den Zugriff
    auf nicht-öffentliche Inhalte frei und darf von Nicht-Admins nicht am eigenen Konto gesetzt werden.
    """
    if member.user == actor.user and not selectors.permission_checker(actor).is_admin():
        raise ServiceError("Den eigenen Vereidigungsstatus können nur Administratoren setzen.")
    member.is_sworn_in = is_sworn_in
    member.save()


def approve_registration(membership: Membership) -> None:
    """Ausstehende Selbstregistrierung freischalten."""
    membership.is_active = True
    membership.save(update_fields=["is_active"])


def reject_registration(membership: Membership) -> str:
    """Ausstehende Selbstregistrierung ablehnen (löschen); liefert den Anzeigenamen."""
    name = membership.user.get_display_name()
    membership.delete()
    return name


# ---------------------------------------------------------------------------
# Änderungsanträge
# ---------------------------------------------------------------------------


REQUEST_DATA_KEYS = {
    "role_change": "requested_roles",
    "committee_change": "requested_committees",
    "permission_request": "requested_permissions",
}


def _requests_link(organization: Organization) -> str:
    return f"/work/{organization.slug}/profile/requests/"


@transaction.atomic
def submit_change_request(
    organization: Organization, membership: Membership, request_type: str | None, reason: str, requested: list[str]
) -> MemberChangeRequest:
    """Änderungsantrag einreichen und Administratoren benachrichtigen."""
    if not request_type or not reason:
        raise ServiceError("Antragstyp und Begründung sind erforderlich.")
    data_key = REQUEST_DATA_KEYS.get(request_type)
    if data_key is None:
        raise ServiceError("Ungültiger Antragstyp.")

    change_request = MemberChangeRequest.objects.create(
        organization=organization,
        requester=membership,
        request_type=request_type,
        request_data={data_key: requested},
        reason=reason,
    )
    _hub().send_bulk(
        recipients=selectors.admin_members(organization, exclude=membership),
        notification_type=_notification_types().CHANGE_REQUEST_NEW,
        title="Neuer Änderungsantrag",
        message=f"{display_name(membership.user)} hat einen {change_request.get_request_type_display()} eingereicht.",
        link=_requests_link(organization),
        actor=membership,
    )
    return change_request


def withdraw_change_request(organization: Organization, membership: Membership, request_id: Any) -> None:
    """Eigenen offenen Antrag zurückziehen."""
    change_request = selectors.get_pending_change_request_or_404(organization, request_id, requester=membership)
    change_request.status = "withdrawn"
    change_request.save()


def approver_may_apply(organization: Organization, change_request: MemberChangeRequest, approver: Membership) -> bool:
    """
    Darf ein Nicht-Admin diesen Antrag genehmigen?

    Nein bei eigenem Antrag, bei Vergabe direkter Berechtigungen und bei
    role_change, der eine Administrator-Rolle enthält.
    """
    if change_request.requester == approver:
        return False
    if change_request.request_type == "permission_request":
        return False
    if change_request.request_type == "role_change":
        role_ids = change_request.request_data.get("requested_roles", [])
        if Role.objects.filter(id__in=role_ids, organization=organization, is_admin=True).exists():
            return False
    return True


def _apply_change(organization: Organization, change_request: MemberChangeRequest) -> None:
    """Genehmigten Antrag anwenden (Rollen, Gremien oder Einzelberechtigungen)."""
    requester = change_request.requester
    data = change_request.request_data

    if change_request.request_type == "role_change":
        role_ids = data.get("requested_roles", [])
        if role_ids:
            requester.roles.set(selectors.roles_by_ids(organization, role_ids))
    elif change_request.request_type == "committee_change":
        bodies = selectors.organization_bodies(organization)
        if bodies.exists():
            requester.oparl_committees.set(selectors.committees_by_ids(bodies, data.get("requested_committees", [])))
    elif change_request.request_type == "permission_request":
        perm_codes = data.get("requested_permissions", [])
        if perm_codes:
            for perm in selectors.permissions_by_codenames(perm_codes):
                requester.individual_permissions.add(perm)


def _ensure_reviewer(membership: Membership) -> Any:
    checker = selectors.permission_checker(membership)
    if not selectors.can_review_change_requests(membership):
        raise ServiceError("Keine Berechtigung.")
    return checker


@transaction.atomic
def approve_change_request(organization: Organization, decider: Membership, request_id: Any) -> MemberChangeRequest:
    """
    Antrag genehmigen und anwenden. Rechte-Eskalation über den Antragsweg verhindern
    (gleiche Invariante wie im Mitglieder-Detail); Antragsteller wird benachrichtigt.
    """
    checker = _ensure_reviewer(decider)
    change_request = selectors.get_pending_change_request_or_404(organization, request_id)
    if not checker.is_admin() and not approver_may_apply(organization, change_request, decider):
        raise ServiceError(
            "Diese Genehmigung ist Administratoren vorbehalten (eigener Antrag oder Vergabe administrativer Rechte)."
        )

    _apply_change(organization, change_request)
    change_request.status = "approved"
    change_request.decided_by = decider
    change_request.decided_at = timezone.now()
    change_request.save()

    _hub().send(
        recipient=change_request.requester,
        notification_type=_notification_types().CHANGE_REQUEST_DECIDED,
        title="Antrag genehmigt",
        message=(f"Ihr {change_request.get_request_type_display()} wurde von {display_name(decider.user)} genehmigt."),
        link=_requests_link(organization),
        actor=decider,
    )
    return change_request


@transaction.atomic
def reject_change_request(
    organization: Organization, decider: Membership, request_id: Any, comment: str
) -> MemberChangeRequest:
    """Antrag ablehnen (mit optionalem Kommentar); Antragsteller wird benachrichtigt."""
    _ensure_reviewer(decider)
    change_request = selectors.get_pending_change_request_or_404(organization, request_id)
    change_request.status = "rejected"
    change_request.decided_by = decider
    change_request.decided_at = timezone.now()
    change_request.decision_comment = comment
    change_request.save()

    msg = f"Ihr {change_request.get_request_type_display()} wurde von {display_name(decider.user)} abgelehnt."
    if comment:
        msg += f" Kommentar: {comment}"
    _hub().send(
        recipient=change_request.requester,
        notification_type=_notification_types().CHANGE_REQUEST_DECIDED,
        title="Antrag abgelehnt",
        message=msg,
        link=_requests_link(organization),
        actor=decider,
    )
    return change_request


# ---------------------------------------------------------------------------
# Ratsfraktionen und Verwaltungskontakte
# ---------------------------------------------------------------------------


@dataclass
class PartyInput:
    """Formulardaten einer Ratsfraktion."""

    name: str
    short_name: str
    email: str = ""
    contact_name: str = ""
    contact_phone: str = ""
    color: str = "#6b7280"
    is_coalition_member: bool = False
    coalition_order: int = 0
    is_active: bool = True


def update_coalition_name(organization: Organization, coalition_name: str) -> None:
    """Koalitionsname speichern."""
    organization.coalition_name = coalition_name
    _save_organization(organization, update_fields=["coalition_name"])


def add_admin_contact(organization: Organization, label: str, email: str) -> AdministrationContact:
    """Verwaltungskontakt anlegen."""
    if not label or not email:
        raise ServiceError("Bezeichnung und E-Mail sind erforderlich.")
    return AdministrationContact.objects.create(organization=organization, label=label, email=email)


def delete_admin_contact(organization: Organization, contact_id: Any) -> bool:
    """Verwaltungskontakt entfernen; ``False``, wenn keiner gelöscht wurde."""
    deleted, _ = AdministrationContact.objects.filter(id=contact_id, organization=organization).delete()
    return bool(deleted)


def add_party(organization: Organization, data: PartyInput) -> CouncilParty:
    """Ratsfraktion anlegen (Kurzname je Organisation eindeutig)."""
    if not data.name or not data.short_name:
        raise ServiceError("Name und Kurzname sind erforderlich.")
    if selectors.party_short_name_exists(organization, data.short_name):
        raise ServiceError(f"Kurzname '{data.short_name}' existiert bereits.")
    return CouncilParty.objects.create(
        organization=organization,
        name=data.name,
        short_name=data.short_name,
        email=data.email,
        contact_name=data.contact_name,
        contact_phone=data.contact_phone,
        color=data.color,
        is_coalition_member=data.is_coalition_member,
        coalition_order=data.coalition_order,
    )


def update_party(organization: Organization, party: CouncilParty, data: PartyInput) -> None:
    """Ratsfraktion aktualisieren (Kurzname je Organisation eindeutig)."""
    if not data.name or not data.short_name:
        raise ServiceError("Name und Kurzname sind erforderlich.")
    if selectors.party_short_name_exists(organization, data.short_name, exclude_id=party.id):
        raise ServiceError(f"Kurzname '{data.short_name}' existiert bereits.")
    party.name = data.name
    party.short_name = data.short_name
    party.email = data.email
    party.contact_name = data.contact_name
    party.contact_phone = data.contact_phone
    party.color = data.color
    party.is_coalition_member = data.is_coalition_member
    party.coalition_order = data.coalition_order
    party.is_active = data.is_active
    party.save()


def delete_party(party: CouncilParty) -> str:
    """Ratsfraktion löschen; liefert den Namen für die Meldung."""
    name = party.name
    party.delete()
    return name


# ---------------------------------------------------------------------------
# E-Mail-, API- und Registrierungs-Einstellungen
# ---------------------------------------------------------------------------


@dataclass
class EmailSettingsInput:
    """Formulardaten der Absender-/SMTP-Einstellungen (Issue #65)."""

    mail_sender_mode: str
    smtp_fallback_to_mandari: bool
    smtp_host: str
    smtp_port_raw: str
    smtp_username: str
    smtp_use_tls: bool
    smtp_from_email: str
    smtp_from_name: str
    smtp_password: str
    smtp_password_clear: bool


def save_email_settings(organization: Organization, data: EmailSettingsInput) -> bool:
    """
    E-Mail-Einstellungen speichern. Passwort nur überschreiben, wenn ein neues eingegeben wurde —
    Ablage ausschließlich über den verschlüsselnden Accessor. Liefert ``True``, wenn eigenes SMTP
    aktiv ist, aber kein Server hinterlegt wurde (Hinweis für die Oberfläche).
    """
    mode = data.mail_sender_mode if data.mail_sender_mode in dict(organization.MAIL_SENDER_MODE_CHOICES) else "mandari"
    if data.smtp_from_email:
        try:
            EmailValidator()(data.smtp_from_email)
        except ValidationError as exc:
            raise ServiceError("Ungültige Absender-Adresse.") from exc
    try:
        port = max(1, min(int(data.smtp_port_raw or 587), 65535))
    except (TypeError, ValueError):
        port = 587

    organization.mail_sender_mode = mode
    organization.smtp_fallback_to_mandari = data.smtp_fallback_to_mandari
    organization.smtp_host = data.smtp_host
    organization.smtp_port = port
    organization.smtp_username = data.smtp_username
    organization.smtp_use_tls = data.smtp_use_tls
    organization.smtp_from_email = data.smtp_from_email
    organization.smtp_from_name = data.smtp_from_name
    if data.smtp_password:
        organization.set_smtp_password(data.smtp_password)
    elif data.smtp_password_clear:
        organization.set_smtp_password("")
    _save_organization(organization)
    return mode == "smtp" and not organization.smtp_host


def send_test_email(organization: Organization, recipient: str) -> str:
    """Testmail über den konfigurierten Versandweg senden (Issue #65); liefert die Erfolgsmeldung."""
    from apps.common.org_email import OrgMailError, send_org_email

    if not recipient:
        raise ServiceError("Dein Benutzerkonto hat keine E-Mail-Adresse.")
    route = "eigenes SMTP" if organization.mail_sender_mode == "smtp" and organization.smtp_host else "mandari-Standard"
    body = "\n".join(
        [
            "Hallo,",
            "",
            f"dies ist eine Testmail von {organization.name} über den Versandweg: {route}.",
            "Wenn diese Nachricht ankommt, funktioniert der konfigurierte Versand.",
            "",
            "Viele Grüße,",
            "mandari",
        ]
    )
    try:
        ok = send_org_email(
            organization,
            subject=f"Testmail: E-Mail-Versand von {organization.name}",
            body=body,
            to=[recipient],
            fail_silently=False,
        )
    except OrgMailError as exc:
        logger.warning("Testmail über Organisations-SMTP fehlgeschlagen (org=%s): %s", organization.slug, exc)
        raise ServiceError(
            "Testmail fehlgeschlagen: Der Versand über das eigene SMTP war nicht möglich "
            "(kein Fallback konfiguriert). Bitte Zugangsdaten prüfen."
        ) from exc
    except Exception as exc:  # noqa: BLE001 — Fehler verständlich anzeigen
        logger.warning("Testmail fehlgeschlagen (org=%s): %s", organization.slug, exc)
        raise ServiceError("Testmail fehlgeschlagen. Bitte Konfiguration prüfen.") from exc
    if not ok:
        raise ServiceError("Testmail konnte nicht versendet werden. Bitte Konfiguration prüfen.")
    return f"Testmail an {recipient} versendet (Versandweg: {route})."


@dataclass
class ApiSettingsInput:
    """Formulardaten der öffentlichen Fraktions-API."""

    is_enabled: bool
    show_location: bool
    show_agenda: bool
    past_days: str | None
    future_days: str | None
    cache_seconds: str | None
    allowed_origins_raw: str


def _bounded_int(raw: str | None, current: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(raw if raw is not None else current), hi))
    except (TypeError, ValueError):
        return current


def _log_api_event(organization: Organization, membership: Membership, access: Any, changes: dict[str, Any]) -> None:
    from apps.work.faction.audit import log_event

    cast(Any, log_event)(
        "api_settings_changed",
        access,
        organization=organization,
        membership=membership,
        is_internal=False,
        changes=changes,
    )


def save_api_settings(organization: Organization, membership: Membership, data: ApiSettingsInput) -> bool:
    """Alle API-Optionen speichern (auditiert); liefert, ob die API danach aktiv ist."""
    from apps.work.faction.models import FactionPublicApiAccess

    access = FactionPublicApiAccess.for_organization(organization)
    access.is_enabled = data.is_enabled
    access.show_location = data.show_location
    access.show_agenda = data.show_agenda
    access.past_days = _bounded_int(data.past_days, access.past_days, 0, 3650)
    access.future_days = _bounded_int(data.future_days, access.future_days, 1, 3650)
    access.cache_seconds = _bounded_int(data.cache_seconds, access.cache_seconds, 0, 86400)

    # CORS-Origins: nur http(s)-Ursprünge übernehmen
    origins = []
    for candidate in data.allowed_origins_raw.replace("\n", ",").split(","):
        candidate = candidate.strip().rstrip("/")
        if candidate.startswith(("https://", "http://")) and " " not in candidate:
            origins.append(candidate)
    access.allowed_origins = ", ".join(dict.fromkeys(origins))

    access.save(
        update_fields=[
            "is_enabled",
            "past_days",
            "future_days",
            "show_location",
            "show_agenda",
            "cache_seconds",
            "allowed_origins",
            "updated_at",
        ]
    )
    _log_api_event(
        organization,
        membership,
        access,
        {
            "is_enabled": access.is_enabled,
            "past_days": access.past_days,
            "future_days": access.future_days,
            "show_location": access.show_location,
            "show_agenda": access.show_agenda,
            "cache_seconds": access.cache_seconds,
            "allowed_origins": access.allowed_origins,
        },
    )
    return bool(access.is_enabled)


def regenerate_api_token(organization: Organization, membership: Membership) -> None:
    """API-Token erneuern — bisherige URLs werden sofort ungültig (auditiert)."""
    from apps.work.faction.models import FactionPublicApiAccess

    access = FactionPublicApiAccess.for_organization(organization)
    cast(Any, access).regenerate()
    _log_api_event(organization, membership, access, {"token": "erneuert"})


def save_registration_settings(
    organization: Organization, *, enabled: bool, auto_approve: bool, domains_text: str, default_role_id: str
) -> None:
    """Selbstregistrierungs-Einstellungen speichern (Domains eine pro Zeile, bereinigt)."""
    organization.registration_enabled = enabled
    organization.registration_auto_approve = auto_approve
    organization.registration_email_domains = [
        d.strip().lower().lstrip("@") for d in domains_text.splitlines() if d.strip()
    ]
    organization.registration_default_role = (
        selectors.find_role(organization, default_role_id) if default_role_id else None
    )
    _save_organization(
        organization,
        update_fields=[
            "registration_enabled",
            "registration_email_domains",
            "registration_auto_approve",
            "registration_default_role",
        ],
    )


def update_two_factor_requirement(organization: Organization, *, required: bool) -> None:
    """Zwei-Faktor-Pflicht für alle Mitglieder ein- oder ausschalten.

    Mitglieder mit Administrator-Rolle oder einer Rolle mit „2FA erforderlich" sind
    unabhängig davon immer verpflichtet (apps/accounts/two_factor_policy.py).
    """
    if organization.require_2fa != required:
        organization.require_2fa = required
        _save_organization(organization, update_fields=["require_2fa"])


def disconnect_ris(organization: Organization) -> bool:
    """Verbindung zur Verwaltung trennen; ``False``, wenn keine besteht."""
    from apps.work.motions import ris_submission

    connection = cast(Any, ris_submission).get_connection(organization)
    if connection is None:
        return False
    connection.is_active = False
    connection.save(update_fields=["is_active"])
    return True


# ---------------------------------------------------------------------------
# Datenschutz, Profil, Konto
# ---------------------------------------------------------------------------


def start_data_export(organization: Organization, membership: Membership, export_format: str) -> DataExport:
    """DSGVO-Export asynchron anstoßen (kein Doppel-Export während eines laufenden)."""
    from apps.work.background_tasks import generate_dsgvo_export_task

    if selectors.has_active_export(organization, membership):
        raise ServiceError(
            "Es läuft bereits ein Export. Bitte warten Sie, bis dieser abgeschlossen ist.", message_levels.INFO
        )
    if export_format not in ("json", "pdf"):
        export_format = "json"
    export = DataExport.objects.create(organization=organization, membership=membership, export_format=export_format)
    cast(Any, generate_dsgvo_export_task).enqueue(str(export.id))
    return export


def request_account_deletion(organization: Organization, membership: Membership, password: str) -> None:
    """Mitgliedschaft deaktivieren (Soft-Delete) nach Passwortprüfung; Eigentümer müssen zuvor übertragen."""
    user = membership.user
    if organization.owner == user:
        raise ServiceError(
            "Als Eigentümer müssen Sie zuerst die Eigentümerschaft übertragen, bevor Sie Ihr Konto löschen können."
        )
    if not user.check_password(password):
        raise ServiceError("Falsches Passwort.")
    membership.is_active = False
    membership.save()


def delete_export(export: DataExport) -> None:
    """Export samt Datei löschen."""
    cast(Any, export).delete_file()
    export.delete()


def update_profile(
    user: User, *, first_name: str, last_name: str, phone: str, avatar: UploadedFile[Any] | None
) -> None:
    """Profildaten und optional das Profilbild (JPG/PNG/WebP, max. 5 MB) speichern."""
    user.first_name = first_name
    user.last_name = last_name
    user.phone = phone
    if avatar is not None:
        if avatar.content_type not in IMAGE_TYPES or (avatar.size or 0) > MAX_IMAGE_BYTES:
            raise ServiceError("Bild muss JPG, PNG oder WebP sein und max. 5 MB groß.")
        if user.avatar:
            user.avatar.delete(save=False)
        user.avatar = avatar
    user.save()


def remove_avatar(user: User) -> bool:
    """Profilbild entfernen; ``False``, wenn keines vorhanden war."""
    if not user.avatar:
        return False
    user.avatar.delete(save=False)
    user.avatar = None
    user.save()
    return True


def regenerate_calendar_feed(user: User) -> None:
    """Persönlichen iCal-Feed-Token erneuern (Issue #70) — die bisherige Feed-URL wird sofort ungültig."""
    from apps.work.faction.models import CalendarFeedToken

    cast(Any, CalendarFeedToken.for_user(user)).regenerate()


def remove_trusted_device(user: User, device_id: str) -> bool:
    """Vertrauenswürdiges Gerät entfernen; ``False``, wenn nicht gefunden."""
    device = selectors.find_trusted_device(user, device_id)
    if device is None:
        return False
    device.delete()
    return True


def save_profile_visibility(
    user: User, *, bio: str, show_email: bool, show_phone: bool, preferred_contact: str, contact_signal: str
) -> None:
    """Sichtbarkeits- und Kontaktangaben in ``User.settings["profile"]`` speichern."""
    settings = user.settings or {}
    profile = settings.get("profile", {})
    profile["bio"] = bio[:500]
    profile["show_email"] = show_email
    profile["show_phone"] = show_phone
    profile["preferred_contact"] = preferred_contact
    profile["contact_signal"] = contact_signal[:100]
    settings["profile"] = profile
    user.settings = settings
    user.save(update_fields=["settings"])


def save_followed_committees(organization: Organization, membership: Membership, committee_ids: list[str]) -> None:
    """„Meine Gremien“ setzen (nur Gremien der verknüpften Kommune(n))."""
    bodies = selectors.organization_bodies(organization)
    membership.followed_organizations.set(selectors.committees_by_ids(bodies, committee_ids))


# ---------------------------------------------------------------------------
# Benachrichtigungen
# ---------------------------------------------------------------------------


def ensure_notification_preferences(membership: Membership) -> NotificationPreference:
    """Benachrichtigungseinstellungen des Mitglieds holen oder anlegen."""
    from apps.work.notifications.models import NotificationPreference

    prefs, _ = NotificationPreference.objects.get_or_create(membership=membership)
    return prefs


def save_notification_preferences(membership: Membership, form: Mapping[str, str]) -> NotificationPreference:
    """Kanal-, Ruhezeit- und Typ-Einstellungen aus dem Formular übernehmen."""
    prefs = ensure_notification_preferences(membership)
    prefs.email_enabled = form.get("email_enabled") == "on"
    prefs.email_digest = form.get("email_digest", "instant")
    prefs.quiet_hours_enabled = form.get("quiet_hours_enabled") == "on"
    if prefs.quiet_hours_enabled:
        start = form.get("quiet_hours_start")
        end = form.get("quiet_hours_end")
        if start:
            prefs.quiet_hours_start = start
        if end:
            prefs.quiet_hours_end = end
    prefs.type_settings = {
        ntype: {
            "in_app": form.get(f"type_{ntype}_in_app") == "on",
            "email": form.get(f"type_{ntype}_email") == "on",
        }
        for ntype, _label in _notification_types().choices
    }
    prefs.save()
    return prefs


# ---------------------------------------------------------------------------
# Abwesenheiten
# ---------------------------------------------------------------------------


@dataclass
class AbsenceInput:
    """Formulardaten einer Abwesenheit."""

    start_date: str | None
    end_date: str | None
    reason: str = ""
    deputy_id: str | None = None
    auto_decline_meetings: bool = False
    notify_deputy: bool = False


def _parse_absence_dates(data: AbsenceInput) -> tuple[date, date]:
    if not data.start_date or not data.end_date:
        raise ServiceError("Von- und Bis-Datum sind erforderlich.")
    try:
        start = datetime.strptime(data.start_date, "%Y-%m-%d").date()
        end = datetime.strptime(data.end_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ServiceError("Ungültiges Datumsformat.") from exc
    if end < start:
        raise ServiceError("Das Enddatum muss nach dem Startdatum liegen.")
    return start, end


def _auto_decline_meetings(organization: Organization, membership: Membership, start: date, end: date) -> None:
    """Fraktionssitzungen im Abwesenheitszeitraum als entschuldigt eintragen."""
    try:
        from apps.work.faction.models import FactionAttendance, FactionMeeting

        meetings = FactionMeeting.objects.filter(
            organization=organization,
            start__date__range=(start, end),
            status__in=["draft", "planned", "invited"],
        )
        for meeting in meetings:
            FactionAttendance.objects.update_or_create(
                meeting=meeting, membership=membership, defaults={"status": "excused"}
            )
    except Exception as exc:  # noqa: BLE001 — Abwesenheit bleibt auch ohne Absagen gespeichert
        logger.error(f"Failed to auto-decline meetings: {exc}")


@transaction.atomic
def create_absence(organization: Organization, membership: Membership, data: AbsenceInput) -> MemberAbsence:
    """Abwesenheit eintragen, optional Sitzungen absagen und Stellvertretung benachrichtigen."""
    start, end = _parse_absence_dates(data)
    deputy = selectors.find_active_member(organization, data.deputy_id) if data.deputy_id else None

    absence = MemberAbsence.objects.create(
        organization=organization,
        membership=membership,
        start_date=start,
        end_date=end,
        reason=data.reason,
        deputy=deputy,
        auto_decline_meetings=data.auto_decline_meetings,
        notify_deputy=data.notify_deputy,
    )
    if data.auto_decline_meetings:
        _auto_decline_meetings(organization, membership, start, end)

    if deputy and data.notify_deputy:
        _hub().send(
            recipient=deputy,
            notification_type=_notification_types().ABSENCE_DEPUTY,
            title="Stellvertretung zugewiesen",
            message=(
                f"{display_name(membership.user)} hat Sie als Stellvertreter eingetragen "
                f"({start.strftime('%d.%m.')} – {end.strftime('%d.%m.%Y')})."
            ),
            link=f"/work/{organization.slug}/profile/absence/",
            actor=membership,
        )
    return absence


def cancel_absence(organization: Organization, membership: Membership, absence_id: Any) -> None:
    """Eigene Abwesenheit stornieren."""
    absence = selectors.get_absence_or_404(organization, membership, absence_id)
    absence.is_active = False
    absence.save()


# ---------------------------------------------------------------------------
# Rollen
# ---------------------------------------------------------------------------


@dataclass
class RoleInput:
    """Formulardaten einer Rolle."""

    name: str
    description: str = ""
    color: str = ""
    priority_raw: str = ""
    is_admin: bool = False
    require_2fa: bool = False
    permission_codes: list[str] = field(default_factory=list)


def _priority(raw: str, fallback: int) -> int:
    return min(max(int(raw or fallback), 0), 100)


def _color(raw: str, fallback: str) -> str:
    return raw if HEX_COLOR_RE.match(raw) else fallback


@transaction.atomic
def create_role(organization: Organization, data: RoleInput) -> Role:
    """Rolle anlegen (Name je Organisation eindeutig); Berechtigungen nur für Nicht-Admin-Rollen."""
    if not data.name:
        raise ServiceError("Der Name ist erforderlich.")
    if selectors.role_name_exists(organization, data.name):
        raise ServiceError(f"Eine Rolle mit dem Namen '{data.name}' existiert bereits.")
    role = Role.objects.create(
        organization=organization,
        name=data.name,
        description=data.description,
        color=_color(data.color, "#6b7280"),
        priority=_priority(data.priority_raw, 50),
        is_admin=data.is_admin,
        require_2fa=data.require_2fa,
        is_system_role=False,
    )
    if data.permission_codes and not role.is_admin:
        role.permissions.set(selectors.permissions_by_codenames(data.permission_codes))
    return role


@transaction.atomic
def update_role(organization: Organization, role: Role, data: RoleInput) -> None:
    """Rolle aktualisieren; bei Systemrollen bleiben is_admin und Priorität unverändert."""
    if not data.name:
        raise ServiceError("Der Name ist erforderlich.")
    if selectors.role_name_exists(organization, data.name, exclude_id=role.id):
        raise ServiceError(f"Eine Rolle mit dem Namen '{data.name}' existiert bereits.")
    role.name = data.name
    role.description = data.description
    role.color = _color(data.color, role.color)
    role.require_2fa = data.require_2fa
    if not role.is_system_role:
        role.is_admin = data.is_admin
        role.priority = _priority(data.priority_raw, role.priority)
    role.save()
    if role.is_admin:
        role.permissions.clear()
    else:
        role.permissions.set(selectors.permissions_by_codenames(data.permission_codes))


def delete_role(role: Role) -> str:
    """Rolle löschen — keine Systemrollen, keine zugewiesenen Rollen; liefert den Namen."""
    if role.is_system_role:
        raise ServiceError("Systemrollen können nicht gelöscht werden.")
    member_count = role.memberships.count()
    if member_count > 0:
        raise ServiceError(
            f"Die Rolle '{role.name}' ist noch {member_count} Mitglied(ern) zugewiesen. "
            "Entfernen Sie zuerst die Zuweisungen."
        )
    name = role.name
    role.delete()
    return name


def reset_role(role: Role) -> bool:
    """Standard-Rolle auf ihre Definition aus setup_roles zurücksetzen."""
    return role.reset_to_default()


def restore_default_roles(organization: Organization) -> list[Role]:
    """Fehlende Standard-Rollen anlegen (Berechtigungskatalog vorher synchronisieren)."""
    cast(Any, Permission).sync_permissions()
    return cast("list[Role]", Role.restore_missing_default_roles(organization))


# ---------------------------------------------------------------------------
# Organisationseinstellungen
# ---------------------------------------------------------------------------


def update_general_settings(
    organization: Organization,
    *,
    name: str,
    description: str,
    primary_color: str,
    logo: UploadedFile[Any] | None,
    remove_logo: bool,
) -> None:
    """Name, Beschreibung, Farbe und Logo (JPG/PNG/WebP, max. 5 MB) speichern."""
    if not name:
        raise ServiceError("Der Name darf nicht leer sein.")
    organization.name = name
    organization.description = description
    if primary_color and HEX_COLOR_RE.match(primary_color):
        organization.primary_color = primary_color
    if logo is not None:
        if logo.content_type not in IMAGE_TYPES or (logo.size or 0) > MAX_IMAGE_BYTES:
            raise ServiceError("Logo muss JPG, PNG oder WebP sein und max. 5 MB gross.")
        if organization.logo:
            organization.logo.delete(save=False)
        organization.logo = logo
    if remove_logo and organization.logo:
        organization.logo.delete(save=False)
        organization.logo = None
    _save_organization(organization)


def update_contact_settings(
    organization: Organization, *, contact_email: str, contact_phone: str, website: str, address: str
) -> None:
    """Kontaktdaten speichern (E-Mail und Website werden validiert)."""
    if contact_email:
        try:
            EmailValidator()(contact_email)
        except ValidationError as exc:
            raise ServiceError("Ungueltige E-Mail-Adresse.") from exc
    if website:
        try:
            URLValidator()(website)
        except ValidationError as exc:
            raise ServiceError("Ungueltige Website-URL.") from exc
    organization.contact_email = contact_email
    organization.contact_phone = contact_phone
    organization.website = website
    organization.address = address
    _save_organization(organization)


@transaction.atomic
def update_parties(organization: Organization, party_ids: list[str], new_party_name: str) -> None:
    """Parteizugehörigkeit setzen; optional neue Partei anlegen; primäre Parteigruppe bleibt immer verknüpft."""
    from apps.tenants.models import PartyGroup

    parties = selectors.parties_by_ids(party_ids)
    if new_party_name:
        existing = selectors.find_party_group_by_name(new_party_name)
        if existing:
            if existing not in parties:
                parties.append(existing)
        else:
            parties.append(PartyGroup.objects.create(name=new_party_name))
    if organization.party_group and organization.party_group not in parties:
        parties.append(organization.party_group)
    organization.parties.set(parties)


def save_faction_settings(organization: Organization, membership: Membership, form: Mapping[str, str]) -> None:
    """Workflow-, Einladungs-, Beschlussfähigkeits- und Titel-Einstellungen der Fraktionssitzungen speichern."""
    from apps.common.quorum import QUORUM_RULES
    from apps.work.faction.invitations import INVITATION_DISPATCH_MODES, INVITATION_MODES

    settings = organization.settings or {}
    faction_settings = settings.get("faction", {})

    for key in (
        "auto_create_approval_item",
        "link_previous_meeting",
        "protocol_revision_safe",
        "auto_lock_protocol_on_complete",
        "require_protocol_approval",
    ):
        faction_settings[key] = form.get(key) == "on"

    # Einladungslogik je Organisation (Issue #62)
    invitation_mode = form.get("invitation_mode", "")
    if invitation_mode in INVITATION_MODES:
        faction_settings["invitation_mode"] = invitation_mode
    invitation_dispatch = form.get("invitation_dispatch", "")
    if invitation_dispatch in INVITATION_DISPATCH_MODES:
        faction_settings["invitation_dispatch"] = invitation_dispatch
    try:
        lead_hours = int(form.get("invitation_lead_hours", ""))
        faction_settings["invitation_lead_hours"] = max(1, min(lead_hours, 24 * 60))
    except (TypeError, ValueError):
        pass

    # Beschlussfähigkeit (Issue #69): nur Datenfeld/Erweiterungspunkt
    quorum_rule = form.get("quorum_rule", "")
    if quorum_rule in QUORUM_RULES:
        faction_settings["quorum_rule"] = quorum_rule

    for key in ("first_agenda_title_with_previous", "first_agenda_title_no_previous", "first_agenda_description"):
        faction_settings[key] = form.get(key, "").strip()

    settings["faction"] = faction_settings
    organization.settings = settings
    # Öffentliche Protokolle (Opt-in): nur mit protocols.publish änderbar
    if membership.has_permission("protocols.publish"):
        organization.publish_protocols = form.get("publish_protocols") == "on"
    _save_organization(organization)


def faction_settings_with_defaults(organization: Organization) -> dict[str, Any]:
    """Aktuelle Fraktionssitzungs-Einstellungen, fehlende Schlüssel mit Standardwerten ergänzt."""
    from apps.work.faction.invitations import INVITATION_DEFAULTS

    settings = organization.settings or {}
    faction_settings: dict[str, Any] = dict(settings.get("faction", {}))
    defaults: dict[str, Any] = {
        "auto_create_approval_item": True,
        "link_previous_meeting": True,
        "protocol_revision_safe": True,
        "auto_lock_protocol_on_complete": True,
        "require_protocol_approval": True,
        "first_agenda_title_with_previous": "Tagesordnung festlegen und letztes Protokoll genehmigen",
        "first_agenda_title_no_previous": "Tagesordnung festlegen",
        "first_agenda_description": "",
        # Einladungslogik (Issue #62)
        **INVITATION_DEFAULTS,
        # Beschlussfähigkeit (Issue #69): Erweiterungspunkt — aktuell nur die Mehrheitsregel
        "quorum_rule": "majority",
    }
    for key, default in defaults.items():
        faction_settings.setdefault(key, default)
    return faction_settings


# -- Sitzungsreihen + Ausfallregeln (Issue #61) -------------------------------


@dataclass
class ScheduleInput:
    """Formulardaten einer Sitzungsreihe."""

    name: str
    time: str
    weekday_raw: str = "0"
    duration_raw: str = "120"
    recurrence: str = "weekly"
    default_location: str = ""
    default_video_link: str = ""


def add_schedule(organization: Organization, data: ScheduleInput) -> FactionMeetingSchedule:
    """Sitzungsreihe anlegen; Termine werden automatisch erzeugt."""
    from apps.work.faction.models import FactionMeetingSchedule

    if not data.name or not data.time:
        raise ServiceError("Name und Uhrzeit sind erforderlich.")
    try:
        weekday = int(data.weekday_raw)
        duration = max(15, int(data.duration_raw or 120))
    except ValueError as exc:
        raise ServiceError("Ungültige Eingaben.") from exc
    recurrence = data.recurrence if data.recurrence in dict(FactionMeetingSchedule.RECURRENCE_CHOICES) else "weekly"
    if weekday not in dict(FactionMeetingSchedule.WEEKDAY_CHOICES):
        weekday = 0
    return FactionMeetingSchedule.objects.create(
        organization=organization,
        name=data.name,
        recurrence=recurrence,
        weekday=weekday,
        time=data.time,
        duration_minutes=duration,
        default_location=data.default_location,
        default_video_link=data.default_video_link,
    )


def _schedule(organization: Organization, schedule_id: Any) -> FactionMeetingSchedule:
    schedule = selectors.find_schedule(organization, schedule_id)
    if schedule is None:
        raise ServiceError("Sitzungsreihe nicht gefunden.")
    return schedule


def toggle_schedule(organization: Organization, schedule_id: Any) -> FactionMeetingSchedule:
    """Sitzungsreihe pausieren/aktivieren."""
    schedule = _schedule(organization, schedule_id)
    schedule.is_active = not schedule.is_active
    schedule.save()
    return schedule


def delete_schedule(organization: Organization, schedule_id: Any) -> str:
    """Sitzungsreihe löschen (bereits erzeugte Sitzungen bleiben); liefert den Namen."""
    schedule = _schedule(organization, schedule_id)
    name = schedule.name
    schedule.delete()
    return name


def add_schedule_exception(
    organization: Organization, schedule_id: Any, *, original_date: str, end_date: str, reason: str
) -> None:
    """Ausnahmezeitraum speichern — Termine im Zeitraum entfallen ersatzlos."""
    from apps.work.faction.models import FactionMeetingException

    schedule = _schedule(organization, schedule_id)
    if not original_date:
        raise ServiceError("Bitte ein Datum angeben.")
    FactionMeetingException.objects.update_or_create(
        schedule=schedule,
        original_date=original_date,
        defaults={"end_date": end_date or None, "exception_type": "cancelled", "reason": reason},
    )


def delete_schedule_exception(organization: Organization, exception_id: Any) -> None:
    """Ausnahme entfernen (nur innerhalb der Organisation)."""
    from apps.work.faction.models import FactionMeetingException

    FactionMeetingException.objects.filter(id=exception_id, schedule__organization=organization).delete()


def add_suspension_rule(organization: Organization, schedule_id: Any, ris_organization_id: Any) -> str:
    """Ausfallregel anlegen (Gremium nur aus den OParl-Organizations der verknüpften Kommune(n)); liefert den Gremiennamen."""
    from apps.work.faction.models import FactionSuspensionRule

    schedule = _schedule(organization, schedule_id)
    ris_org = selectors.find_ris_organization(organization, ris_organization_id)
    if ris_org is None:
        raise ServiceError("Gremium nicht gefunden.")
    FactionSuspensionRule.objects.get_or_create(schedule=schedule, ris_organization=ris_org)
    return str(ris_org.name)


def delete_suspension_rule(organization: Organization, rule_id: Any) -> None:
    """Ausfallregel entfernen (nur innerhalb der Organisation)."""
    from apps.work.faction.models import FactionSuspensionRule

    FactionSuspensionRule.objects.filter(id=rule_id, schedule__organization=organization).delete()
