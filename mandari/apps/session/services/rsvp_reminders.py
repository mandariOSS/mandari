# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Erinnerungen zu Ladungen – eine Regel für Knopf und täglichen Lauf (Issues #83, #225, #619).

Pflicht ist nur die Ladung; ob und wie an Rückmeldungen erinnert wird, stellt jeder Mandant unter
*Einstellungen → Fristen-Erinnerungen* ein (``SessionTenant.reminder_config()``):

- an/aus (``rsvp_enabled``) – aus heißt: kein Knopf, kein täglicher Lauf; die Ladung bleibt unberührt
- Anlass (``rsvp_reason``): fehlende Zu-/Absage (Standard), fehlende Empfangsbestätigung (ohne
  Rückmeldung) oder beides (eines von beiden fehlt)
- Empfängerkreis (``rsvp_audience``): alle Geladenen (Standard), Mitglieder der Gremien ohne Gäste,
  nur stimmberechtigte Mitglieder und ihre Vertretungen
- Zeitpunkte (``rsvp_days``, z. B. 7 und 2 Tage vorher) – nur für den täglichen Lauf; der Knopf
  erinnert sofort

Wer erinnert wird, bestimmt :func:`targets` für beide Wege gleich; :func:`send` versendet: mit
persönlichem Rückmeldelink aus dem jüngsten Versand im gemeinsamen Mail-Layout, ohne Versand an die
Person (nur Anwesenheitsliste) als Textmail mit Verweis auf den Sitzungsdienst. Personen mit
Zustellweg Brief und ohne E-Mail-Adresse erhalten keine Mail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.common.email import send_email
from apps.session.models import (
    SessionAttendance,
    SessionInvitationRecipient,
    SessionMeeting,
    SessionPerson,
    SessionTenant,
)

logger = logging.getLogger(__name__)


@dataclass
class Target:
    """Eine zu erinnernde Person mit dem, was von ihr noch fehlt."""

    person: SessionPerson
    #: jüngster zugestellter Mail- bzw. Portal-Versand (für den persönlichen Rückmeldelink)
    recipient: SessionInvitationRecipient | None
    missing_acknowledgement: bool
    missing_response: bool

    @property
    def email(self) -> str:
        return (self.recipient.email if self.recipient is not None else "") or self.person.email or ""


def enabled(tenant: SessionTenant, config: dict[str, Any] | None = None) -> bool:
    return bool((config or tenant.reminder_config())["rsvp_enabled"])


def _audience(meeting: SessionMeeting, audience: str) -> set[Any] | None:
    """Personen-IDs des Empfängerkreises; ``None`` = alle Geladenen."""
    if audience == SessionTenant.RSVP_AUDIENCE_ALL:
        return None
    from apps.session.services import joint_meeting_service

    seats = joint_meeting_service.seats(meeting)
    if audience == SessionTenant.RSVP_AUDIENCE_VOTING:
        return {seat.person.pk for seat in seats if seat.has_voting_rights}
    return {seat.person.pk for seat in seats if seat.full_agenda}


def _wanted(reason: str, missing_acknowledgement: bool, missing_response: bool) -> bool:
    if reason == SessionTenant.RSVP_REASON_ACKNOWLEDGEMENT:
        return missing_acknowledgement and missing_response
    if reason == SessionTenant.RSVP_REASON_BOTH:
        return missing_acknowledgement or missing_response
    return missing_response


def targets(meeting: SessionMeeting, config: dict[str, Any] | None = None) -> list[Target]:
    """
    Wen die Erinnerung zu dieser Sitzung erreicht – für Knopf und täglichen Lauf dieselbe Regel.

    Grundlage sind alle Ladungsempfänger (Ladung, Nachladung, Vertretungsanfrage) und die Personen der
    Anwesenheitsliste mit Status „eingeladen“. Eine Zu- oder Absage fehlt, solange die Anwesenheit
    fehlt oder „eingeladen“ ist; eine Empfangsbestätigung fehlt, solange ein zugestellter Mail- oder
    Portal-Versand unbestätigt ist.
    """
    from apps.session.services.invitation_response_service import MAIL_CHANNELS

    config = config or meeting.tenant.reminder_config()
    if not config["rsvp_enabled"]:
        return []
    rows: dict[Any, list[SessionInvitationRecipient]] = {}
    persons: dict[Any, SessionPerson] = {}
    substitutes_for: dict[Any, set[Any]] = {}
    for row in (
        SessionInvitationRecipient.objects.filter(dispatch__meeting=meeting, person__isnull=False)
        .select_related("person", "dispatch")
        .order_by("dispatch__sent_at", "created_at")
    ):
        person = row.person
        if person is None:
            continue
        persons[person.pk] = person
        rows.setdefault(person.pk, []).append(row)
        if row.substitute_for_id:
            substitutes_for.setdefault(person.pk, set()).add(row.substitute_for_id)
    attendances = {
        attendance.person_id: attendance
        for attendance in SessionAttendance.objects.filter(meeting=meeting).select_related("person")
    }
    for row_attendance in attendances.values():
        if row_attendance.status == "invited":
            persons.setdefault(row_attendance.person_id, row_attendance.person)

    circle = _audience(meeting, config["rsvp_audience"])
    result = []
    for person_id, person in persons.items():
        if not person.is_active or person.delivery_channel == "letter":
            continue
        if circle is not None and person_id not in circle and not (substitutes_for.get(person_id, set()) & circle):
            continue
        attendance = attendances.get(person_id)
        missing_response = attendance is None or attendance.status == "invited"
        mail_rows = [r for r in rows.get(person_id, []) if r.channel in MAIL_CHANNELS and r.status == "sent"]
        open_rows = [r for r in mail_rows if r.acknowledged_at is None]
        missing_acknowledgement = bool(open_rows)
        if not _wanted(config["rsvp_reason"], missing_acknowledgement, missing_response):
            continue
        recipient = open_rows[-1] if open_rows else (mail_rows[-1] if mail_rows else None)
        target = Target(person, recipient, missing_acknowledgement, missing_response)
        if target.email:
            result.append(target)
    result.sort(key=lambda t: (t.person.family_name, t.person.given_name))
    return result


def due_stage(meeting: SessionMeeting, days: list[int], today: date) -> int | None:
    """
    Fälliger Zeitpunkt des täglichen Laufs: der kleinste, den die Sitzung erreicht hat (``None``: keiner).

    Bei 7 und 2 Tagen ist ab sieben Tagen vorher „7“ fällig, ab zwei Tagen „2“. Wer den ersten
    Zeitpunkt verpasst (Ladung spät versandt), erhält nur die jeweils fällige Erinnerung.
    """
    remaining = (timezone.localtime(meeting.start).date() - today).days
    if remaining < 0:
        return None
    reached = [d for d in days if remaining <= d]
    return min(reached) if reached else None


def subject(target: Target, meeting: SessionMeeting) -> str:
    start = timezone.localtime(meeting.start).strftime("%d.%m.%Y")
    if target.missing_response:
        return f"Erinnerung – bitte Rückmeldung: {meeting.name} am {start}"
    return f"Erinnerung – bitte Empfang bestätigen: {meeting.name} am {start}"


def send(target: Target, meeting: SessionMeeting) -> bool:
    """Erinnerung an eine Person versenden; ``False`` bei Fehlschlag (wird geloggt, nie geworfen)."""
    from apps.session.services import invitation_response_service

    tenant = meeting.tenant
    try:
        if target.recipient is not None:
            target.recipient.dispatch.meeting = meeting
            invitation_response_service.send_reminder_mail(
                target.recipient,
                subject=subject(target, meeting),
                missing_acknowledgement=target.missing_acknowledgement,
                missing_response=target.missing_response,
            )
            return True
        # Ohne Versand an die Person (nur Anwesenheitsliste): Verweis auf den Sitzungsdienst
        start_local = timezone.localtime(meeting.start)
        base = f"{settings.SITE_URL.rstrip('/')}/session/{tenant.slug}"
        body = (
            f"Guten Tag {target.person.display_name},\n\n"
            f"für die Sitzung „{meeting.name}“ ({meeting.organization.name}) am "
            f"{start_local.strftime('%d.%m.%Y um %H:%M Uhr')} liegt noch keine "
            "Zu- oder Absage von Ihnen vor. Bitte melden Sie sich beim Sitzungsdienst zurück.\n\n"
            f"Zur Sitzung: {base}/meetings/{meeting.id}/\n"
        )
        return bool(
            send_email(
                subject=f"[{tenant.name}] Bitte Rückmeldung: {meeting.name} am {start_local.strftime('%d.%m.%Y')}",
                body=body,
                to=[target.email],
                fail_silently=True,
            )
        )
    except Exception:  # noqa: BLE001 — je Person; ein Fehlschlag stoppt die übrigen nicht
        logger.exception("Erinnerung zur Ladung konnte nicht versendet werden.")
        return False


def send_now(meeting: SessionMeeting) -> tuple[int, int]:
    """
    Knopf in der Übersicht: sofort an alle nach der Regel des Mandanten erinnern (ohne Zeitpunkte).

    Returns:
        (versandt, fehlgeschlagen)
    """
    from apps.session import audit

    sent = failed = 0
    for target in targets(meeting):
        if send(target, meeting):
            sent += 1
        else:
            failed += 1
    if sent or failed:
        audit.log_event(
            "update",
            meeting,
            changes={"erinnerung_ladung": {"versandt": sent, "fehlgeschlagen": failed}},
        )
    return sent, failed
