# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungserzeugung aus der Sitzungsreihe + modulare Ausfallregeln (Issue #61).

Erzeugt aus aktiven FactionMeetingSchedule-Reihen rollierend Sitzungen
innerhalb eines Horizonts (FACTION_SCHEDULE_HORIZON_DAYS). Die Erzeugung
ist idempotent: je Reihe und Solltermin entsteht höchstens eine Sitzung
(UniqueConstraint auf schedule + scheduled_date).

Ausfallregeln (modular, je Regel eine Prüf-Funktion):
1. Manuelle Ausnahmen/Urlaubszeiträume (FactionMeetingException, optional
   mit Enddatum): Termine im Zeitraum entfallen ersatzlos.
2. RIS-Regeln (FactionSuspensionRule): "Nach einer Sitzung von Gremium X
   fällt die nächste Fraktionssitzung aus" — z.B. nur Ratssitzungen. Die
   Gremien stammen aus den OParl-Organizations der verknüpften Kommune(n).

Ausgefallene Termine werden ersatzlos gestrichen: Sie werden als Sitzung
mit Status "cancelled" und Ausfallgrund angelegt (als "entfällt" sichtbar),
es wird nicht verschoben.

Läuft periodisch über den Sync-Watchdog (insight_sync/daemon.py) — analog
zum Erinnerungslauf (Issue #59) und Auto-Georef-Lauf.
"""

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

_SCHEDULE_LOCK_KEY = "faction:schedule:lock"
_SCHEDULE_LOCK_TIMEOUT = 10 * 60

DEFAULT_HORIZON_DAYS = 90


def _horizon_days() -> int:
    return max(1, int(getattr(settings, "FACTION_SCHEDULE_HORIZON_DAYS", DEFAULT_HORIZON_DAYS)))


# =============================================================================
# Terminberechnung je Wiederholungsregel
# =============================================================================


def _align_to_weekday(date, weekday):
    """Erstes Datum >= date mit dem gewünschten Wochentag."""
    return date + timedelta(days=(weekday - date.weekday()) % 7)


def _biweekly_anchor(schedule):
    """Ankertermin für 2-Wochen-Reihen (Parität ab Anlage der Reihe)."""
    created = timezone.localtime(schedule.created_at).date() if schedule.created_at else timezone.localdate()
    return _align_to_weekday(created, schedule.weekday)


def _nth_weekday_of_month(year, month, weekday, last=False):
    """Erster bzw. letzter Wochentag eines Monats."""
    import calendar

    if last:
        last_day = calendar.monthrange(year, month)[1]
        date = datetime(year, month, last_day).date()
        return date - timedelta(days=(date.weekday() - weekday) % 7)
    date = datetime(year, month, 1).date()
    return _align_to_weekday(date, weekday)


def occurrence_dates(schedule, from_date, to_date):
    """Solltermine der Reihe im Zeitraum [from_date, to_date] (aufsteigend)."""
    recurrence = schedule.recurrence
    dates = []

    if recurrence in ("weekly", "biweekly"):
        step = 7 if recurrence == "weekly" else 14
        current = _align_to_weekday(from_date, schedule.weekday)
        if recurrence == "biweekly":
            anchor = _biweekly_anchor(schedule)
            offset_days = (current - anchor).days % 14
            if offset_days:
                current += timedelta(days=14 - offset_days)
        while current <= to_date:
            if current >= from_date:
                dates.append(current)
            current += timedelta(days=step)
        return dates

    # Monatliche Reihen: "monthly" wird deterministisch wie "monthly_first"
    # behandelt (erster passender Wochentag im Monat)
    last = recurrence == "monthly_last"
    year, month = from_date.year, from_date.month
    while True:
        date = _nth_weekday_of_month(year, month, schedule.weekday, last=last)
        if date > to_date:
            break
        if date >= from_date:
            dates.append(date)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return dates


def previous_occurrence(schedule, date):
    """Vorheriger Solltermin der Reihe vor dem gegebenen Termin."""
    recurrence = schedule.recurrence
    if recurrence == "weekly":
        return date - timedelta(days=7)
    if recurrence == "biweekly":
        return date - timedelta(days=14)
    last = recurrence == "monthly_last"
    year, month = date.year, date.month
    month -= 1
    if month < 1:
        month = 12
        year -= 1
    return _nth_weekday_of_month(year, month, schedule.weekday, last=last)


def _occurrence_start(schedule, date):
    """Aware-Beginn-Zeitpunkt eines Solltermins."""
    start = datetime.combine(date, schedule.time)
    if timezone.is_naive(start):
        start = timezone.make_aware(start)
    return start


# =============================================================================
# Ausfallregeln (modular)
# =============================================================================


def check_manual_exceptions(schedule, date, exceptions=None) -> str | None:
    """
    Regel 1: Manuelle Ausnahmen/Urlaubszeiträume.

    Returns:
        Ausfallgrund oder None, wenn der Termin stattfindet.
    """
    if exceptions is None:
        exceptions = list(schedule.exceptions.all())
    for exception in exceptions:
        if exception.exception_type == "special":
            # Sondertermine sagen nichts ab
            continue
        if exception.covers(date):
            reason = exception.reason or "Manuelle Ausnahme"
            return f"Entfällt: {reason}"
    return None


def check_ris_rules(schedule, date, rules=None) -> str | None:
    """
    Regel 2: RIS-Regeln — nach einer Sitzung von Gremium X fällt die
    nächste Fraktionssitzung aus.

    Prüft, ob zwischen dem vorherigen Solltermin (exklusiv) und diesem
    Solltermin (inklusive) eine nicht abgesagte Sitzung eines der
    konfigurierten Gremien stattfindet.

    Returns:
        Ausfallgrund oder None, wenn der Termin stattfindet.
    """
    if rules is None:
        rules = list(schedule.suspension_rules.filter(is_active=True).select_related("ris_organization"))
    if not rules:
        return None

    from insight_core.models import OParlMeeting

    window_start = _occurrence_start(schedule, previous_occurrence(schedule, date))
    window_end = _occurrence_start(schedule, date)

    ris_meeting = (
        OParlMeeting.objects.filter(
            organizations__in=[rule.ris_organization_id for rule in rules],
            start__gt=window_start,
            start__lte=window_end,
            cancelled=False,
        )
        .order_by("start")
        .first()
    )
    if ris_meeting is None:
        return None

    org_names = {rule.ris_organization_id: rule.ris_organization.name for rule in rules}
    matched = ris_meeting.organizations.filter(id__in=org_names.keys()).first()
    name = org_names.get(matched.id) if matched else "RIS-Gremium"
    when = timezone.localtime(ris_meeting.start).strftime("%d.%m.%Y")
    return f"Entfällt nach Sitzung von {name} am {when}"


# Reihenfolge der modularen Ausfallregeln
CANCELLATION_RULES = [check_manual_exceptions, check_ris_rules]


def evaluate_cancellation(schedule, date, *, exceptions=None, rules=None) -> str | None:
    """Alle Ausfallregeln prüfen — erster Treffer gewinnt."""
    reason = check_manual_exceptions(schedule, date, exceptions=exceptions)
    if reason:
        return reason
    return check_ris_rules(schedule, date, rules=rules)


# =============================================================================
# Erzeugung
# =============================================================================


def generate_meetings_for_schedule(schedule, now=None) -> dict:
    """
    Sitzungen einer Reihe im rollierenden Horizont erzeugen (idempotent).

    Returns:
        Statistik-Dict: created, cancelled, skipped
    """
    from apps.work.faction.models import FactionMeeting

    now = now or timezone.now()
    today = timezone.localtime(now).date()
    horizon_end = today + timedelta(days=_horizon_days())

    stats = {"created": 0, "cancelled": 0, "skipped": 0}

    exceptions = list(schedule.exceptions.all())
    rules = list(schedule.suspension_rules.filter(is_active=True).select_related("ris_organization"))
    existing_dates = set(
        FactionMeeting.objects.filter(schedule=schedule, scheduled_date__isnull=False).values_list(
            "scheduled_date", flat=True
        )
    )

    organization = schedule.organization
    faction_settings = (organization.settings or {}).get("faction", {})

    for date in occurrence_dates(schedule, today, horizon_end):
        if date in existing_dates:
            stats["skipped"] += 1
            continue

        start = _occurrence_start(schedule, date)
        if start <= now:
            # Heutige Termine, deren Beginn bereits verstrichen ist,
            # werden nicht mehr nachträglich erzeugt
            stats["skipped"] += 1
            continue

        reason = evaluate_cancellation(schedule, date, exceptions=exceptions, rules=rules)

        meeting = FactionMeeting(
            organization=organization,
            schedule=schedule,
            scheduled_date=date,
            title=schedule.name,
            start=start,
            end=start + timedelta(minutes=schedule.duration_minutes or 120),
            location=schedule.default_location,
            is_virtual=bool(schedule.default_video_link),
            video_link=schedule.default_video_link,
            created_by=None,
        )

        if reason:
            # Ersatzlos gestrichen — als "entfällt" sichtbar, kein Verschieben
            meeting.status = "cancelled"
            meeting.cancellation_reason = reason[:300]
            meeting._audit_created_action = "auto_cancelled"
            meeting.save()
            stats["cancelled"] += 1
            continue

        meeting.status = "planned"
        meeting.meeting_number = FactionMeeting.get_next_meeting_number(organization)

        # Vorherige Sitzung verketten (OneToOne — nur wenn noch frei)
        previous = FactionMeeting.find_previous_meeting(organization, before_date=start)
        if previous is not None and not FactionMeeting.objects.filter(previous_meeting=previous).exists():
            meeting.previous_meeting = previous

        meeting._audit_created_action = "generated"
        meeting.save()

        # Anwesenheiten für alle aktiven Mitglieder (wie manuelle Anlage)
        from apps.work.faction.models import FactionAttendance

        for member in organization.memberships.filter(is_active=True):
            FactionAttendance.objects.create(meeting=meeting, membership=member, status="invited")

        # Automatischer erster TOP (Genehmigung TO/Protokoll)
        if faction_settings.get("auto_create_approval_item", True):
            from apps.work.faction.services import ProtocolApprovalService

            try:
                ProtocolApprovalService.auto_create_approval_item(meeting)
            except Exception:
                logger.exception("Genehmigungs-TOP konnte nicht erstellt werden (meeting=%s)", meeting.id)

        stats["created"] += 1

    return stats


def run_faction_schedule_pass(now=None) -> dict:
    """
    Periodischer Erzeugungslauf (Issue #61).

    Wird vom Sync-Watchdog-Zyklus (insight_sync/daemon.py) aufgerufen —
    analog zum Erinnerungslauf. Ein Cache-Lock verhindert parallele Läufe.

    Returns:
        Statistik-Dict (schedules, created, cancelled bzw. skipped-Grund).
    """
    from django.core.cache import cache

    from apps.work.faction.models import FactionMeetingSchedule

    now = now or timezone.now()

    if not cache.add(_SCHEDULE_LOCK_KEY, "1", timeout=_SCHEDULE_LOCK_TIMEOUT):
        return {"skipped": "lock"}

    try:
        stats = {"schedules": 0, "created": 0, "cancelled": 0}
        schedules = FactionMeetingSchedule.objects.filter(is_active=True, organization__is_active=True).select_related(
            "organization"
        )
        for schedule in schedules:
            try:
                result = generate_meetings_for_schedule(schedule, now=now)
            except Exception:
                logger.exception("Sitzungserzeugung fehlgeschlagen (schedule=%s)", schedule.id)
                continue
            stats["schedules"] += 1
            stats["created"] += result["created"]
            stats["cancelled"] += result["cancelled"]

        if stats["created"] or stats["cancelled"]:
            logger.info(
                "Fraktions-Sitzungserzeugung: %d Reihe(n), %d erzeugt, %d entfallen",
                stats["schedules"],
                stats["created"],
                stats["cancelled"],
            )
        return stats
    finally:
        cache.delete(_SCHEDULE_LOCK_KEY)
