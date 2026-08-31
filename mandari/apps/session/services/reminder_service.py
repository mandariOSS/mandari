# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fristen-Erinnerungen für den Sitzungsdienst (Issue #83).

Ein täglicher Lauf (Management-Command `send_session_reminders`) prüft je
Mandant fünf Fristtypen und versendet E-Mails:

- Ladungsfrist läuft ab / ist verstrichen  -> Sitzungsdienst (edit_meetings)
- Vorlagenfrist läuft ab                   -> Vorlagen-Bearbeitung (edit_papers)
- Rückmeldung zur Sitzung fehlt            -> eingeladene Person selbst
- Wiedervorlage Beschlusskontrolle (#37)   -> Sitzungsdienst (edit_meetings)

Idempotenz: Jede Erinnerung wird über SessionReminderLog mit einem
dedup_key genau einmal versendet; der Lauf kann beliebig oft wiederholt
werden. Vorlaufzeiten und An/Aus je Typ kommen aus
SessionTenant.reminder_config().
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from apps.common.email import send_email

from ..models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionMeeting,
    SessionPaper,
    SessionReminderLog,
    SessionTenant,
    SessionUser,
)

logger = logging.getLogger(__name__)

# Vorlagen-Status, für die eine Fristerinnerung sinnvoll ist
PAPER_OPEN_STATUSES = ("draft", "review")


def _base_url(tenant: SessionTenant) -> str:
    return f"{settings.SITE_URL.rstrip('/')}/session/{tenant.slug}"


def _staff_recipients(tenant: SessionTenant, permission: str) -> list[str]:
    """E-Mail-Adressen aller aktiven Session-Benutzer mit einer Berechtigung."""
    recipients = []
    users = (
        SessionUser.objects.filter(tenant=tenant, is_active=True, user__is_active=True)
        .select_related("user")
        .prefetch_related("roles")
    )
    for su in users:
        if su.has_permission(permission) and su.user.email:
            recipients.append(su.user.email)
    return sorted(set(recipients))


def _claim(tenant: SessionTenant, kind: str, dedup_key: str, recipients: list[str]) -> bool:
    """
    Erinnerung atomar beanspruchen. False, wenn sie bereits versendet wurde.
    """
    try:
        SessionReminderLog.objects.create(tenant=tenant, kind=kind, dedup_key=dedup_key, recipients=recipients)
        return True
    except IntegrityError:
        return False


def _send(tenant, kind, dedup_key, recipients, subject, body, *, dry_run=False) -> bool:
    if not recipients:
        return False
    if dry_run:
        logger.info("[dry-run] %s -> %s: %s", kind, recipients, subject)
        return True
    if not _claim(tenant, kind, dedup_key, recipients):
        return False
    ok = send_email(subject=subject, body=body, to=recipients, fail_silently=True)
    if not ok:
        logger.warning("Erinnerung %s (%s) konnte nicht versendet werden.", kind, dedup_key)
    return ok


def _remind_invitations(tenant, config, today, *, dry_run) -> dict:
    """Ladungsfristen: bevorstehend und verstrichen."""
    sent = {"invitation_upcoming": 0, "invitation_overdue": 0}
    if not config["invitation_enabled"]:
        return sent

    recipients = _staff_recipients(tenant, "edit_meetings")
    if not recipients:
        return sent

    meetings = (
        SessionMeeting.objects.filter(
            tenant=tenant,
            start__gte=timezone.now(),
            cancelled=False,
            invitation_sent_at__isnull=True,
            meeting_state__in=["draft", "scheduled"],
        )
        .select_related("organization")
        .order_by("start")
    )
    horizon = today + timedelta(days=config["invitation_days_before"])
    base = _base_url(tenant)

    for meeting in meetings:
        deadline = meeting.invitation_deadline
        url = f"{base}/meetings/{meeting.id}/"
        if deadline < today:
            subject = f"[{tenant.name}] Ladungsfrist verstrichen: {meeting.name}"
            body = (
                f"Die Ladungsfrist für „{meeting.name}“ ({meeting.organization.name}, "
                f"Sitzung am {timezone.localtime(meeting.start).strftime('%d.%m.%Y %H:%M')}) "
                f"ist am {deadline.strftime('%d.%m.%Y')} verstrichen — die Einladung wurde "
                f"noch nicht versandt.\n\nZur Sitzung: {url}\n"
            )
            if _send(tenant, "invitation_overdue", str(meeting.id), recipients, subject, body, dry_run=dry_run):
                sent["invitation_overdue"] += 1
        elif deadline <= horizon:
            subject = f"[{tenant.name}] Ladung muss bis {deadline.strftime('%d.%m.')} raus: {meeting.name}"
            body = (
                f"Für „{meeting.name}“ ({meeting.organization.name}, Sitzung am "
                f"{timezone.localtime(meeting.start).strftime('%d.%m.%Y %H:%M')}) muss die "
                f"Einladung bis zum {deadline.strftime('%d.%m.%Y')} versandt werden.\n\n"
                f"Zur Sitzung: {url}\n"
            )
            if _send(tenant, "invitation_upcoming", str(meeting.id), recipients, subject, body, dry_run=dry_run):
                sent["invitation_upcoming"] += 1
    return sent


def _remind_papers(tenant, config, today, *, dry_run) -> dict:
    """Vorlagen mit ablaufender Frist."""
    sent = {"paper_deadline": 0}
    if not config["paper_enabled"]:
        return sent

    recipients = _staff_recipients(tenant, "edit_papers")
    if not recipients:
        return sent

    horizon = today + timedelta(days=config["paper_days_before"])
    papers = SessionPaper.objects.filter(
        tenant=tenant,
        status__in=PAPER_OPEN_STATUSES,
        deadline__isnull=False,
        deadline__lte=horizon,
    ).order_by("deadline")
    base = _base_url(tenant)

    for paper in papers:
        overdue = paper.deadline < today
        subject = (
            f"[{tenant.name}] Vorlagenfrist {'verstrichen' if overdue else 'läuft ab'}: {paper.reference or paper.name}"
        )
        body = (
            f"Die Vorlage „{paper.name}“ ({paper.reference or 'ohne Nummer'}, "
            f"Status: {paper.get_status_display()}) hat die Frist "
            f"{paper.deadline.strftime('%d.%m.%Y')}"
            f"{' bereits überschritten' if overdue else ''}.\n\n"
            f"Zur Vorlage: {base}/papers/{paper.id}/\n"
        )
        dedup = f"{paper.id}:{paper.deadline.isoformat()}"
        if _send(tenant, "paper_deadline", dedup, recipients, subject, body, dry_run=dry_run):
            sent["paper_deadline"] += 1
    return sent


def _remind_rsvp(tenant, config, today, *, dry_run) -> dict:
    """Eingeladene ohne Zu-/Absage kurz vor der Sitzung erinnern."""
    sent = {"attendance_rsvp": 0}
    if not config["rsvp_enabled"]:
        return sent

    horizon = today + timedelta(days=config["rsvp_days_before"])
    attendances = (
        SessionAttendance.objects.filter(
            meeting__tenant=tenant,
            meeting__cancelled=False,
            meeting__start__date__gte=today,
            meeting__start__date__lte=horizon,
            meeting__invitation_sent_at__isnull=False,
            status="invited",
        )
        .select_related("person", "meeting__organization")
        .order_by("meeting__start")
    )
    base = _base_url(tenant)

    for attendance in attendances:
        email = attendance.person.email
        if not email:
            continue
        meeting = attendance.meeting
        start_local = timezone.localtime(meeting.start)
        subject = f"[{tenant.name}] Bitte Rückmeldung: {meeting.name} am {start_local.strftime('%d.%m.%Y')}"
        body = (
            f"Guten Tag {attendance.person.display_name},\n\n"
            f"für die Sitzung „{meeting.name}“ ({meeting.organization.name}) am "
            f"{start_local.strftime('%d.%m.%Y um %H:%M Uhr')} liegt noch keine "
            f"Zu- oder Absage von Ihnen vor. Bitte melden Sie sich beim "
            f"Sitzungsdienst zurück.\n\nZur Sitzung: {base}/meetings/{meeting.id}/\n"
        )
        if _send(tenant, "attendance_rsvp", str(attendance.id), [email], subject, body, dry_run=dry_run):
            sent["attendance_rsvp"] += 1
    return sent


def _remind_resolutions(tenant, config, today, *, dry_run) -> dict:
    """Wiedervorlage Beschlusskontrolle (Issue #37): Frist naht oder verstrichen."""
    sent = {"resolution_followup": 0}
    if not config["resolution_enabled"]:
        return sent

    recipients = _staff_recipients(tenant, "edit_meetings")
    if not recipients:
        return sent

    horizon = today + timedelta(days=config["resolution_days_before"])
    items = (
        SessionAgendaItem.objects.filter(
            meeting__tenant=tenant,
            vote_result="approved",
            implementation_deadline__isnull=False,
            implementation_deadline__lte=horizon,
        )
        .exclude(implementation_status="done")
        .select_related("meeting__organization")
        .order_by("implementation_deadline")
    )
    base = _base_url(tenant)

    for item in items:
        overdue = item.implementation_deadline < today
        label = item.resolution_number or f"TOP {item.number}"
        subject = f"[{tenant.name}] Beschlusskontrolle: {label} {'überfällig' if overdue else 'zur Wiedervorlage'}"
        body = (
            f"Der Beschluss {label} „{item.name}“ ({item.meeting.organization.name}) "
            f"hat die Erledigungsfrist {item.implementation_deadline.strftime('%d.%m.%Y')}"
            f"{' überschritten' if overdue else ''}.\n"
            f"Umsetzungsstand: {item.get_implementation_status_display()}"
            f"{f', zuständig: {item.implementation_recipient}' if item.implementation_recipient else ''}\n\n"
            f"Zur Beschlusskontrolle: {base}/resolutions/?overdue=1\n"
        )
        # Frist im dedup_key: Wird die Frist verschoben, wird erneut erinnert.
        dedup = f"{item.id}:{item.implementation_deadline.isoformat()}"
        if _send(tenant, "resolution_followup", dedup, recipients, subject, body, dry_run=dry_run):
            sent["resolution_followup"] += 1
    return sent


def run_for_tenant(tenant: SessionTenant, *, dry_run: bool = False) -> dict:
    """Alle Erinnerungstypen für einen Mandanten prüfen und versenden."""
    today = timezone.localdate()
    config = tenant.reminder_config()
    counts: dict[str, int] = {}
    for func in (_remind_invitations, _remind_papers, _remind_rsvp, _remind_resolutions):
        counts.update(func(tenant, config, today, dry_run=dry_run))
    return counts


def run_all(*, dry_run: bool = False, tenant_slug: str | None = None) -> dict:
    """Erinnerungslauf über alle aktiven Mandanten."""
    tenants = SessionTenant.objects.filter(is_active=True)
    if tenant_slug:
        tenants = tenants.filter(slug=tenant_slug)
    totals: dict[str, int] = {}
    for tenant in tenants:
        counts = run_for_tenant(tenant, dry_run=dry_run)
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
    return totals
