# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lesende Zugriffe für die RIS-Datenansicht im Work-Portal (Issue #160, Service-Layer).

Alle Querysets sind an die Kommunen (``OParlBody``) einer Organisation gebunden;
die Views reichen nur noch Filterparameter durch. Die Beschlusskontrolle liest
zusätzlich die Session-Mandanten, die mit diesen Kommunen verknüpft sind.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, cast
from uuid import UUID

from django.db.models import Count, Exists, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from apps.session.models import SessionAgendaItem, SessionOrganization, SessionTenant
from apps.session.services.resolution_service import DECIDED_RESULTS
from apps.tenants.models import Organization
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)

Bodies = QuerySet[OParlBody]

DECISION_ORGANIZATION_TYPES = ("council", "committee", "advisory")


# ---------------------------------------------------------------------------
# Kommunen der Organisation
# ---------------------------------------------------------------------------


def bodies_for_organization(organization: Organization) -> Bodies:
    """Alle verknüpften Kommunen (M2M ``bodies`` + primärer FK ``body``), dedupliziert."""
    return cast("QuerySet[OParlBody]", cast(Any, organization).get_all_bodies())


def primary_body(organization: Organization) -> OParlBody | None:
    """Primäre Kommune (FK), Fallback: erste verknüpfte Kommune."""
    return cast("OParlBody | None", cast(Any, organization).get_primary_body())


def _active_filter(today: date) -> Q:
    """Gremien/Mitgliedschaften ohne Enddatum oder mit Enddatum in der Zukunft."""
    return Q(end_date__isnull=True) | Q(end_date__gte=today)


def _start_of_today(now: datetime) -> datetime:
    """Sitzungen gelten erst ab Mitternacht als vergangen, nicht ab Sitzungsbeginn."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Übersicht
# ---------------------------------------------------------------------------


def overview_stats(bodies: Bodies) -> dict[str, int]:
    """Kennzahlen der Übersichtsseite (Vorgänge, Sitzungen, Gremien, Personen)."""
    now = timezone.now()
    today = now.date()
    organizations = OParlOrganization.objects.filter(body__in=bodies)
    return {
        "papers_total": OParlPaper.objects.filter(body__in=bodies).count(),
        "papers_this_year": OParlPaper.objects.filter(body__in=bodies, date__year=today.year).count(),
        "meetings_total": OParlMeeting.objects.filter(body__in=bodies).count(),
        "meetings_upcoming": OParlMeeting.objects.filter(body__in=bodies, start__gt=now, cancelled=False).count(),
        "organizations_total": organizations.count(),
        "organizations_active": organizations.filter(_active_filter(today)).count(),
        "persons_total": OParlPerson.objects.filter(body__in=bodies).count(),
    }


def recent_papers(bodies: Bodies, *, limit: int = 5) -> QuerySet[OParlPaper]:
    """Neueste Vorgänge."""
    return OParlPaper.objects.filter(body__in=bodies).order_by("-date", "-oparl_created")[:limit]


def upcoming_meetings(bodies: Bodies, *, limit: int = 5) -> QuerySet[OParlMeeting]:
    """Nächste, nicht abgesagte Sitzungen."""
    return (
        OParlMeeting.objects.filter(body__in=bodies, start__gt=timezone.now(), cancelled=False)
        .prefetch_related("organizations")
        .order_by("start")[:limit]
    )


# ---------------------------------------------------------------------------
# Sitzungen
# ---------------------------------------------------------------------------


def meetings_in_bodies(bodies: Bodies) -> QuerySet[OParlMeeting]:
    """Sitzungen der Kommunen (Basis für Detail-Lookups)."""
    return OParlMeeting.objects.filter(body__in=bodies)


def meetings_queryset(
    bodies: Bodies, *, view_mode: str = "upcoming", organization_id: str = "", year: int | None = None
) -> QuerySet[OParlMeeting]:
    """Sitzungsliste nach Ansicht (upcoming/past/all), Gremium und Jahr."""
    meetings = meetings_in_bodies(bodies).prefetch_related("organizations")
    now = timezone.now()
    if view_mode == "upcoming":
        meetings = meetings.filter(start__gt=now, cancelled=False).order_by("start")
    elif view_mode == "past":
        meetings = meetings.filter(start__lte=now).order_by("-start")
    else:
        meetings = meetings.order_by("-start")
    if organization_id:
        meetings = meetings.filter(organizations__id=organization_id)
    if year is not None:
        meetings = meetings.filter(start__year=year)
    return meetings


def meeting_years(bodies: Bodies) -> QuerySet[OParlMeeting, date]:
    """Jahre mit Sitzungen, absteigend."""
    return meetings_in_bodies(bodies).filter(start__isnull=False).dates("start", "year", order="DESC")


def organizations_for_filter(bodies: Bodies) -> QuerySet[OParlOrganization]:
    """Gremien der Kommunen alphabetisch (Filter-Dropdown)."""
    return OParlOrganization.objects.filter(body__in=bodies).order_by("name")


def _natural_key(number: str | None) -> list[tuple[int, int | str]]:
    """Natürliche Sortierung von TOP-Nummern: 1, 2, 10 statt 1, 10, 2."""
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in re.split(r"(\d+)", number or "999") if p]


def agenda_items_with_papers(meeting: OParlMeeting) -> list[dict[str, Any]]:
    """Tagesordnung natürlich sortiert, je TOP mit verknüpften Vorgängen."""
    items = sorted(meeting.agenda_items.all(), key=lambda item: _natural_key(item.number))
    return [{"item": item, "papers": cast(Any, item).get_papers()} for item in items]


# ---------------------------------------------------------------------------
# Gremien
# ---------------------------------------------------------------------------


def organizations_in_bodies(bodies: Bodies) -> QuerySet[OParlOrganization]:
    """Gremien der Kommunen (Basis für Detail-Lookups)."""
    return OParlOrganization.objects.filter(body__in=bodies)


def _has_any_meeting() -> Exists:
    return Exists(OParlMeeting.objects.filter(organizations=OuterRef("pk")))


def organizations_with_meeting_info(bodies: Bodies) -> QuerySet[OParlOrganization]:
    """Gremien mit Annotationen ``next_meeting``, ``last_meeting`` und ``has_meetings``."""
    today_start = _start_of_today(timezone.now())
    next_meeting = Subquery(
        OParlMeeting.objects.filter(organizations=OuterRef("pk"), start__gte=today_start, cancelled=False)
        .order_by("start")
        .values("start")[:1]
    )
    last_meeting = Subquery(
        OParlMeeting.objects.filter(organizations=OuterRef("pk"), start__lt=today_start)
        .order_by("-start")
        .values("start")[:1]
    )
    return organizations_in_bodies(bodies).annotate(
        next_meeting=next_meeting, last_meeting=last_meeting, has_meetings=_has_any_meeting()
    )


def filter_organizations(
    organizations: QuerySet[OParlOrganization], *, search: str = "", active_only: bool = True
) -> QuerySet[OParlOrganization]:
    """Suche nach Name/Kurzname und Tab-Filter (aktiv = laufend und mit Sitzungen)."""
    if search:
        organizations = organizations.filter(Q(name__icontains=search) | Q(short_name__icontains=search))
    if active_only:
        organizations = organizations.filter(_active_filter(timezone.now().date()), _has_any_meeting())
    return organizations


def ranked_organizations(organizations: QuerySet[OParlOrganization]) -> QuerySet[OParlOrganization]:
    """Nach Gremientyp und Aktivität sortieren (Rat zuerst, inaktive ans Ende)."""
    from insight_core.ranking import sort_organizations_by_ranking

    return cast(
        "QuerySet[OParlOrganization]",
        cast(Any, sort_organizations_by_ranking)(organizations, include_activity=True),
    )


def organization_tab_counts(bodies: Bodies) -> dict[str, int]:
    """Zähler der Tabs "aktiv" und "alle" (ohne Suchfilter)."""
    all_orgs = organizations_in_bodies(bodies)
    return {
        "active_count": all_orgs.filter(_active_filter(timezone.now().date()), _has_any_meeting()).count(),
        "all_count": all_orgs.count(),
    }


def organization_members(organization: OParlOrganization) -> dict[str, QuerySet[OParlMembership]]:
    """Aktive und ehemalige Mitglieder eines Gremiums."""
    today = timezone.now().date()
    memberships = organization.memberships.select_related("person")
    return {
        "active": memberships.filter(_active_filter(today)).order_by("person__family_name", "person__name"),
        "past": memberships.filter(end_date__lt=today).order_by("-end_date"),
    }


def organization_meetings(
    organization: OParlOrganization, *, past_limit: int = 30
) -> dict[str, QuerySet[OParlMeeting]]:
    """Kommende (ab heute) und vergangene Sitzungen eines Gremiums."""
    today_start = _start_of_today(timezone.now())
    return {
        "upcoming": organization.meetings.filter(start__gte=today_start, cancelled=False).order_by("start"),
        "past": organization.meetings.filter(start__lt=today_start).order_by("-start")[:past_limit],
    }


# ---------------------------------------------------------------------------
# Personen
# ---------------------------------------------------------------------------


def persons_in_bodies(bodies: Bodies) -> QuerySet[OParlPerson]:
    """Personen der Kommunen (Basis für Detail-Lookups)."""
    return OParlPerson.objects.filter(body__in=bodies)


def persons_queryset(bodies: Bodies, *, search: str = "") -> QuerySet[OParlPerson]:
    """Personenliste mit Ratsrolle (falls ein Gremium "Rat" existiert), Suche und Mitgliedschaftszahl."""
    persons = persons_in_bodies(bodies)
    rat_orgs = OParlOrganization.objects.filter(body__in=bodies, name="Rat")
    if rat_orgs.exists():
        council_role = Subquery(
            OParlMembership.objects.filter(person=OuterRef("pk"), organization__in=rat_orgs)
            .filter(_active_filter(timezone.now().date()))
            .values("role")[:1]
        )
        persons = persons.annotate(council_role=council_role)
    if search:
        persons = persons.filter(
            Q(name__icontains=search)
            | Q(family_name__icontains=search)
            | Q(given_name__icontains=search)
            | Q(email__icontains=search)
        )
    return persons.annotate(membership_count=Count("memberships")).order_by("family_name", "given_name")


def person_memberships(person: OParlPerson) -> dict[str, QuerySet[OParlMembership]]:
    """Aktive und ehemalige Mitgliedschaften einer Person."""
    today = timezone.now().date()
    memberships = person.memberships.select_related("organization")
    return {
        "active": memberships.filter(_active_filter(today)).order_by("organization__name"),
        "past": memberships.filter(end_date__lt=today).order_by("-end_date"),
    }


# ---------------------------------------------------------------------------
# Vorgänge
# ---------------------------------------------------------------------------


def papers_in_bodies(bodies: Bodies) -> QuerySet[OParlPaper]:
    """Vorgänge der Kommunen (Basis für Detail-Lookups)."""
    return OParlPaper.objects.filter(body__in=bodies)


def papers_queryset(
    bodies: Bodies, *, search: str = "", paper_type: str = "", year: int | None = None
) -> QuerySet[OParlPaper]:
    """Vorgangsliste mit Suche (Name/Aktenzeichen), Art und Jahr, neueste zuerst."""
    papers = papers_in_bodies(bodies)
    if search:
        papers = papers.filter(Q(name__icontains=search) | Q(reference__icontains=search))
    if paper_type:
        papers = papers.filter(paper_type=paper_type)
    if year is not None:
        papers = papers.filter(date__year=year)
    return papers.order_by("-date", "-oparl_created")


def paper_types(bodies: Bodies) -> QuerySet[OParlPaper, Any]:
    """Vorhandene Vorlagenarten (inkl. leer/NULL, wie im Filter-Dropdown angezeigt)."""
    return papers_in_bodies(bodies).values_list("paper_type", flat=True).distinct().order_by("paper_type")


def paper_years(bodies: Bodies) -> QuerySet[OParlPaper, date]:
    """Jahre mit Vorgängen, absteigend."""
    return papers_in_bodies(bodies).filter(date__isnull=False).dates("date", "year", order="DESC")


def _raw_file(data: dict[str, Any], *, is_main: bool) -> dict[str, Any]:
    default_name = "Hauptdokument" if is_main else "Dokument"
    return {
        "name": data.get("name", data.get("fileName", default_name)),
        "file_name": data.get("fileName", ""),
        "mime_type": data.get("mimeType", ""),
        "access_url": data.get("accessUrl", ""),
        "download_url": data.get("downloadUrl", ""),
        "is_main": is_main,
    }


def paper_files(paper: OParlPaper) -> tuple[QuerySet[OParlFile] | list[dict[str, Any]], bool]:
    """
    Dateien eines Vorgangs.

    Bevorzugt die Datenbank-Relation; ist sie leer, werden ``mainFile`` und
    ``auxiliaryFile`` aus ``raw_json`` gelesen. Zweiter Rückgabewert: ``True``,
    wenn die Dateien aus ``raw_json`` stammen.
    """
    db_files = paper.files.all()
    if db_files.exists():
        return db_files, False
    raw_json = paper.raw_json or {}
    raw_files: list[dict[str, Any]] = []
    main_file = raw_json.get("mainFile")
    if main_file and isinstance(main_file, dict):
        raw_files.append(_raw_file(main_file, is_main=True))
    aux_files = raw_json.get("auxiliaryFile", [])
    if isinstance(aux_files, list):
        raw_files.extend(_raw_file(af, is_main=False) for af in aux_files if isinstance(af, dict))
    return raw_files, True


def enriched_consultations(paper: OParlPaper) -> list[dict[str, Any]]:
    """
    Beratungsfolge eines Vorgangs mit aufgelösten Sitzungen und TOPs.

    OParl verknüpft Consultation → Meeting/AgendaItem nur über ``external_id``-Strings;
    beide werden gebündelt nachgeladen. Ergebnis chronologisch (älteste zuerst).
    """
    consultations: list[OParlConsultation] = list(paper.consultations.all())
    if not consultations:
        return []

    meeting_ids = [c.meeting_external_id for c in consultations if c.meeting_external_id]
    agenda_item_ids = [c.agenda_item_external_id for c in consultations if c.agenda_item_external_id]

    meetings_by_id: dict[str, OParlMeeting] = {}
    if meeting_ids:
        meetings = OParlMeeting.objects.filter(external_id__in=meeting_ids).prefetch_related("organizations")
        meetings_by_id = {m.external_id: m for m in meetings}
    agenda_items_by_id: dict[str, OParlAgendaItem] = {}
    if agenda_item_ids:
        agenda_items_by_id = {a.external_id: a for a in OParlAgendaItem.objects.filter(external_id__in=agenda_item_ids)}

    result: list[dict[str, Any]] = []
    for consultation in consultations:
        meeting = meetings_by_id.get(consultation.meeting_external_id or "")
        agenda_item = agenda_items_by_id.get(consultation.agenda_item_external_id or "")
        org_name = None
        if meeting:
            orgs = list(meeting.organizations.all())
            if orgs:
                org_name = orgs[0].name or orgs[0].short_name
        result.append(
            {
                "consultation": consultation,
                "meeting": meeting,
                "agenda_item": agenda_item,
                "date": meeting.start if meeting else None,
                "organization_name": org_name,
                "meeting_name": meeting.name if meeting else None,
                "agenda_number": agenda_item.number if agenda_item else None,
                "result": agenda_item.result if agenda_item else None,
                "public": agenda_item.public if agenda_item else True,
                "role": consultation.role,
                "authoritative": consultation.authoritative,
            }
        )
    now = timezone.now()
    result.sort(key=lambda entry: entry["date"] or now)
    return result


# ---------------------------------------------------------------------------
# Dokumente, Karte
# ---------------------------------------------------------------------------


def files_queryset(bodies: Bodies, *, search: str = "") -> QuerySet[OParlFile]:
    """Dokumente der Kommunen, neueste zuerst; Suche über Name, Dateiname und Vorgang."""
    files = OParlFile.objects.filter(body__in=bodies).select_related("paper").order_by("-file_date", "-created_at")
    if search:
        files = files.filter(
            Q(name__icontains=search) | Q(file_name__icontains=search) | Q(paper__name__icontains=search)
        )
    return files


def annotate_files_with_context(files: Any) -> None:
    """Hängt ``context_info`` (Gremium, Sitzung, TOP) an eine Seite Dateien."""
    from insight_core.views import _annotate_files_with_context

    cast(Any, _annotate_files_with_context)(files)


def papers_with_locations(bodies: Bodies, *, limit: int = 500) -> QuerySet[OParlPaper]:
    """Vorgänge mit georeferenzierten Orten (Kartenansicht)."""
    return papers_in_bodies(bodies).filter(locations__isnull=False).exclude(locations=[])[:limit]


# ---------------------------------------------------------------------------
# Suche (Filteroptionen und ORM-Fallback)
# ---------------------------------------------------------------------------


def search_committees(bodies: Bodies) -> QuerySet[OParlOrganization, Any]:
    """Gremiennamen für das Filter-Dropdown."""
    return organizations_in_bodies(bodies).exclude(name="").order_by("name").values_list("name", flat=True)


def search_paper_types(bodies: Bodies) -> QuerySet[OParlPaper, Any]:
    """Vorlagenarten (ohne leer/NULL) für das Filter-Dropdown."""
    return (
        papers_in_bodies(bodies)
        .exclude(paper_type__isnull=True)
        .exclude(paper_type="")
        .values_list("paper_type", flat=True)
        .distinct()
        .order_by("paper_type")
    )


def orm_search(
    body_ids: Sequence[str | UUID],
    *,
    query: str = "",
    date_from: str = "",
    date_to: str = "",
    paper_type: str = "",
    committee: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """
    Einfache ORM-Suche (Fallback ohne Elasticsearch, ohne OCR-Volltext).

    Returns:
        ``{"papers", "meetings", "organizations", "persons", "total"}``
    """
    ids = [UUID(str(body_id)) for body_id in body_ids]
    papers = OParlPaper.objects.filter(body_id__in=ids)
    meetings = OParlMeeting.objects.filter(body_id__in=ids)
    if query:
        papers = papers.filter(Q(name__icontains=query) | Q(reference__icontains=query))
        meetings = meetings.filter(Q(name__icontains=query) | Q(location_name__icontains=query))
    if date_from:
        papers = papers.filter(date__gte=date_from)
        meetings = meetings.filter(start__gte=date_from)
    if date_to:
        papers = papers.filter(date__lte=date_to)
        meetings = meetings.filter(start__lte=date_to)
    if paper_type:
        papers = papers.filter(paper_type=paper_type)
    if committee:
        meetings = meetings.filter(organizations__name=committee)

    papers = papers.order_by("-date")[:limit]
    meetings = meetings.prefetch_related("organizations").order_by("-start")[:limit]
    organizations = (
        OParlOrganization.objects.filter(body_id__in=ids)
        .filter(Q(name__icontains=query) | Q(short_name__icontains=query))
        .order_by("name")[:limit]
        if query
        else OParlOrganization.objects.none()
    )
    persons = (
        OParlPerson.objects.filter(body_id__in=ids)
        .filter(Q(name__icontains=query) | Q(family_name__icontains=query) | Q(given_name__icontains=query))
        .order_by("family_name")[:limit]
        if query
        else OParlPerson.objects.none()
    )
    return {
        "papers": papers,
        "meetings": meetings,
        "organizations": organizations,
        "persons": persons,
        "total": len(papers) + len(meetings) + organizations.count() + persons.count(),
    }


# ---------------------------------------------------------------------------
# Beschlusskontrolle (Session-Mandanten der Kommunen)
# ---------------------------------------------------------------------------


def session_tenants(bodies: Bodies) -> QuerySet[SessionTenant]:
    """Aktive Verwaltungs-Mandanten, die mit den Kommunen verknüpft sind."""
    return SessionTenant.objects.filter(oparl_body__in=bodies, is_active=True)


def decided_items(tenants: QuerySet[SessionTenant]) -> QuerySet[SessionAgendaItem]:
    """Öffentliche, entschiedene TOPs öffentlicher Sitzungen (nie nicht-öffentliche Inhalte)."""
    return (
        SessionAgendaItem.objects.filter(
            meeting__tenant__in=tenants,
            vote_result__in=DECIDED_RESULTS,
            is_public=True,
            meeting__is_public=True,
        )
        .exclude(is_withdrawn=True)
        .select_related("meeting__organization", "meeting__tenant", "paper")
        .order_by("-meeting__start", "order")
    )


def filter_decisions(
    items: QuerySet[SessionAgendaItem], *, organization_id: str = "", year: str = "", query: str = ""
) -> QuerySet[SessionAgendaItem]:
    """Filter nach Gremium, Jahr und Volltext (Titel, Beschlusstext, Nummer, Adressat)."""
    if organization_id:
        items = items.filter(Q(meeting__organization_id=organization_id))
    if year.isdigit():
        items = items.filter(meeting__start__year=int(year))
    if query:
        items = items.filter(
            Q(name__icontains=query)
            | Q(resolution_text__icontains=query)
            | Q(resolution_number__icontains=query)
            | Q(implementation_recipient__icontains=query)
        )
    return items


def overdue_filter(today: date) -> Q:
    """Beschlossen, Frist überschritten und nicht umgesetzt."""
    return Q(vote_result="approved", implementation_deadline__lt=today) & ~Q(implementation_status="done")


def filter_implementation(
    items: QuerySet[SessionAgendaItem], *, status: str = "", overdue_only: bool = False, today: date
) -> QuerySet[SessionAgendaItem]:
    """Filter nach Umsetzungsstand (nur beschlossene) und Fristüberschreitung."""
    if status in dict(SessionAgendaItem.IMPLEMENTATION_CHOICES):
        items = items.filter(vote_result="approved", implementation_status=status)
    if overdue_only:
        items = items.filter(overdue_filter(today))
    return items


def tracking_stats(items: QuerySet[SessionAgendaItem], today: date) -> dict[str, int]:
    """Ampel-Kennzahlen zum Umsetzungsstand."""
    approved = items.filter(vote_result="approved")
    return {
        "open": approved.filter(implementation_status="open").count(),
        "in_progress": approved.filter(implementation_status="in_progress").count(),
        "done": approved.filter(implementation_status="done").count(),
        "deferred": approved.filter(implementation_status="deferred").count(),
        "overdue": items.filter(overdue_filter(today)).count(),
        "total": items.count(),
    }


def decision_organizations(tenants: QuerySet[SessionTenant]) -> QuerySet[SessionOrganization]:
    """Beschlussfassende Gremien der Mandanten (Rat, Ausschüsse, Beiräte)."""
    return SessionOrganization.objects.filter(
        tenant__in=tenants, is_active=True, organization_type__in=DECISION_ORGANIZATION_TYPES
    ).order_by("name")


def decision_years(tenants: QuerySet[SessionTenant]) -> list[int]:
    """Jahre mit öffentlichen Beschlüssen, absteigend."""
    starts = SessionAgendaItem.objects.filter(
        meeting__tenant__in=tenants, vote_result__in=DECIDED_RESULTS, is_public=True
    ).values_list("meeting__start", flat=True)
    return sorted({d.year for d in starts if d}, reverse=True)
