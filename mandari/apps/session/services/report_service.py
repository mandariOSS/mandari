# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Statistiken und Berichte für den Sitzungsdienst (Issue #84).

- Anwesenheitsstatistik je Person (eingeladen/anwesend/entschuldigt, Quote)
- Sitzungsstatistik je Gremium (Sitzungen, TOPs, Beschlüsse, Ø-Dauer)
- Sitzungsgeld-Jahresbericht (Summen je Person und Status)
- Vorlagen-Durchlaufzeiten (Entwurf bis Freigabe)
"""

from statistics import median

from django.db.models import Count, Q
from django.utils import timezone

from ..models import (
    SessionAgendaItem,
    SessionAllowance,
    SessionAttendance,
    SessionMeeting,
    SessionPaper,
)

# Als „anwesend" zählen auch Verspätete und vorzeitig Gegangene
PRESENT_STATUSES = ("present", "joined_late", "left_early")
DECIDED_RESULTS = ("approved", "rejected", "deferred", "noted")


def _meetings(tenant, year, organization=None, *, include_non_public):
    qs = SessionMeeting.objects.filter(tenant=tenant, start__year=year, cancelled=False)
    if organization is not None:
        qs = qs.filter(organization=organization)
    if not include_non_public:
        qs = qs.filter(is_public=True)
    return qs


def attendance_stats(tenant, year, organization=None, *, include_non_public):
    """Anwesenheit je Person: eingeladen, anwesend, entschuldigt, Quote."""
    meetings = _meetings(tenant, year, organization, include_non_public=include_non_public)
    rows = (
        SessionAttendance.objects.filter(meeting__in=meetings)
        .values("person_id", "person__given_name", "person__family_name")
        .annotate(
            invited=Count("id"),
            present=Count("id", filter=Q(status__in=PRESENT_STATUSES)),
            excused=Count("id", filter=Q(status__in=("excused", "declined"))),
            absent=Count("id", filter=Q(status="absent")),
        )
        .order_by("person__family_name", "person__given_name")
    )
    result = []
    for row in rows:
        rate = round(row["present"] / row["invited"] * 100) if row["invited"] else 0
        result.append(
            {
                "name": f"{row['person__given_name']} {row['person__family_name']}".strip(),
                "invited": row["invited"],
                "present": row["present"],
                "excused": row["excused"],
                "absent": row["absent"],
                "rate": rate,
            }
        )
    return result


def meeting_stats(tenant, year, *, include_non_public):
    """Sitzungsstatistik je Gremium: Sitzungen, TOPs, Beschlüsse, Ø-Dauer."""
    meetings = _meetings(tenant, year, include_non_public=include_non_public).select_related("organization")
    per_org: dict = {}
    for meeting in meetings:
        entry = per_org.setdefault(
            meeting.organization_id,
            {"organization": meeting.organization, "meetings": 0, "durations": []},
        )
        entry["meetings"] += 1
        start = meeting.actual_start or meeting.start
        end = meeting.actual_end or meeting.end
        if start and end and end > start:
            entry["durations"].append((end - start).total_seconds() / 60)

    items = (
        SessionAgendaItem.objects.filter(meeting__in=meetings)
        .values("meeting__organization_id")
        .annotate(
            tops=Count("id"),
            resolutions=Count("id", filter=Q(vote_result__in=DECIDED_RESULTS)),
        )
    )
    item_map = {row["meeting__organization_id"]: row for row in items}

    result = []
    for org_id, entry in per_org.items():
        row = item_map.get(org_id, {})
        durations = entry.pop("durations")
        entry["tops"] = row.get("tops", 0)
        entry["resolutions"] = row.get("resolutions", 0)
        entry["avg_duration"] = round(sum(durations) / len(durations)) if durations else None
        result.append(entry)
    return sorted(result, key=lambda e: e["organization"].name)


def allowance_stats(tenant, year):
    """Sitzungsgeld-Summen je Person (ohne Stornierungen)."""
    rows = (
        SessionAllowance.objects.filter(
            attendance__meeting__tenant=tenant,
            attendance__meeting__start__year=year,
        )
        .exclude(status="cancelled")
        .select_related("attendance__person")
    )
    per_person: dict = {}
    totals = {"count": 0, "amount": 0, "paid": 0}
    for allowance in rows:
        person = allowance.attendance.person
        entry = per_person.setdefault(
            person.pk,
            {"name": person.display_name, "count": 0, "amount": 0, "paid": 0},
        )
        entry["count"] += 1
        entry["amount"] += allowance.amount
        totals["count"] += 1
        totals["amount"] += allowance.amount
        if allowance.status == "paid":
            entry["paid"] += allowance.amount
            totals["paid"] += allowance.amount
    return sorted(per_person.values(), key=lambda e: e["name"]), totals


def paper_throughput(tenant, year):
    """Durchlaufzeit der Vorlagen (Anlage bis Freigabe) in Tagen."""
    papers = SessionPaper.objects.filter(
        tenant=tenant,
        status__in=("approved", "scheduled", "completed"),
        updated_at__year=year,
    )
    durations = [
        (paper.updated_at - paper.created_at).days for paper in papers if paper.updated_at and paper.created_at
    ]
    if not durations:
        return {"count": 0, "median_days": None, "max_days": None}
    return {
        "count": len(durations),
        "median_days": round(median(durations)),
        "max_days": max(durations),
    }


def available_years(tenant):
    years = {
        value.year for value in SessionMeeting.objects.filter(tenant=tenant).values_list("start", flat=True) if value
    }
    years.add(timezone.localdate().year)
    return sorted(years, reverse=True)
