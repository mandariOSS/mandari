# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anträge per E-Mail an die Verwaltung einreichen (Issue #580).

Für Kommunen ohne mandari Session. Hat die Organisation eine nutzbare Einreichungsverbindung
zu mandari Session, hat diese Vorrang (``ris_submission``); sonst geht der Antrag an die in den
Organisationseinstellungen gepflegten Verwaltungskontakte:

- je Empfänger eine eigene Mail (Empfänger sehen einander nicht) über den Mailweg der
  Organisation (eigenes SMTP oder mandari-Versand, ``apps.common.org_email``),
- Antragstext als PDF (wie der Export, mit Briefkopf) und die Anhänge des Dokuments (#584),
- Antworten gehen an die einreichende Person,
- in jeder Mail ein persönlicher Link, mit dem die Verwaltung den Eingang bestätigt
  (signiert, an Empfänger gebunden, ohne Anmeldung; erst ein Klick bestätigt),
- festgehalten wird, wer wann an wen eingereicht hat (``MotionEmailSubmission``); der Status des
  Dokuments wechselt über die definierten Übergänge auf „Eingereicht“.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from django.conf import settings
from django.core.cache import cache
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import transaction
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from apps.common.uploads import MB

from .models import (
    Motion,
    MotionAdministrationEvent,
    MotionEmailRecipient,
    MotionEmailSubmission,
    StatusTransitionError,
)
from .ris_submission import SubmissionError, can_submit

if TYPE_CHECKING:
    from apps.tenants.models import AdministrationContact, Membership

logger = logging.getLogger(__name__)

#: Obergrenze für alle Anhänge einer Mail zusammen – viele Postfächer nehmen nicht mehr an
EMAIL_ATTACHMENTS_MAX_BYTES = 20 * MB
PDF_MIME = "application/pdf"

SALT = "mandari.work.einreichung.eingang"
MAX_AGE = timedelta(days=365)
RATE_LIMIT_REQUESTS = 60
RATE_LIMIT_WINDOW = 10 * 60
RATE_LIMIT_FAILURES = 20
RATE_LIMIT_FAILURES_WINDOW = 60 * 60


# =============================================================================
# Verfügbarkeit
# =============================================================================


def contacts_for(organization: Any) -> list[AdministrationContact]:
    """Gepflegte Verwaltungskontakte der Organisation (Reihenfolge wie in den Einstellungen)."""
    from apps.tenants.models import AdministrationContact

    return list(AdministrationContact.objects.filter(organization=organization))


def latest_submission(motion: Motion) -> MotionEmailSubmission | None:
    return motion.email_submissions.prefetch_related("recipients").first()


def can_submit_by_email(motion: Motion, membership: Membership) -> tuple[bool, str]:
    """Darf dieses Dokument jetzt per E-Mail eingereicht werden? (Regeln wie beim Session-Weg)."""
    if motion.email_submissions.exists():
        return False, "Dieses Dokument wurde bereits per E-Mail eingereicht."
    return can_submit(motion, membership)


# =============================================================================
# Anhänge
# =============================================================================


@dataclass
class MailAttachment:
    filename: str
    size: int
    mime_type: str
    document: Any = None  # MotionDocument oder None für das Antrags-PDF


def pdf_filename(motion: Motion) -> str:
    safe = "".join(c for c in motion.title if c.isalnum() or c in " -_").strip()[:80] or "Antrag"
    return f"{safe}.pdf"


def planned_attachments(motion: Motion) -> list[MailAttachment]:
    """Anhänge der Dokument-Mail ohne das Antrags-PDF (dessen Größe steht erst nach dem Erzeugen fest)."""
    return [
        MailAttachment(
            filename=d.filename, size=d.file_size, mime_type=d.mime_type or "application/octet-stream", document=d
        )
        for d in motion.documents.all()
    ]


def _read_attachments(motion: Motion) -> list[tuple[str, bytes, str]]:
    """Antrags-PDF und Anhänge als (Name, Inhalt, Typ) – mit Größenprüfung."""
    from .export_service import motion_export_service

    pdf = motion_export_service.export_to_pdf(motion)
    result: list[tuple[str, bytes, str]] = [(pdf_filename(motion), pdf, PDF_MIME)]
    total = len(pdf)
    for item in planned_attachments(motion):
        total += item.size
        if total > EMAIL_ATTACHMENTS_MAX_BYTES:
            raise SubmissionError(
                f"Die Anhänge sind zusammen größer als {EMAIL_ATTACHMENTS_MAX_BYTES // MB} MB – so große Mails nehmen "
                "viele Postfächer nicht an. Bitte große Anhänge entfernen oder verkleinern."
            )
        try:
            with item.document.file.open("rb") as handle:
                content = handle.read()
        except (FileNotFoundError, ValueError, OSError):
            raise SubmissionError(f"Der Anhang „{item.filename}“ ist nicht lesbar. Bitte neu hochladen.") from None
        result.append((item.filename, content, item.mime_type))
    return result


# =============================================================================
# Einreichen
# =============================================================================


def _confirm_url(recipient: MotionEmailRecipient) -> str:
    base_url = str(getattr(settings, "SITE_URL", "")).rstrip("/")
    return f"{base_url}{reverse('work:submission_confirm', kwargs={'token': make_token(recipient)})}"


def _send_to(
    motion: Motion,
    submission: MotionEmailSubmission,
    recipient: MotionEmailRecipient,
    message: str,
    attachments: list[tuple[str, bytes, str]],
) -> bool:
    from apps.common.email import render_email
    from apps.common.org_email import send_org_email

    organization = motion.organization
    context = {
        "organization": organization,
        "motion": motion,
        "submission": submission,
        "recipient": recipient,
        "message": message,
        "attachment_names": [name for name, _content, _mime in attachments],
        "confirm_url": _confirm_url(recipient),
    }
    html_body, text_body = render_email("work/organization/email/submission_email.html", context)
    reply_to = [submission.submitted_by_email] if submission.submitted_by_email else None
    try:
        return bool(
            send_org_email(
                organization,
                subject=submission.subject,
                body=text_body,
                html_body=html_body,
                to=[recipient.email],
                reply_to=reply_to,
                attachments=attachments,
                fail_silently=False,
            )
        )
    except Exception:  # noqa: BLE001 – Versandfehler je Empfänger protokollieren, Text nie weitergeben
        logger.exception("Einreichung per E-Mail an Empfänger %s fehlgeschlagen", recipient.pk)
        return False


def submit_by_email(
    motion: Motion,
    membership: Membership,
    *,
    contact_ids: list[str],
    subject: str,
    message: str,
) -> MotionEmailSubmission:
    """
    Antrag an die gewählten Verwaltungskontakte senden und festhalten.

    Scheitert der Versand an alle Empfänger, bleibt nichts gespeichert (``SubmissionError``).
    Scheitert er nur an einzelne, ist die Einreichung erfolgt; die Empfänger sind als nicht
    versendet markiert.
    """
    from .administration_feedback import SUBMISSION_VIA

    allowed, reason = can_submit_by_email(motion, membership)
    if not allowed:
        raise SubmissionError(reason)
    # Vor dem Versand prüfen, ob der Statuswechsel möglich ist – versendete Mails lassen sich nicht zurückholen
    if motion.status != "submitted" and motion.transition_path("submitted", via=SUBMISSION_VIA) is None:
        raise SubmissionError(f"Im Status „{motion.get_status_display()}“ kann nicht eingereicht werden.")
    wanted = {str(value) for value in contact_ids}
    contacts = [c for c in contacts_for(motion.organization) if str(c.pk) in wanted]
    if not contacts:
        raise SubmissionError("Bitte mindestens einen Verwaltungskontakt als Empfänger auswählen.")
    subject = (subject or "").strip()[:300] or f"Antrag: {motion.title}"[:300]

    attachments = _read_attachments(motion)
    user = membership.user

    with transaction.atomic():
        submission = MotionEmailSubmission.objects.create(
            motion=motion,
            submitted_by=membership,
            submitted_by_name=(user.get_full_name() or user.email)[:255],
            submitted_by_email=user.email or "",
            subject=subject,
            attachment_names=[name for name, _content, _mime in attachments],
        )
        recipients = [
            MotionEmailRecipient.objects.create(submission=submission, label=c.label, email=c.email) for c in contacts
        ]
        for recipient in recipients:
            recipient.delivered = _send_to(motion, submission, recipient, message.strip(), attachments)
            recipient.save(update_fields=["delivered"])
        if not any(r.delivered for r in recipients):
            raise SubmissionError(
                "Die E-Mail konnte nicht versendet werden. Bitte die E-Mail-Einstellungen der Organisation "
                "prüfen oder es später erneut versuchen."
            )

        if motion.status == "submitted":
            motion.submitted_at = timezone.now()
            motion.save(update_fields=["submitted_at", "updated_at"])
        else:
            try:
                motion.advance_to("submitted", via=SUBMISSION_VIA)
            except StatusTransitionError as exc:
                raise SubmissionError(
                    f"Im Status „{motion.get_status_display()}“ kann nicht eingereicht werden."
                ) from exc
        MotionAdministrationEvent.objects.get_or_create(
            motion=motion, key=f"email:{submission.pk}:submitted", defaults={"kind": "submitted"}
        )
    submission_id, membership_id = submission.pk, membership.pk
    transaction.on_commit(lambda: send_receipt(submission_id, membership_id), robust=True)
    return submission


def send_receipt(submission_id: Any, membership_id: Any) -> bool:
    """Kopie für die einreichende Person: an wen, wann, welche Anhänge (Versandfehler nur protokolliert)."""
    from apps.tenants.models import Membership
    from apps.work.organization.emails import absolute_url, send_organization_mail

    try:
        submission = (
            MotionEmailSubmission.objects.select_related("motion__organization")
            .prefetch_related("recipients")
            .get(pk=submission_id)
        )
        motion = submission.motion
        membership = Membership.objects.select_related("user").get(
            pk=membership_id, organization_id=motion.organization_id
        )
        if not membership.user.email:
            return False
        status_url = absolute_url(
            reverse("work:document_submit_ris", kwargs={"org_slug": motion.organization.slug, "motion_id": motion.pk})
        )
        return send_organization_mail(
            motion.organization,
            template="submission_email_receipt.html",
            subject=f"Per E-Mail eingereicht: {motion.title}",
            to=membership.user.email,
            context={
                "motion": motion,
                "submission": submission,
                "recipient": membership.user,
                "status_url": status_url,
            },
        )
    except Exception:  # noqa: BLE001 – die Einreichung ist bereits erfolgt
        logger.exception("Kopie der Einreichung %s konnte nicht versendet werden", submission_id)
        return False


# =============================================================================
# Eingangsbestätigung durch die Verwaltung
# =============================================================================

TokenState = Literal["ok", "invalid", "expired"]


@dataclass(frozen=True)
class TokenCheck:
    state: TokenState
    recipient: MotionEmailRecipient | None = None


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=SALT)


def make_token(recipient: MotionEmailRecipient) -> str:
    """Signiert: Empfänger-ID und Link-Schlüssel (liegt nur in der Datenbank)."""
    return _signer().sign(f"{recipient.pk.hex}.{recipient.confirmation_nonce}")


def check_token(token: str) -> TokenCheck:
    try:
        value = _signer().unsign(token, max_age=MAX_AGE)
    except SignatureExpired:
        return TokenCheck("expired")
    except BadSignature:
        return TokenCheck("invalid")
    recipient_hex, _, nonce = value.partition(".")
    try:
        recipient_id = uuid.UUID(hex=recipient_hex)
    except ValueError:
        return TokenCheck("invalid")
    recipient = (
        MotionEmailRecipient.objects.select_related("submission__motion__organization").filter(pk=recipient_id).first()
    )
    if recipient is None or not constant_time_compare(recipient.confirmation_nonce, nonce):
        return TokenCheck("invalid")
    return TokenCheck("ok", recipient)


def confirm_receipt(recipient: MotionEmailRecipient) -> bool:
    """Eingang bestätigen; ``True`` beim ersten Mal (benachrichtigt Autor:in und Federführung)."""
    from .administration_feedback import _notify

    with transaction.atomic():
        updated = MotionEmailRecipient.objects.filter(pk=recipient.pk, confirmed_at__isnull=True).update(
            confirmed_at=timezone.now()
        )
    if not updated:
        return False
    recipient.refresh_from_db(fields=["confirmed_at"])
    motion = recipient.submission.motion
    _notify(
        motion,
        "Eingang bestätigt",
        f"{recipient.label} hat den Eingang von „{motion.title}“ bestätigt.",
    )
    return True


def _count(key: str, window: int) -> int:
    if cache.add(key, 1, timeout=window):
        return 1
    try:
        return int(cache.incr(key))
    except ValueError:
        cache.add(key, 1, timeout=window)
        return 1


def rate_limited(request: HttpRequest) -> bool:
    from apps.accounts.two_factor_policy import client_ip

    ip = client_ip(request) or "unbekannt"
    if int(cache.get(f"work-einreichung:fehler:{ip}", 0) or 0) >= RATE_LIMIT_FAILURES:
        return True
    return _count(f"work-einreichung:aufrufe:{ip}", RATE_LIMIT_WINDOW) > RATE_LIMIT_REQUESTS


def record_failure(request: HttpRequest) -> None:
    from apps.accounts.two_factor_policy import client_ip

    ip = client_ip(request) or "unbekannt"
    _count(f"work-einreichung:fehler:{ip}", RATE_LIMIT_FAILURES_WINDOW)
