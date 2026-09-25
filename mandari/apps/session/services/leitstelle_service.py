# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Leitstellen-Übersicht über die Mandanten einer Mandantengruppe (Issue #317).

Zugang
    Nur über eine aktive Mitgliedschaft in der Leitstelle einer aktiven Gruppe
    (``SessionTenantGroupMembership``). Ohne sie antwortet die Seite mit 404 – auch für Superuser.
    Die Gruppenrolle öffnet keine Mandantenseite: Links in einen Mandanten erscheinen nur, wo die
    Person dort selbst Mitglied ist und das Recht für die Zielseite hat; sonst steht „kein Zugang“.

Sichtbarkeit – strikt je Mandant
    - Öffentliche Vorlagen, Sitzungen und TOPs erscheinen mit Titel.
    - Nichtöffentliche nur für Mandanten, in denen die Person über ihre eigene Mitgliedschaft das
      NÖ-Sichtrecht hat (``view_non_public_papers`` bzw. ``view_non_public_meetings``). Sonst zeigt
      die Übersicht eine Zeile „Nichtöffentlich“ ohne Titel, Nummer und Gremium; die Suche findet
      solche Vorgänge gar nicht (auch nicht als Zahl – ein Treffer verriete den Inhalt).
    - Verschlüsselte Felder werden nicht geladen und nie entschlüsselt (``defer``).
    - Die Gruppenrolle „Kennzahlen“ sieht nur Zählwerte je Mandant, keine Listen und keine Suche.

Protokoll
    Jede Nutzung ist ein Lesezugriff im Protokoll jedes Mandanten der Gruppe (``audit.log_read``):
    Der Mandant sieht in seinem eigenen, manipulationsgeschützten Protokoll, wer seine Daten über die
    Leitstelle gesehen hat – auch ohne Mitgliedschaft (dann mit dem Konto im Eintrag). Der Eintrag
    fasst eine Ansicht zusammen (Übersicht bzw. Suche, je Person und Mandant einmal in zehn Minuten)
    und gilt unabhängig vom Schalter für Lesezugriffe, weil er einen mandantenübergreifenden Zugriff
    belegt. Suchbegriffe werden nicht gespeichert, nur die Zahl der Treffer im Mandanten.

Leistung
    Die Zahl der Abfragen hängt nicht von der Zahl der Mandanten ab: Kennzahlen als je eine
    gruppierte Abfrage über alle Mandanten, Listen über alle Mandanten mit Obergrenze. Nur das
    Protokoll schreibt je Mandant (höchstens einmal in zehn Minuten).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Q
from django.urls import reverse
from django.utils import timezone

from apps.common.encryption import EncryptedTextField
from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionCosignature,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionTenant,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionUser,
)
from apps.session.permissions import role_permissions
from apps.session.services import joint_meeting_service

#: Einträge je Liste der Übersicht (über alle Mandanten)
LIST_LIMIT = 15
#: Treffer je Trefferart in der Suche
SEARCH_LIMIT = 25
SEARCH_MIN_LENGTH = 2
SEARCH_MAX_LENGTH = 100
#: Vorschau der nächsten Sitzungen in Tagen
MEETING_DAYS = 28
#: Fristen: überfällig oder in den nächsten … Tagen fällig
DEADLINE_DAYS = 14
#: Obergrenze der Sitzungen, aus denen die Ladungsfristen berechnet werden
INVITATION_CANDIDATES = 500

#: Offene Vorlagen (Fristen)
OPEN_PAPER_STATUSES = ("draft", "review")
#: Entschiedene TOPs (Kennzahl „Beschlüsse“), wie in den Berichten (report_service)
DECIDED_RESULTS = ("approved", "rejected", "deferred", "noted")

HIDDEN_PAPER = "Nichtöffentliche Vorlage"
HIDDEN_MEETING = "Nichtöffentliche Sitzung"

VIEW_OVERVIEW = "uebersicht"
VIEW_SEARCH = "suche"

#: Lange Textfelder, die die Übersicht nie braucht (neben den verschlüsselten Feldern)
_TEXT_FIELDS: dict[type[Any], tuple[str, ...]] = {
    SessionPaper: ("main_text", "resolution_text", "financial_impact_note"),
    SessionMeeting: ("invitation_text", "cancellation_reason"),
    SessionAgendaItem: (
        "resolution_text",
        "protocol_note",
        "withdrawn_reason",
        "implementation_note",
        "implementation_public_note",
    ),
}


def _deferred(model: type[Any]) -> list[str]:
    """Verschlüsselte und lange Textfelder eines Modells – werden nicht geladen."""
    encrypted = [f.name for f in model._meta.get_fields() if isinstance(f, EncryptedTextField)]
    return [*encrypted, *_TEXT_FIELDS.get(model, ())]


# =============================================================================
# Zugang und Rechte je Mandant
# =============================================================================


@dataclass
class TenantAccess:
    """Was die Person im Mandanten über ihre eigene Mitgliedschaft darf – die Gruppenrolle zählt hier nicht."""

    tenant: SessionTenant
    session_user: SessionUser | None = None
    permissions: frozenset[str] = frozenset()

    @property
    def is_member(self) -> bool:
        return self.session_user is not None

    def can(self, permission: str) -> bool:
        return self.session_user is not None and permission in self.permissions

    @property
    def sees_non_public_papers(self) -> bool:
        return self.can("view_non_public_papers")

    @property
    def sees_non_public_meetings(self) -> bool:
        return self.can("view_non_public_meetings")

    @property
    def dashboard_url(self) -> str | None:
        if not self.can("view_dashboard"):
            return None
        return reverse("session:dashboard", kwargs={"tenant_slug": self.tenant.slug})


def membership_for(user: Any, group_slug: str) -> SessionTenantGroupMembership | None:
    """Aktive Leitstellen-Mitgliedschaft in einer aktiven Gruppe – sonst None (die Seite antwortet mit 404)."""
    if not getattr(user, "is_authenticated", False):
        return None
    return (
        SessionTenantGroupMembership.objects.select_related("group")
        .filter(user=user, is_active=True, group__slug=group_slug, group__is_active=True)
        .first()
    )


def tenant_accesses(group: SessionTenantGroup, user: Any) -> list[TenantAccess]:
    """Aktive Mandanten der Gruppe mit den Rechten aus der eigenen Mitgliedschaft (ohne Vertretungen)."""
    tenants = list(SessionTenant.objects.filter(group_link__group=group, is_active=True).order_by("name"))
    session_users = {
        su.tenant_id: su
        for su in SessionUser.objects.filter(user=user, is_active=True, tenant__in=tenants).prefetch_related("roles")
    }
    result = []
    for tenant in tenants:
        session_user = session_users.get(tenant.pk)
        permissions = frozenset(role_permissions(session_user)) if session_user is not None else frozenset()
        result.append(TenantAccess(tenant=tenant, session_user=session_user, permissions=permissions))
    return result


# =============================================================================
# Zeilen der Übersicht
# =============================================================================


@dataclass
class Row:
    """Eine Zeile einer Liste; ``hidden`` = Titel, Nummer und Gremium zurückgehalten (Nichtöffentlich)."""

    access: TenantAccess
    title: str
    hidden: bool = False
    reference: str = ""
    detail: str = ""
    when: date | datetime | None = None
    due: date | None = None
    overdue: bool = False
    url: str | None = None

    @property
    def tenant(self) -> SessionTenant:
        return self.access.tenant


def _paper_visible(access: TenantAccess, paper: SessionPaper) -> bool:
    return paper.is_public or access.sees_non_public_papers


def _meeting_visible(access: TenantAccess, meeting: SessionMeeting) -> bool:
    return meeting.is_public or access.sees_non_public_meetings


def _paper_url(access: TenantAccess, paper: SessionPaper) -> str | None:
    if not (access.can("view_papers") and _paper_visible(access, paper)):
        return None
    return reverse("session:paper_detail", kwargs={"tenant_slug": access.tenant.slug, "paper_id": paper.pk})


def _meeting_url(access: TenantAccess, meeting: SessionMeeting) -> str | None:
    if not (access.can("view_meetings") and _meeting_visible(access, meeting)):
        return None
    return reverse("session:meeting_detail", kwargs={"tenant_slug": access.tenant.slug, "meeting_id": meeting.pk})


def _paper_row(access: TenantAccess, paper: SessionPaper, **extra: Any) -> Row:
    if not _paper_visible(access, paper):
        return Row(access=access, title=HIDDEN_PAPER, hidden=True, **extra)
    organization = paper.main_organization.name if paper.main_organization is not None else ""
    extra.setdefault("detail", organization)
    return Row(access=access, title=paper.name, reference=paper.reference, url=_paper_url(access, paper), **extra)


def _meeting_row(access: TenantAccess, meeting: SessionMeeting, **extra: Any) -> Row:
    if not _meeting_visible(access, meeting):
        return Row(access=access, title=HIDDEN_MEETING, hidden=True, **extra)
    detail = meeting.organizations_label + (" (gemeinsame Sitzung)" if meeting.is_joint else "")
    return Row(access=access, title=meeting.name, detail=detail, url=_meeting_url(access, meeting), **extra)


# =============================================================================
# Kennzahlen je Mandant
# =============================================================================


@dataclass
class TenantFigures:
    """Zählwerte eines Mandanten; Nichtöffentliches zählt mit, erscheint aber nie mit Titel."""

    access: TenantAccess
    papers_review: int = 0
    papers_review_non_public: int = 0
    cosignatures_open: int = 0
    invitation_deadlines: int = 0
    invitation_overdue: int = 0
    paper_deadlines: int = 0
    paper_deadlines_overdue: int = 0
    meetings_upcoming: int = 0
    meetings_year: int = 0
    resolutions_year: int = 0
    resolutions_overdue: int = 0

    COUNTERS = (
        "papers_review",
        "papers_review_non_public",
        "cosignatures_open",
        "invitation_deadlines",
        "invitation_overdue",
        "paper_deadlines",
        "paper_deadlines_overdue",
        "meetings_upcoming",
        "meetings_year",
        "resolutions_year",
        "resolutions_overdue",
    )

    @property
    def tenant(self) -> SessionTenant:
        return self.access.tenant


@dataclass
class Overview:
    """Alles, was die Übersichtsseite zeigt."""

    group: SessionTenantGroup
    membership: SessionTenantGroupMembership
    accesses: list[TenantAccess]
    figures: list[TenantFigures]
    totals: dict[str, int]
    year: int
    review_papers: list[Row] = field(default_factory=list)
    cosignatures: list[Row] = field(default_factory=list)
    invitation_deadlines: list[Row] = field(default_factory=list)
    paper_deadlines: list[Row] = field(default_factory=list)
    meetings: list[Row] = field(default_factory=list)


def _count_rows(figures: dict[Any, TenantFigures], rows: Any, key: str, mapping: dict[str, str]) -> None:
    for row in rows:
        entry = figures.get(row[key])
        if entry is None:
            continue
        for column, attr in mapping.items():
            setattr(entry, attr, row[column] or 0)


def _invitation_candidates(tenant_ids: list[Any], now: datetime, horizon: date) -> list[SessionMeeting]:
    """
    Sitzungen ohne versandte Ladung, deren Ladungsfrist verstrichen ist oder bis ``horizon`` abläuft.

    Die Frist hängt vom Gremium ab (bei gemeinsamen Sitzungen die längste): Vorgefiltert wird über die
    längste Ladungsfrist aller Gremien der Gruppe, genau berechnet danach in Python.
    """
    longest = SessionOrganization.objects.filter(tenant_id__in=tenant_ids).aggregate(m=Max("invitation_period_days"))
    latest = now + timedelta(days=DEADLINE_DAYS + int(longest["m"] or 0) + 1)
    meetings = list(
        SessionMeeting.with_joint_flag(
            SessionMeeting.objects.filter(
                tenant_id__in=tenant_ids,
                start__gte=now,
                start__lte=latest,
                cancelled=False,
                invitation_sent_at__isnull=True,
                meeting_state__in=("draft", "scheduled"),
            )
            .select_related("organization")
            .defer(*_deferred(SessionMeeting))
        ).order_by("start")[:INVITATION_CANDIDATES]
    )
    joint_meeting_service.prefetch_joint(meetings)
    return [meeting for meeting in meetings if meeting.invitation_deadline <= horizon]


def _figures(
    accesses: list[TenantAccess], invitation_due: list[SessionMeeting], *, today: date, now: datetime
) -> dict[Any, TenantFigures]:
    """Kennzahlen aller Mandanten in je einer gruppierten Abfrage."""
    ids = [access.tenant.pk for access in accesses]
    figures = {access.tenant.pk: TenantFigures(access=access) for access in accesses}
    horizon = today + timedelta(days=DEADLINE_DAYS)
    year = today.year

    papers = (
        SessionPaper.objects.filter(tenant_id__in=ids)
        .values("tenant_id")
        .annotate(
            review=Count("id", filter=Q(status="review")),
            review_np=Count("id", filter=Q(status="review", is_public=False)),
            deadlines=Count("id", filter=Q(status__in=OPEN_PAPER_STATUSES, deadline__lte=horizon)),
            deadlines_overdue=Count("id", filter=Q(status__in=OPEN_PAPER_STATUSES, deadline__lt=today)),
        )
        .order_by()
    )
    _count_rows(
        figures,
        papers,
        "tenant_id",
        {
            "review": "papers_review",
            "review_np": "papers_review_non_public",
            "deadlines": "paper_deadlines",
            "deadlines_overdue": "paper_deadlines_overdue",
        },
    )
    cosignatures = (
        SessionCosignature.objects.filter(paper__tenant_id__in=ids, paper__status="review", status="pending")
        .values("paper__tenant_id")
        .annotate(open=Count("id"))
        .order_by()
    )
    _count_rows(figures, cosignatures, "paper__tenant_id", {"open": "cosignatures_open"})
    meetings = (
        SessionMeeting.objects.filter(tenant_id__in=ids, cancelled=False)
        .values("tenant_id")
        .annotate(
            upcoming=Count("id", filter=Q(start__gte=now, start__lt=now + timedelta(days=MEETING_DAYS))),
            year=Count("id", filter=Q(start__year=year)),
        )
        .order_by()
    )
    _count_rows(figures, meetings, "tenant_id", {"upcoming": "meetings_upcoming", "year": "meetings_year"})
    items = (
        SessionAgendaItem.objects.filter(meeting__tenant_id__in=ids)
        .values("meeting__tenant_id")
        .annotate(
            decided=Count("id", filter=Q(vote_result__in=DECIDED_RESULTS, meeting__start__year=year)),
            overdue=Count(
                "id",
                filter=Q(vote_result="approved", implementation_deadline__lt=today) & ~Q(implementation_status="done"),
            ),
        )
        .order_by()
    )
    _count_rows(figures, items, "meeting__tenant_id", {"decided": "resolutions_year", "overdue": "resolutions_overdue"})
    for meeting in invitation_due:
        entry = figures[meeting.tenant_id]
        entry.invitation_deadlines += 1
        if meeting.invitation_overdue:
            entry.invitation_overdue += 1
    return figures


# =============================================================================
# Übersicht
# =============================================================================


def build_overview(membership: SessionTenantGroupMembership, user: Any) -> Overview:
    """Kennzahlen je Mandant und – für die Gruppenrolle Leitstelle – die Arbeitsvorräte aller Mandanten."""
    group = membership.group
    accesses = tenant_accesses(group, user)
    by_id = {access.tenant.pk: access for access in accesses}
    ids = list(by_id)
    today = timezone.localdate()
    now = timezone.now()
    horizon = today + timedelta(days=DEADLINE_DAYS)

    invitation_due = _invitation_candidates(ids, now, horizon) if ids else []
    figures = _figures(accesses, invitation_due, today=today, now=now)
    totals = {name: sum(getattr(entry, name) for entry in figures.values()) for name in TenantFigures.COUNTERS}
    overview = Overview(
        group=group,
        membership=membership,
        accesses=accesses,
        figures=[figures[access.tenant.pk] for access in accesses],
        totals=totals,
        year=today.year,
    )
    if not ids or not membership.sees_worklists:
        return overview

    papers = (
        SessionPaper.objects.filter(tenant_id__in=ids)
        .select_related("main_organization")
        .defer(*_deferred(SessionPaper))
    )
    overview.review_papers = [
        _paper_row(by_id[paper.tenant_id], paper, when=paper.created_at, due=paper.deadline)
        for paper in papers.filter(status="review").order_by("created_at")[:LIST_LIMIT]
    ]
    overview.paper_deadlines = [
        _paper_row(
            by_id[paper.tenant_id], paper, due=paper.deadline, overdue=bool(paper.deadline and paper.deadline < today)
        )
        for paper in papers.filter(status__in=OPEN_PAPER_STATUSES, deadline__lte=horizon).order_by("deadline")[
            :LIST_LIMIT
        ]
    ]
    cosignatures = (
        SessionCosignature.objects.filter(paper__tenant_id__in=ids, paper__status="review", status="pending")
        .select_related("paper__main_organization", "department")
        .defer(*(f"paper__{name}" for name in _deferred(SessionPaper)))
        .order_by("paper__created_at", "order")[:LIST_LIMIT]
    )
    for cosignature in cosignatures:
        paper = cosignature.paper
        access = by_id[paper.tenant_id]
        row = _paper_row(access, paper, when=cosignature.created_at)
        if not row.hidden:
            row.detail = f"Mitzeichnung: {cosignature.department.name}"
        overview.cosignatures.append(row)
    overview.invitation_deadlines = [
        _meeting_row(
            by_id[meeting.tenant_id],
            meeting,
            when=meeting.start,
            due=meeting.invitation_deadline,
            overdue=meeting.invitation_overdue,
        )
        for meeting in sorted(invitation_due, key=lambda m: m.invitation_deadline)[:LIST_LIMIT]
    ]
    upcoming = list(
        SessionMeeting.with_joint_flag(
            SessionMeeting.objects.filter(
                tenant_id__in=ids, cancelled=False, start__gte=now, start__lt=now + timedelta(days=MEETING_DAYS)
            )
            .select_related("organization")
            .defer(*_deferred(SessionMeeting))
        ).order_by("start")[:LIST_LIMIT]
    )
    joint_meeting_service.prefetch_joint(upcoming)
    overview.meetings = [_meeting_row(by_id[m.tenant_id], m, when=m.start) for m in upcoming]
    return overview


# =============================================================================
# Suche über Titel und Nummern
# =============================================================================


@dataclass
class SearchResult:
    query: str
    accesses: list[TenantAccess]
    papers: list[Row] = field(default_factory=list)
    meetings: list[Row] = field(default_factory=list)
    items: list[Row] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.papers) + len(self.meetings) + len(self.items)

    def hits_by_tenant(self) -> dict[Any, int]:
        hits: dict[Any, int] = {access.tenant.pk: 0 for access in self.accesses}
        for row in (*self.papers, *self.meetings, *self.items):
            hits[row.tenant.pk] = hits.get(row.tenant.pk, 0) + 1
        return hits


def normalize_query(raw: str) -> str:
    """Suchbegriff bereinigen; zu kurz ergibt eine leere Zeichenkette (keine Suche)."""
    query = " ".join(str(raw or "").split())[:SEARCH_MAX_LENGTH]
    return query if len(query) >= SEARCH_MIN_LENGTH else ""


def search(membership: SessionTenantGroupMembership, user: Any, query: str) -> SearchResult:
    """
    Einfache Datenbanksuche über Betreff und Nummer (Vorlagen), Name (Sitzungen) sowie Betreff und
    Beschlussnummer (TOPs) aller Mandanten der Gruppe, je Trefferart begrenzt.

    Nichtöffentliches erscheint nur aus Mandanten, in denen die Person über ihre eigene Mitgliedschaft
    das NÖ-Sichtrecht hat – andernfalls wird es gar nicht erst abgefragt.
    """
    accesses = tenant_accesses(membership.group, user)
    result = SearchResult(query=query, accesses=accesses)
    if not query or not accesses:
        return result
    by_id = {access.tenant.pk: access for access in accesses}
    ids = list(by_id)
    np_papers = [access.tenant.pk for access in accesses if access.sees_non_public_papers]
    np_meetings = [access.tenant.pk for access in accesses if access.sees_non_public_meetings]

    papers = (
        SessionPaper.objects.filter(tenant_id__in=ids)
        .filter(Q(name__icontains=query) | Q(reference__icontains=query))
        .filter(Q(is_public=True) | Q(tenant_id__in=np_papers))
        .select_related("main_organization")
        .defer(*_deferred(SessionPaper))
        .order_by("-created_at")[:SEARCH_LIMIT]
    )
    result.papers = [_paper_row(by_id[p.tenant_id], p, when=p.date or p.created_at) for p in papers]

    meetings = list(
        SessionMeeting.with_joint_flag(
            SessionMeeting.objects.filter(tenant_id__in=ids, name__icontains=query)
            .filter(Q(is_public=True) | Q(tenant_id__in=np_meetings))
            .select_related("organization")
            .defer(*_deferred(SessionMeeting))
        ).order_by("-start")[:SEARCH_LIMIT]
    )
    joint_meeting_service.prefetch_joint(meetings)
    result.meetings = [_meeting_row(by_id[m.tenant_id], m, when=m.start) for m in meetings]

    items = (
        SessionAgendaItem.objects.filter(meeting__tenant_id__in=ids)
        .filter(Q(name__icontains=query) | Q(resolution_number__icontains=query))
        .filter(Q(is_public=True, meeting__is_public=True) | Q(meeting__tenant_id__in=np_meetings))
        .select_related("meeting__organization")
        .defer(*_deferred(SessionAgendaItem), *(f"meeting__{name}" for name in _deferred(SessionMeeting)))
        .order_by("-meeting__start", "order")[:SEARCH_LIMIT]
    )
    for item in items:
        meeting = item.meeting
        access = by_id[meeting.tenant_id]
        visible = (item.is_public and meeting.is_public) or access.sees_non_public_meetings
        result.items.append(
            Row(
                access=access,
                title=f"TOP {item.number}: {item.name}",
                reference=item.resolution_number,
                detail=f"{meeting.name} · {meeting.organization.name}",
                when=meeting.start,
                url=_meeting_url(access, meeting) if visible else None,
            )
        )
    return result


# =============================================================================
# Protokoll (Lesezugriff je Mandant)
# =============================================================================


def log_view(
    request: Any,
    membership: SessionTenantGroupMembership,
    accesses: list[TenantAccess],
    *,
    view: str,
    hits: dict[Any, int] | None = None,
) -> int:
    """
    Nutzung der Leitstelle im Protokoll jedes Mandanten der Gruppe festhalten (zusammengefasst).

    Returns: Zahl der neu geschriebenen Einträge (zusammengefasste Aufrufe schreiben keinen).
    """
    group = membership.group
    written = 0
    for access in accesses:
        changes: dict[str, Any] = {
            "leitstelle": group.name,
            "ansicht": "Suche" if view == VIEW_SEARCH else "Übersicht",
            "gruppenrolle": membership.get_role_display(),
            "mitglied_im_mandanten": access.is_member,
            "nichtoeffentliche_titel": access.sees_non_public_papers or access.sees_non_public_meetings,
        }
        if not access.is_member:
            # Ohne Mitgliedschaft fehlt der Nutzerbezug im Eintrag – das Konto steht deshalb hier
            changes["konto"] = getattr(request.user, "email", "")
        if hits is not None:
            changes["treffer"] = hits.get(access.tenant.pk, 0)
        entry = audit.log_read(
            request,
            group,
            tenant=access.tenant,
            user=access.session_user,
            changes=changes,
            respect_setting=False,
            dedup_suffix=view,
        )
        if entry is not None:
            written += 1
    return written


# =============================================================================
# Nachvollziehbarkeit der Leitstellen-Rechte (Signale)
# =============================================================================


def _changed_by() -> str:
    get_request: Any = audit.get_current_request
    user = getattr(get_request(), "user", None)
    return str(getattr(user, "email", "") or "") if getattr(user, "is_authenticated", False) else ""


def _log_rights(group: SessionTenantGroup, instance: Any, changes: dict[str, Any], tenants: Any = None) -> None:
    """Änderung der Leitstellen-Rechte im Protokoll jedes betroffenen Mandanten („Rechte geändert“)."""
    if tenants is None:
        tenants = SessionTenant.objects.filter(group_link__group=group)
    changed_by = _changed_by()
    if changed_by:
        changes = {**changes, "geaendert_von": changed_by}
    for tenant in tenants:
        audit.log_event("permissions_changed", instance, tenant=tenant, changes={"leitstelle": group.name, **changes})


def membership_saved(sender: Any, instance: SessionTenantGroupMembership, created: bool, **kwargs: Any) -> None:
    """Leitstellen-Mitgliedschaft erteilt oder geändert."""
    if kwargs.get("raw"):
        return
    _log_rights(
        instance.group,
        instance,
        {
            "vorgang": "Leitstellen-Zugang erteilt" if created else "Leitstellen-Zugang geändert",
            "konto": instance.user.email,
            "gruppenrolle": instance.get_role_display(),
            "aktiv": instance.is_active,
        },
    )


def membership_deleted(sender: Any, instance: SessionTenantGroupMembership, **kwargs: Any) -> None:
    """Leitstellen-Mitgliedschaft entzogen."""
    group = SessionTenantGroup.objects.filter(pk=instance.group_id).first()
    if group is None:
        return
    email = get_user_model().objects.filter(pk=instance.user_id).values_list("email", flat=True).first()
    _log_rights(group, instance, {"vorgang": "Leitstellen-Zugang entzogen", "konto": email or ""})


def group_tenant_saved(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    """Mandant einer Gruppe zugeordnet: Der Mandant sieht, welche Konten nun seine Übersicht sehen."""
    if kwargs.get("raw") or not created:
        return
    members = sorted(instance.group.memberships.filter(is_active=True).values_list("user__email", flat=True))
    _log_rights(
        instance.group,
        instance,
        {"vorgang": "Mandant der Leitstelle zugeordnet", "leitstelle_konten": members},
        tenants=[instance.tenant],
    )


def group_tenant_deleted(sender: Any, instance: Any, **kwargs: Any) -> None:
    """Mandant aus einer Gruppe entfernt."""
    group = SessionTenantGroup.objects.filter(pk=instance.group_id).first()
    tenant = SessionTenant.objects.filter(pk=instance.tenant_id).first()
    if group is None or tenant is None:
        return
    _log_rights(group, instance, {"vorgang": "Mandant aus der Leitstelle entfernt"}, tenants=[tenant])


def group_pre_delete(sender: Any, instance: SessionTenantGroup, **kwargs: Any) -> None:
    """Gruppe wird gelöscht: Die Mandanten erfahren es, bevor ihre Zuordnung verschwindet."""
    _log_rights(instance, instance, {"vorgang": "Leitstelle aufgelöst"})
