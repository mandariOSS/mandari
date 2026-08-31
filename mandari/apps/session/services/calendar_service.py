# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungskalender und Jahresplanung (Issue #82).

- Monatskalender über alle Gremien
- Serientermine (Jahresplanung) mit Vorschau
- Kollisionsprüfung: gleicher Raum am selben Tag oder zeitliche Überschneidung
- Sitzungsplan-PDF (Jahresübersicht)
- ICS-Abo-Feed je Gremium (nur öffentliche Sitzungen)
"""

import calendar as _calendar
from datetime import date, datetime, time, timedelta

from django.template.loader import render_to_string
from django.utils import timezone

from apps.common.ical import build_ics_feed
from apps.common.pdf import html_to_pdf

from ..models import SessionMeeting, SessionOrganization, SessionTenant

# Ohne gepflegtes Ende nehmen wir eine typische Sitzungsdauer für die
# Überschneidungsprüfung an.
DEFAULT_DURATION = timedelta(hours=3)

RHYTHM_CHOICES = [
    ("weekly", "Wöchentlich"),
    ("biweekly", "14-täglich"),
    ("monthly_1", "Monatlich (1. Woche)"),
    ("monthly_2", "Monatlich (2. Woche)"),
    ("monthly_3", "Monatlich (3. Woche)"),
    ("monthly_4", "Monatlich (4. Woche)"),
]

WEEKDAY_CHOICES = [
    (0, "Montag"),
    (1, "Dienstag"),
    (2, "Mittwoch"),
    (3, "Donnerstag"),
    (4, "Freitag"),
    (5, "Samstag"),
    (6, "Sonntag"),
]


def _window(start: datetime, end: datetime | None) -> tuple[datetime, datetime]:
    return start, end or (start + DEFAULT_DURATION)


def find_conflicts(
    tenant: SessionTenant,
    start: datetime,
    *,
    end: datetime | None = None,
    room: str = "",
    exclude_id=None,
) -> list[SessionMeeting]:
    """
    Kollidierende Sitzungen zu einem geplanten Termin finden.

    Kollision = am selben Tag UND (gleicher Raum ODER zeitliche
    Überschneidung der Sitzungsfenster).
    """
    day = timezone.localtime(start).date()
    qs = (
        SessionMeeting.objects.filter(tenant=tenant, cancelled=False, start__date=day)
        .select_related("organization")
        .order_by("start")
    )
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)

    new_start, new_end = _window(start, end)
    room_norm = room.strip().lower()

    conflicts = []
    for meeting in qs:
        other_start, other_end = _window(meeting.start, meeting.end)
        overlaps = new_start < other_end and other_start < new_end
        same_room = bool(room_norm) and meeting.room.strip().lower() == room_norm
        if overlaps or same_room:
            meeting.conflict_reason = "Raum belegt" if same_room and not overlaps else "Zeitliche Überschneidung"
            conflicts.append(meeting)
    return conflicts


def generate_series(
    *,
    rhythm: str,
    weekday: int,
    start_time: time,
    date_from: date,
    date_to: date,
) -> list[datetime]:
    """
    Termine einer Sitzungsserie berechnen (aware datetimes, lokale Zeitzone).

    monthly_N = N-ter <Wochentag> im Monat.
    """
    if date_to < date_from or (date_to - date_from).days > 400:
        return []

    tz = timezone.get_current_timezone()
    results = []

    if rhythm in ("weekly", "biweekly"):
        step = timedelta(days=7 if rhythm == "weekly" else 14)
        # Erster passender Wochentag ab date_from
        current = date_from + timedelta(days=(weekday - date_from.weekday()) % 7)
        while current <= date_to:
            results.append(timezone.make_aware(datetime.combine(current, start_time), tz))
            current += step
    elif rhythm.startswith("monthly_"):
        nth = int(rhythm.split("_")[1])
        year, month = date_from.year, date_from.month
        while (year, month) <= (date_to.year, date_to.month):
            first = date(year, month, 1)
            day = first + timedelta(days=(weekday - first.weekday()) % 7 + (nth - 1) * 7)
            if day.month == month and date_from <= day <= date_to:
                results.append(timezone.make_aware(datetime.combine(day, start_time), tz))
            month += 1
            if month > 12:
                month, year = 1, year + 1
    return results


def month_grid(tenant: SessionTenant, year: int, month: int, *, include_non_public: bool):
    """
    Wochen eines Monats mit den Sitzungen je Tag.

    Returns:
        (weeks, meetings_count): weeks = Liste von Wochen, jede Woche eine
        Liste von dicts {day, in_month, is_today, meetings}
    """
    qs = (
        SessionMeeting.objects.filter(tenant=tenant, start__year=year, start__month=month)
        .select_related("organization")
        .order_by("start")
    )
    if not include_non_public:
        qs = qs.filter(is_public=True)

    by_day: dict[date, list[SessionMeeting]] = {}
    for meeting in qs:
        day = timezone.localtime(meeting.start).date()
        by_day.setdefault(day, []).append(meeting)

    today = timezone.localdate()
    weeks = []
    for week in _calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        weeks.append(
            [
                {
                    "day": day,
                    "in_month": day.month == month,
                    "is_today": day == today,
                    "meetings": by_day.get(day, []),
                }
                for day in week
            ]
        )
    return weeks, qs.count()


def year_meetings(tenant: SessionTenant, year: int, *, organization=None, include_non_public: bool):
    """Sitzungen eines Jahres, gruppiert nach Monat (für den Sitzungsplan)."""
    qs = (
        SessionMeeting.objects.filter(tenant=tenant, start__year=year, cancelled=False)
        .select_related("organization")
        .order_by("start")
    )
    if organization is not None:
        qs = qs.filter(organization=organization)
    if not include_non_public:
        qs = qs.filter(is_public=True)

    months: dict[int, list[SessionMeeting]] = {}
    for meeting in qs:
        months.setdefault(timezone.localtime(meeting.start).month, []).append(meeting)
    return [{"month": month, "meetings": meetings} for month, meetings in sorted(months.items())]


def build_year_plan_pdf(tenant: SessionTenant, year: int, *, organization=None, include_non_public: bool) -> bytes:
    """Sitzungsplan-PDF (Jahresübersicht, optional je Gremium)."""
    context = {
        "tenant": tenant,
        "year": year,
        "organization": organization,
        "months": year_meetings(tenant, year, organization=organization, include_non_public=include_non_public),
        "internal": include_non_public,
        "generated_at": timezone.localtime(),
        "address_lines": [line for line in (tenant.address or "").splitlines() if line.strip()],
    }
    html = render_to_string("session/pdf/year_plan.html", context)
    return html_to_pdf(html)


def build_organization_feed(organization: SessionOrganization) -> bytes:
    """
    ICS-Abo-Feed eines Gremiums.

    Enthält bewusst NUR öffentliche, nicht abgesagte Sitzungen (der Feed ist
    ohne Anmeldung abonnierbar, z. B. in Outlook) — nichtöffentliche
    Sitzungen erscheinen hier nie.
    """
    since = timezone.now() - timedelta(days=90)
    meetings = SessionMeeting.objects.filter(
        organization=organization,
        is_public=True,
        cancelled=False,
        start__gte=since,
    ).order_by("start")[:200]
    events = []
    for meeting in meetings:
        location = ", ".join(part for part in (meeting.location, meeting.room) if part)
        events.append(
            {
                "uid": f"session-meeting-{meeting.pk}@mandari",
                "summary": meeting.name,
                "start": meeting.start,
                "end": meeting.end,
                "description": f"Sitzung des Gremiums {organization.name}",
                "location": location,
            }
        )
    return build_ics_feed(events, name=f"{organization.tenant.name}: {organization.name}")
