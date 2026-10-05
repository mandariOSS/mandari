# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fristen-Erinnerungen für den Sitzungsdienst (Issue #83).

Ein täglicher Lauf (Management-Command `send_session_reminders`) prüft je
Mandant fünf Fristtypen und versendet E-Mails:

- Ladungsfrist läuft ab / ist verstrichen  -> Sitzungsdienst (edit_meetings)
- Vorlagenfrist läuft ab                   -> Vorlagen-Bearbeitung (edit_papers)
- Rückmeldung zur Sitzung fehlt            -> eingeladene Person selbst (mit
  persönlichem Rückmeldelink, Issue #225; nicht bei Zustellweg Brief); Anlass,
  Empfängerkreis und Zeitpunkte je Mandant, dieselbe Regel wie der Knopf (Issue #619)
- Wiedervorlage Beschlusskontrolle (#37)   -> Sitzungsdienst (edit_meetings)

Idempotenz: Jede Erinnerung wird über SessionReminderLog mit einem
dedup_key genau einmal versendet; der Lauf kann beliebig oft wiederholt
werden. Vorlaufzeiten und An/Aus je Typ kommen aus
SessionTenant.reminder_config().
"""

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common import mail
from apps.session.services import joint_meeting_service
from apps.session.services.staff_recipients import StaffRecipients
from apps.session.visibility import agenda_item_visible, paper_visible

from ..models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionMeeting,
    SessionPaper,
    SessionReminderLog,
    SessionTenant,
)

logger = logging.getLogger(__name__)

# Vorlagen-Status, für die eine Fristerinnerung sinnvoll ist
PAPER_OPEN_STATUSES = ("draft", "review")


def _base_url(tenant: SessionTenant) -> str:
    return f"{settings.SITE_URL.rstrip('/')}/session/{tenant.slug}"


def _claim(tenant: SessionTenant, kind: str, dedup_key: str, recipients: list[str]) -> bool:
    """
    Erinnerung atomar beanspruchen. False, wenn sie bereits versendet wurde.
    """
    try:
        # Savepoint: Der Konflikt darf eine umgebende Transaktion nicht abbrechen
        with transaction.atomic():
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
    ok = mail.send(kind="session.frist", subject=subject, body=body, to=recipients, fail_silently=True)
    if not ok:
        logger.warning("Erinnerung %s (%s) konnte nicht versendet werden.", kind, dedup_key)
    return ok


def _remind_invitations(tenant, config, today, *, dry_run) -> dict:
    """Ladungsfristen: bevorstehend und verstrichen."""
    sent = {"invitation_upcoming": 0, "invitation_overdue": 0}
    if not config["invitation_enabled"]:
        return sent

    staff = StaffRecipients(tenant, "edit_meetings")
    if not staff:
        return sent

    meetings = list(
        SessionMeeting.with_joint_flag(
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
    )
    # Gemeinsame Sitzungen (Issue #317): längste Ladungsfrist der beteiligten Gremien
    joint_meeting_service.prefetch_joint(meetings)
    horizon = today + timedelta(days=config["invitation_days_before"])
    base = _base_url(tenant)

    for meeting in meetings:
        deadline = meeting.invitation_deadline
        url = f"{base}/meetings/{meeting.id}/"
        # Nichtöffentliche Sitzungen nur an Personen, die sie selbst sehen dürfen
        recipients = staff.for_meeting(meeting)
        if deadline < today:
            subject = f"[{tenant.name}] Ladungsfrist verstrichen: {meeting.name}"
            body = (
                f"Die Ladungsfrist für „{meeting.name}“ ({meeting.organizations_label}, "
                f"Sitzung am {timezone.localtime(meeting.start).strftime('%d.%m.%Y %H:%M')}) "
                f"ist am {deadline.strftime('%d.%m.%Y')} verstrichen — die Einladung wurde "
                f"noch nicht versandt.\n\nZur Sitzung: {url}\n"
            )
            if _send(tenant, "invitation_overdue", str(meeting.id), recipients, subject, body, dry_run=dry_run):
                sent["invitation_overdue"] += 1
        elif deadline <= horizon:
            subject = f"[{tenant.name}] Ladung muss bis {deadline.strftime('%d.%m.')} raus: {meeting.name}"
            body = (
                f"Für „{meeting.name}“ ({meeting.organizations_label}, Sitzung am "
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

    staff = StaffRecipients(tenant, "edit_papers")
    if not staff:
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
        # Nichtöffentliche Vorlagen nur an Personen, die sie selbst sehen dürfen
        recipients = staff.emails(lambda perms, p=paper: paper_visible(perms, p))
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


def _rsvp_key(meeting_id: Any, person_id: Any, stage: int | None = None) -> str:
    """
    Schlüssel der Rückmelde-Erinnerung: je Sitzung, Person und Zeitpunkt, unabhängig vom Weg.

    Der erste (früheste) Zeitpunkt behält den bisherigen Schlüssel ``<Sitzung>:<Person>``, damit
    vor Issue #619 Erinnerte nicht erneut angeschrieben werden; weitere Zeitpunkte hängen die Tage an.
    """
    base = f"{meeting_id}:{person_id}"
    return base if stage is None else f"{base}:{stage}"


def _remind_rsvp(tenant, config, today, *, dry_run) -> dict:
    """
    Erinnerung zur Ladung zu den eingestellten Zeitpunkten (Issues #83, #225, #619).

    Wer erinnert wird, bestimmt dieselbe Regel wie der Knopf in der Übersicht
    (:func:`apps.session.services.rsvp_reminders.targets`: Anlass, Empfängerkreis, an/aus). Erinnert
    wird nur zu Sitzungen, deren Ladung versandt ist, je Person und Zeitpunkt höchstens einmal; ist
    ein späterer Zeitpunkt schon erreicht, geht nur dessen Erinnerung raus. Frühere Läufe schlüsselten
    Anwesenheitszeilen nach deren ID; solche Einträge gelten für den ersten Zeitpunkt weiter.
    """
    from apps.session.services import rsvp_reminders

    sent = {"attendance_rsvp": 0}
    if not config["rsvp_enabled"]:
        return sent

    days = config["rsvp_days"]
    meetings = (
        SessionMeeting.objects.filter(
            tenant=tenant,
            cancelled=False,
            start__date__gte=today,
            start__date__lte=today + timedelta(days=max(days)),
            invitation_sent_at__isnull=False,
        )
        .select_related("organization", "tenant")
        .order_by("start")
    )
    for meeting in meetings:
        stage = rsvp_reminders.due_stage(meeting, days, today)
        if stage is None:
            continue
        first = stage == days[0]
        targets = rsvp_reminders.targets(meeting, config)
        legacy_keys: set[str] = set()
        if first:
            # Schlüssel früherer Läufe (ID der Anwesenheitszeile): bereits Erinnerte nicht erneut anschreiben
            attendance_ids = {
                str(pk): person_id
                for pk, person_id in SessionAttendance.objects.filter(meeting=meeting).values_list("id", "person_id")
            }
            legacy_keys = {
                str(attendance_ids[key])
                for key in SessionReminderLog.objects.filter(
                    tenant=tenant, kind="attendance_rsvp", dedup_key__in=list(attendance_ids)
                ).values_list("dedup_key", flat=True)
            }
        for target in targets:
            if str(target.person.pk) in legacy_keys:
                continue
            dedup_key = _rsvp_key(meeting.id, target.person.pk, None if first else stage)
            if dry_run:
                logger.info("[dry-run] attendance_rsvp -> %s: %s", [target.email], meeting.name)
                sent["attendance_rsvp"] += 1
                continue
            if not _claim(tenant, "attendance_rsvp", dedup_key, [target.email]):
                continue
            if rsvp_reminders.send(target, meeting):
                sent["attendance_rsvp"] += 1
            else:
                logger.warning("Erinnerung attendance_rsvp (%s) konnte nicht versendet werden.", dedup_key)
    return sent


def _remind_resolutions(tenant, config, today, *, dry_run) -> dict:
    """Wiedervorlage Beschlusskontrolle (Issue #37): Frist naht oder verstrichen."""
    sent = {"resolution_followup": 0}
    if not config["resolution_enabled"]:
        return sent

    staff = StaffRecipients(tenant, "edit_meetings")
    if not staff:
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
        # Beschlüsse aus nichtöffentlichen TOPs/Sitzungen nur an Personen mit NÖ-Sichtrecht
        recipients = staff.emails(lambda perms, i=item: agenda_item_visible(perms, i))
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
