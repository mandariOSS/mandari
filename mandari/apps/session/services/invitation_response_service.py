# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ladung mit Empfangsbestätigung und Rückmeldung (Issue #225).

- Zustellung der Ladungs-Mails im gemeinsamen Mail-Layout mit persönlichem Rückmeldelink
  (E-Mail mit Unterlagen; Zustellweg „Portal“ als Hinweis-Mail ohne Anhänge)
- Empfangsbestätigung je Ladungsempfänger – nur durch aktive Handlung (Link, Portal oder
  Eintrag des Sitzungsdienstes), nie durch bloßes Öffnen der Mail, kein Tracking-Pixel
- Rückmeldung Zusage / Absage mit Grund / Vertretungswunsch → SessionAttendance mit
  Zeitstempel und Herkunft
- automatische Vertretungsanfrage an die hinterlegte Stellvertretung (eigener Versand vom Typ
  „Vertretungsanfrage“ mit eigenem Rückmeldelink); ohne erreichbare Stellvertretung erfährt es
  der Sitzungsdienst per Mail
- Übersicht je Sitzung und Erinnerung an alle ohne Bestätigung
- Rückmeldung im Portal (mandari Work) für eindeutig zugeordnete Personen
  (siehe :mod:`apps.session.services.portal_link_service`)
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.common.email import render_email, send_email
from apps.session import audit
from apps.session.models import (
    SessionAttendance,
    SessionInvitationDispatch,
    SessionInvitationRecipient,
    SessionMeeting,
    SessionPerson,
    SessionTenant,
    SessionUser,
)
from apps.session.services import attendance_service, invitation_token, portal_link_service
from apps.session.services.user_invitations import sender_for

logger = logging.getLogger(__name__)
_log_event = cast(Any, audit).log_event

Decision = Literal["confirm", "decline"]
Source = Literal["link", "portal", "staff"]

MAX_REASON_LENGTH = 2000
# Höchstzahl automatischer Vertretungsanfragen je Person und Sitzung (Schutz vor Mail-Fluten)
MAX_SUBSTITUTION_ROUNDS = 3
# Versandarten, deren Empfang bestätigt werden soll (die Vertretungsanfrage zählt mit)
MAIL_CHANNELS = ("email", "portal")


# =============================================================================
# Hilfen
# =============================================================================


def meeting_location(meeting: SessionMeeting) -> str:
    """Ort der Sitzung als eine Zeile (Ort, Raum, Anschrift)."""
    parts = [meeting.location, meeting.room, meeting.street_address]
    parts.append(f"{meeting.postal_code} {meeting.locality}".strip())
    return ", ".join(part for part in parts if part)


def is_open_for_responses(meeting: SessionMeeting) -> bool:
    """Rückmeldungen sind bis Sitzungsbeginn möglich, nicht bei abgesagten Sitzungen."""
    return not meeting.cancelled and meeting.meeting_state != "cancelled" and meeting.start > timezone.now()


@dataclass(frozen=True)
class ResponseForm:
    """Eingaben eines Rückmeldeformulars (öffentlicher Link, Portal, Sitzungsdienst)."""

    action: str
    reason: str = ""
    substitute_requested: bool = False

    @property
    def decision(self) -> Decision | None:
        if self.action == "confirm":
            return "confirm"
        if self.action == "decline":
            return "decline"
        return None


def parse_response_form(data: Mapping[str, Any]) -> ResponseForm:
    """Formulardaten lesen: action (acknowledge/confirm/decline), reason, substitute."""
    return ResponseForm(
        action=str(data.get("action", "") or ""),
        reason=str(data.get("reason", "") or "")[:MAX_REASON_LENGTH],
        substitute_requested=bool(data.get("substitute")),
    )


def effective_channel(person: SessionPerson, portal_ids: set[Any]) -> str:
    """Zustellweg der Person; „Portal“ ohne Portalzugang fällt auf E-Mail zurück."""
    channel = person.delivery_channel or "email"
    if channel == "portal" and person.pk not in portal_ids:
        return "email"
    return channel


def _site_url() -> str:
    return str(getattr(settings, "SITE_URL", "https://mandari.de")).rstrip("/")


def _staff_emails(tenant: SessionTenant) -> list[str]:
    """Adressen aller aktiven Sitzungsdienst-Nutzer (Berechtigung edit_meetings)."""
    users = (
        SessionUser.objects.filter(tenant=tenant, is_active=True, user__is_active=True)
        .select_related("user")
        .prefetch_related("roles")
    )
    return sorted({su.user.email for su in users if su.user.email and su.has_permission("edit_meetings")})


def _send(
    template: str,
    context: dict[str, Any],
    *,
    subject: str,
    to: str,
    tenant: SessionTenant,
    attachments: list[tuple[str, Any, str]] | None = None,
) -> None:
    """Mail im gemeinsamen Layout versenden; Fehler werden an den Aufrufer weitergereicht."""
    html, text = render_email(template, context)
    send_email(
        subject=subject,
        body=text,
        html_body=html,
        to=[to],
        from_email=sender_for(tenant.name),
        attachments=attachments,
        fail_silently=False,
    )


def _mail_context(recipient: SessionInvitationRecipient) -> dict[str, Any]:
    meeting = recipient.dispatch.meeting
    return {
        "tenant": meeting.tenant,
        "meeting": meeting,
        "recipient": recipient,
        "start_local": timezone.localtime(meeting.start),
        "location": meeting_location(meeting),
        "response_url": invitation_token.response_url(recipient),
        "portal_url": f"{_site_url()}/work/",
        "is_portal": recipient.channel == "portal",
    }


# =============================================================================
# Zustellung
# =============================================================================


def send_invitation_mail(
    recipient: SessionInvitationRecipient,
    *,
    subject: str,
    message: str,
    supplementary: bool,
    attachments: list[tuple[str, Any, str]] | None,
) -> None:
    """Ladung bzw. Nachladung an einen Empfänger (E-Mail oder Portal-Hinweis) versenden."""
    context = _mail_context(recipient)
    context.update({"message": message, "supplementary": supplementary, "has_attachments": bool(attachments)})
    _send(
        "emails/session/invitation.html",
        context,
        subject=subject,
        to=recipient.email,
        tenant=recipient.dispatch.meeting.tenant,
        attachments=attachments if recipient.channel == "email" else None,
    )


# =============================================================================
# Empfangsbestätigung und Rückmeldung
# =============================================================================


@dataclass
class SubstituteOutcome:
    """Ergebnis der Vertretungsanfrage nach einer Absage mit Vertretungswunsch."""

    notified: list[str] = field(default_factory=list)
    by_letter: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    staff_informed: bool = False
    already_done: bool = False
    limit_reached: bool = False


@dataclass
class ResponseResult:
    attendance: SessionAttendance
    substitutes: SubstituteOutcome | None = None


def acknowledge(recipient: SessionInvitationRecipient, *, via: Source) -> bool:
    """Empfang bestätigen (einmalig; weitere Bestätigungen ändern den Zeitstempel nicht)."""
    if recipient.acknowledged_at is not None:
        return False
    recipient.acknowledged_at = timezone.now()
    recipient.acknowledged_via = via
    recipient.save(update_fields=["acknowledged_at", "acknowledged_via"])
    return True


def record_response(
    meeting: SessionMeeting,
    person: SessionPerson,
    *,
    decision: Decision,
    source: Source,
    reason: str = "",
    substitute_requested: bool = False,
    recipient: SessionInvitationRecipient | None = None,
) -> ResponseResult:
    """
    Zu- oder Absage einer Person speichern und Folgeschritte auslösen.

    - schreibt Status, Zeitstempel, Herkunft, Vertretungswunsch und (verschlüsselt) den Grund
      in die Anwesenheitszeile; fehlt sie, wird sie aus der Besetzung angelegt
    - bestätigt dabei den Empfang des Ladungsempfängers, über den die Rückmeldung kam
    - Absage mit Vertretungswunsch: Stellvertretung benachrichtigen (einmal je Absage)
    - Zusage nach bereits benachrichtigter Stellvertretung: Stellvertretung entlasten
    """
    if decision not in ("confirm", "decline"):
        raise ValueError(f"Unbekannte Rückmeldung: {decision}")
    wants_substitute = decision == "decline" and substitute_requested
    with transaction.atomic():
        attendance = attendance_service.ensure_attendance(meeting, person)
        withdraw = bool(attendance.substitutes_notified_at) and not wants_substitute
        attendance.meeting = meeting  # Mandant für die Verschlüsselung ohne Nachladen
        attendance.status = "confirmed" if decision == "confirm" else "declined"
        attendance.responded_at = timezone.now()
        attendance.response_source = source
        attendance.substitute_requested = wants_substitute
        cast(Any, attendance).set_response_reason_encrypted(
            reason.strip()[:MAX_REASON_LENGTH] if decision == "decline" else ""
        )
        attendance.save()
        if recipient is not None:
            acknowledge(recipient, via=source)

    result = ResponseResult(attendance)
    if withdraw:
        _withdraw_substitution(attendance)
    if wants_substitute:
        result.substitutes = notify_substitutes(attendance)
    return result


def substitute_memberships(meeting: SessionMeeting, person: SessionPerson) -> list[Any]:
    """Aktive Stellvertretungen einer Person im Gremium der Sitzung."""
    return [
        membership
        for membership in attendance_service.active_memberships(meeting).filter(substitute_for=person)
        if membership.person_id != person.pk
    ]


def notify_substitutes(attendance: SessionAttendance) -> SubstituteOutcome:
    """
    Hinterlegte Stellvertretung nach einer Absage mit Vertretungswunsch benachrichtigen.

    Legt einen eigenen Versand „Vertretungsanfrage“ an: je Stellvertretung ein Empfänger mit
    eigenem Rückmeldelink (Zustellweg der Person; Brief → Serienbrief). Stellvertretungen, die
    selbst schon abgesagt haben, werden übersprungen. Ohne per Mail erreichbare Stellvertretung
    informiert eine Mail den Sitzungsdienst. Idempotent über ``substitutes_notified_at``.

    Missbrauchsschutz: Wer ständig zwischen Zu- und Absage wechselt, löst höchstens
    ``MAX_SUBSTITUTION_ROUNDS`` Vertretungsanfragen je Sitzung aus und den Hinweis an den
    Sitzungsdienst höchstens einmal je Stunde.
    """
    if attendance.substitutes_notified_at is not None:
        return SubstituteOutcome(already_done=True)

    meeting = attendance.meeting
    absent = attendance.person
    rounds = (
        SessionInvitationDispatch.objects.filter(
            meeting=meeting, dispatch_type="substitution", recipients__substitute_for=absent
        )
        .distinct()
        .count()
    )
    if rounds >= MAX_SUBSTITUTION_ROUNDS:
        return SubstituteOutcome(limit_reached=True)
    memberships = substitute_memberships(meeting, absent)
    declined = set(
        SessionAttendance.objects.filter(
            meeting=meeting, person_id__in=[m.person_id for m in memberships], status="declined"
        ).values_list("person_id", flat=True)
    )
    candidates = [m for m in memberships if m.person_id not in declined]

    outcome = SubstituteOutcome()
    if candidates:
        outcome = _send_substitution(meeting, absent, candidates)
    if not outcome.notified:
        outcome.staff_informed = _notify_staff_without_substitute(attendance, outcome)

    attendance.substitutes_notified_at = timezone.now()
    attendance.save(update_fields=["substitutes_notified_at", "updated_at"])
    return outcome


def _send_substitution(meeting: SessionMeeting, absent: SessionPerson, memberships: list[Any]) -> SubstituteOutcome:
    from apps.session.services import invitation_service

    outcome = SubstituteOutcome()
    date_str = timezone.localtime(meeting.start).strftime("%d.%m.%Y")
    dispatch = SessionInvitationDispatch.objects.create(
        meeting=meeting,
        dispatch_type="substitution",
        subject=f"Vertretungsanfrage: {meeting.name} am {date_str}",
        message=f"Vertretung für {absent.display_name}",
        sent_by=None,
    )
    needs_portal = any(m.person.delivery_channel == "portal" for m in memberships)
    portal_ids = portal_link_service.portal_person_ids(meeting.tenant) if needs_portal else set()
    pdfs: dict[bool, bytes] = {}

    for membership in memberships:
        person = membership.person
        channel = effective_channel(person, portal_ids)
        if channel != "letter" and not person.email:
            outcome.failed.append(person.display_name)
            continue
        include_np = membership.role != "guest"
        recipient = SessionInvitationRecipient.objects.create(
            dispatch=dispatch,
            person=person,
            name=person.display_name,
            email=person.email,
            membership_role=membership.get_role_display(),
            includes_non_public=include_np,
            channel=channel,
            status="letter_pending" if channel == "letter" else "failed",
            error="" if channel == "letter" else "Versand nicht abgeschlossen",
            substitute_for=absent,
        )
        if channel == "letter":
            outcome.by_letter.append(person.display_name)
            continue
        attachments: list[tuple[str, Any, str]] | None = None
        if channel == "email":
            if include_np not in pdfs:
                pdfs[include_np] = invitation_service.build_agenda_pdf(meeting, include_non_public=include_np)
            attachments = [("einladung-tagesordnung.pdf", pdfs[include_np], "application/pdf")]
        try:
            context = _mail_context(recipient)
            context.update({"absent": absent, "has_attachments": bool(attachments)})
            _send(
                "emails/session/substitute_request.html",
                context,
                subject=dispatch.subject,
                to=recipient.email,
                tenant=meeting.tenant,
                attachments=attachments,
            )
            recipient.status, recipient.error, recipient.sent_at = "sent", "", timezone.now()
            outcome.notified.append(person.display_name)
        except Exception as exc:  # noqa: BLE001 — Zustellstatus je Empfänger dokumentieren, Rückmeldung bleibt gültig
            logger.exception("Vertretungsanfrage konnte nicht versendet werden.")
            recipient.error = str(exc)[:1000]
            outcome.failed.append(person.display_name)
        recipient.save(update_fields=["status", "error", "sent_at"])

    _log_event(
        "invitation_sent",
        meeting,
        user=None,
        changes={
            "versandart": dispatch.get_dispatch_type_display(),
            "vertretung_fuer": absent.display_name,
            "empfaenger_versandt": len(outcome.notified),
            "empfaenger_brief": len(outcome.by_letter),
            "empfaenger_fehlgeschlagen": len(outcome.failed),
        },
    )
    return outcome


def _notify_staff_without_substitute(attendance: SessionAttendance, outcome: SubstituteOutcome) -> bool:
    """Sitzungsdienst informieren, wenn keine Stellvertretung per Mail erreicht wurde."""
    meeting = attendance.meeting
    recipients = _staff_emails(meeting.tenant)
    if not recipients or not cache.add(f"session-ladung:vertretung-gesucht:{attendance.pk}", 1, timeout=60 * 60):
        return False
    context = {
        "tenant": meeting.tenant,
        "meeting": meeting,
        "absent": attendance.person,
        "start_local": timezone.localtime(meeting.start),
        "outcome": outcome,
        "overview_url": _site_url()
        + reverse(
            "session:meeting_invitation_status",
            kwargs={"tenant_slug": meeting.tenant.slug, "meeting_id": meeting.id},
        ),
    }
    subject = f"Vertretung gesucht: {meeting.name} am {timezone.localtime(meeting.start).strftime('%d.%m.%Y')}"
    sent = False
    for address in recipients:
        try:
            _send("emails/session/substitute_missing.html", context, subject=subject, to=address, tenant=meeting.tenant)
            sent = True
        except Exception:  # noqa: BLE001 — Hinweis an den Sitzungsdienst darf die Rückmeldung nicht verhindern
            logger.exception("Hinweis an den Sitzungsdienst (Vertretung gesucht) konnte nicht versendet werden.")
    return sent


def _withdraw_substitution(attendance: SessionAttendance) -> int:
    """Benachrichtigte Stellvertretungen entlasten, wenn die Person doch teilnimmt (bzw. keine Vertretung mehr braucht)."""
    meeting = attendance.meeting
    rows = list(
        SessionInvitationRecipient.objects.filter(
            dispatch__meeting=meeting,
            dispatch__dispatch_type="substitution",
            substitute_for=attendance.person,
            status="sent",
        ).select_related("dispatch__meeting__tenant", "dispatch__meeting__organization", "person")
    )
    informed = 0
    subject = f"Vertretung nicht mehr erforderlich: {meeting.name}"
    for recipient in rows:
        if not recipient.email:
            continue
        try:
            context = _mail_context(recipient)
            context["absent"] = attendance.person
            _send(
                "emails/session/substitute_withdrawn.html",
                context,
                subject=subject,
                to=recipient.email,
                tenant=meeting.tenant,
            )
            informed += 1
        except Exception:  # noqa: BLE001 — Entwarnung ist ein Zusatzhinweis, die Rückmeldung bleibt gespeichert
            logger.exception("Entwarnung an die Stellvertretung konnte nicht versendet werden.")
    SessionAttendance.objects.filter(pk=attendance.pk).update(substitutes_notified_at=None)
    attendance.substitutes_notified_at = None
    return informed


def substitution_requests(attendance: SessionAttendance | None) -> list[SessionInvitationRecipient]:
    """Vertretungsanfragen, die nach der Absage dieser Person verschickt wurden (für die Rückmeldeseite)."""
    if attendance is None or not attendance.substitute_requested or attendance.substitutes_notified_at is None:
        return []
    return list(
        SessionInvitationRecipient.objects.filter(
            dispatch__meeting_id=attendance.meeting_id,
            dispatch__dispatch_type="substitution",
            substitute_for_id=attendance.person_id,
        ).order_by("name")
    )


def response_reason(attendance: SessionAttendance | None) -> str:
    """Entschlüsselter Grund der Absage (nur für den Sitzungsdienst bzw. die Person selbst)."""
    if attendance is None or not attendance.response_reason_encrypted:
        return ""
    return str(cast(Any, attendance).get_response_reason_decrypted() or "")


# =============================================================================
# Übersicht und Erinnerung für den Sitzungsdienst
# =============================================================================


@dataclass
class PersonStatus:
    """Ladungsstatus einer Person in einer Sitzung (alle Versände, Rückmeldung)."""

    person: SessionPerson | None
    name: str
    rows: list[SessionInvitationRecipient]
    attendance: SessionAttendance | None = None
    reason: str = ""

    @property
    def latest(self) -> SessionInvitationRecipient:
        return self.rows[-1]

    @property
    def role(self) -> str:
        return self.latest.membership_role

    @property
    def mail_rows(self) -> list[SessionInvitationRecipient]:
        return [r for r in self.rows if r.channel in MAIL_CHANNELS and r.status == "sent"]

    @property
    def acknowledgement(self) -> str:
        """„confirmed“ (alle Versände bestätigt), „partial“, „open“ oder „none“ (nichts zu bestätigen)."""
        relevant = [r for r in self.rows if r.status in ("sent", "letter_sent")]
        if not relevant:
            return "none"
        done = [r for r in relevant if r.acknowledged_at]
        if len(done) == len(relevant):
            return "confirmed"
        return "partial" if done else "open"

    @property
    def responded(self) -> bool:
        return self.attendance is not None and self.attendance.status in ("confirmed", "declined")

    @property
    def response_label(self) -> str:
        attendance = self.attendance
        if attendance is None or attendance.status == "invited":
            return "offen"
        if attendance.status == "declined" and attendance.substitute_requested:
            return "Abgesagt, Vertretung erbeten"
        return str(attendance.get_status_display())

    @property
    def substitution_for(self) -> SessionPerson | None:
        for row in reversed(self.rows):
            if row.substitute_for is not None:
                return row.substitute_for
        return None


@dataclass
class MeetingOverview:
    rows: list[PersonStatus]

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def acknowledged(self) -> int:
        return sum(1 for r in self.rows if r.acknowledgement == "confirmed")

    @property
    def confirmed(self) -> int:
        return sum(1 for r in self.rows if r.attendance is not None and r.attendance.status == "confirmed")

    @property
    def declined(self) -> int:
        return sum(1 for r in self.rows if r.attendance is not None and r.attendance.status == "declined")

    @property
    def open_responses(self) -> int:
        return sum(1 for r in self.rows if not r.responded)

    @property
    def letters_pending(self) -> int:
        return sum(1 for r in self.rows for row in r.rows if row.status == "letter_pending")


def meeting_overview(meeting: SessionMeeting, *, include_reasons: bool) -> MeetingOverview:
    """Status je Empfänger: Versände, Empfangsbestätigung und Rückmeldung (zwei Abfragen)."""
    recipients = list(
        SessionInvitationRecipient.objects.filter(dispatch__meeting=meeting)
        .select_related("dispatch", "person", "substitute_for")
        .order_by("dispatch__sent_at", "name")
    )
    attendances = {
        a.person_id: a
        for a in SessionAttendance.objects.filter(meeting=meeting).select_related("meeting__tenant", "person")
    }
    grouped: dict[Any, PersonStatus] = {}
    for row in recipients:
        key = row.person_id or f"ohne-person:{row.pk}"
        status = grouped.get(key)
        if status is None:
            status = PersonStatus(person=row.person, name=row.name, rows=[])
            grouped[key] = status
            if row.person_id:
                status.attendance = attendances.get(row.person_id)
        status.rows.append(row)
    rows = sorted(
        grouped.values(), key=lambda s: (s.person.family_name, s.person.given_name) if s.person else ("", s.name)
    )
    if include_reasons:
        for status in rows:
            status.reason = response_reason(status.attendance)
    return MeetingOverview(rows)


def reminder_candidates(
    meeting: SessionMeeting, overview: MeetingOverview | None = None
) -> list[SessionInvitationRecipient]:
    """Je Person ohne Empfangsbestätigung und ohne Rückmeldung der jüngste offene Mail-Versand."""
    candidates = []
    overview = overview or meeting_overview(meeting, include_reasons=False)
    for status in overview.rows:
        if status.responded or status.person is None or not status.person.is_active:
            continue
        open_rows = [r for r in status.mail_rows if r.acknowledged_at is None and r.email]
        if open_rows:
            candidates.append(open_rows[-1])
    return candidates


def send_acknowledgement_reminders(meeting: SessionMeeting) -> tuple[int, int]:
    """
    Erinnerung an alle ohne Empfangsbestätigung versenden (Knopf in der Übersicht).

    Returns:
        (versandt, fehlgeschlagen)
    """
    sent = failed = 0
    for recipient in reminder_candidates(meeting):
        recipient.dispatch.meeting = meeting
        try:
            _send(
                "emails/session/invitation_reminder.html",
                _mail_context(recipient),
                subject=f"Erinnerung: {recipient.dispatch.subject}",
                to=recipient.email,
                tenant=meeting.tenant,
            )
        except Exception:  # noqa: BLE001 — Erinnerung je Empfänger, ein Fehlschlag stoppt die übrigen nicht
            logger.exception("Erinnerung zur Ladung konnte nicht versendet werden.")
            failed += 1
            continue
        recipient.reminder_count += 1
        recipient.last_reminded_at = timezone.now()
        recipient.save(update_fields=["reminder_count", "last_reminded_at"])
        sent += 1
    if sent or failed:
        _log_event(
            "update",
            meeting,
            changes={"erinnerung_ladung": {"versandt": sent, "fehlgeschlagen": failed}},
        )
    return sent, failed


def mark_letters_sent(dispatch: SessionInvitationDispatch) -> int:
    """Briefe eines Versands als zur Post gegeben vermerken (Zeitpunkt = jetzt)."""
    count = dispatch.recipients.filter(status="letter_pending").update(status="letter_sent", sent_at=timezone.now())
    if count:
        _log_event(
            "update",
            dispatch.meeting,
            changes={"briefversand": {"versandart": dispatch.get_dispatch_type_display(), "briefe": count}},
        )
    return int(count)


def latest_recipient(meeting: SessionMeeting, person: SessionPerson) -> SessionInvitationRecipient | None:
    """Jüngster zustellbarer Versand an eine Person (für Links in Erinnerungen)."""
    return (
        SessionInvitationRecipient.objects.filter(dispatch__meeting=meeting, person=person, status="sent")
        .select_related("dispatch__meeting")
        .order_by("-dispatch__sent_at")
        .first()
    )


# =============================================================================
# Rückmeldung im Portal (mandari Work)
# =============================================================================


@dataclass
class PortalInvitation:
    """Eine Sitzung mit allen Versänden an die Person und ihrer Rückmeldung."""

    meeting: SessionMeeting
    rows: list[SessionInvitationRecipient]
    attendance: SessionAttendance | None = None

    @property
    def latest(self) -> SessionInvitationRecipient:
        return self.rows[-1]

    @property
    def open_rows(self) -> list[SessionInvitationRecipient]:
        return [r for r in self.rows if r.acknowledged_at is None and r.status in ("sent", "letter_sent")]

    @property
    def can_respond(self) -> bool:
        meeting = self.meeting
        return not meeting.cancelled and meeting.meeting_state != "cancelled" and meeting.start > timezone.now()

    @property
    def substitute_for(self) -> SessionPerson | None:
        for row in reversed(self.rows):
            if row.substitute_for is not None:
                return row.substitute_for
        return None


def portal_invitations(person: SessionPerson) -> list[PortalInvitation]:
    """Anstehende Ladungen einer Person (Sitzungen ab heute), älteste zuerst."""
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = list(
        SessionInvitationRecipient.objects.filter(
            person=person, dispatch__meeting__tenant_id=person.tenant_id, dispatch__meeting__start__gte=today_start
        )
        .select_related("dispatch__meeting__organization", "dispatch__meeting__tenant", "substitute_for")
        .order_by("dispatch__meeting__start", "dispatch__sent_at")
    )
    grouped: dict[Any, PortalInvitation] = {}
    for row in rows:
        meeting = row.dispatch.meeting
        entry = grouped.get(meeting.pk)
        if entry is None:
            entry = PortalInvitation(meeting=meeting, rows=[])
            grouped[meeting.pk] = entry
        entry.rows.append(row)
    attendances = SessionAttendance.objects.filter(person=person, meeting_id__in=list(grouped))
    for attendance in attendances:
        grouped[attendance.meeting_id].attendance = attendance
    return list(grouped.values())


def respond_via_portal(
    person: SessionPerson, recipient: SessionInvitationRecipient, form: ResponseForm
) -> ResponseResult | None:
    """
    Rückmeldung aus mandari Work: bestätigt den Empfang aller Versände der Sitzung an die Person.

    Raises:
        ValueError: unbekannte Aktion oder Sitzung nicht mehr offen
    """
    meeting = recipient.dispatch.meeting
    if not is_open_for_responses(meeting):
        raise ValueError("Die Sitzung hat bereits begonnen oder ist abgesagt.")
    if form.action == "acknowledge":
        acknowledge_meeting(person, meeting, via="portal")
        return None
    decision = form.decision
    if decision is None:
        raise ValueError("Unbekannte Aktion.")
    result = record_response(
        meeting,
        person,
        decision=decision,
        source="portal",
        reason=form.reason,
        substitute_requested=form.substitute_requested,
        recipient=recipient,
    )
    acknowledge_meeting(person, meeting, via="portal")
    return result


def portal_recipient(person: SessionPerson, recipient_id: Any) -> SessionInvitationRecipient | None:
    """Ladungsempfänger der Person (fremde Empfänger bleiben unsichtbar)."""
    return (
        SessionInvitationRecipient.objects.filter(
            pk=recipient_id, person=person, dispatch__meeting__tenant_id=person.tenant_id
        )
        .select_related("dispatch__meeting__organization", "dispatch__meeting__tenant", "person")
        .first()
    )


def acknowledge_meeting(person: SessionPerson, meeting: SessionMeeting, *, via: Source) -> int:
    """Alle offenen Versände einer Sitzung an die Person bestätigen (Portal: „Erhalt bestätigen“)."""
    return int(
        SessionInvitationRecipient.objects.filter(
            dispatch__meeting=meeting, person=person, acknowledged_at__isnull=True
        )
        .exclude(status__in=("failed", "letter_pending"))
        .update(acknowledged_at=timezone.now(), acknowledged_via=via)
    )
