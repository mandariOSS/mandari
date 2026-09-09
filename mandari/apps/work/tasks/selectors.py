# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lesende Zugriffe für das Aufgaben-Modul (Issue #160, Service-Layer).

Alle Querysets sind an eine Organisation gebunden; Sichtbarkeitsregeln
(privat/geteilt/organisationsweit) werden hier zentral angewendet, damit
Board, Panel, Export und Import dieselbe Grenze ziehen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db.models import Prefetch, Q, QuerySet
from django.utils import timezone

from apps.tenants.models import Membership, Organization

from .models import Task, TaskActivity, TaskLabel, TaskShare

if TYPE_CHECKING:
    from apps.work.faction.models import FactionProtocolEntry
    from apps.work.motions.models import Motion

OPEN_STATUSES = ("todo", "in_progress")
STATUSES = ("todo", "in_progress", "done")


def _base_queryset() -> QuerySet[Task]:
    """Aufgaben mit den Relationen, die Karte und Panel immer brauchen (noch ohne Organisationsfilter)."""
    return Task.objects.select_related(
        "assigned_to__user",
        "created_by__user",
        "related_meeting",
        "related_motion",
        "related_faction_meeting",
    )


def _with_card_relations(qs: QuerySet[Task]) -> QuerySet[Task]:
    return qs.prefetch_related("labels", "checklist_items", "attachments")


def _with_panel_relations(qs: QuerySet[Task]) -> QuerySet[Task]:
    return _with_card_relations(qs).prefetch_related(
        Prefetch(
            "activities",
            queryset=TaskActivity.objects.select_related("actor__user").order_by("created_at"),
        )
    )


def tasks_for_organization(organization: Organization) -> QuerySet[Task]:
    """Alle Aufgaben der Organisation mit Standard-Relationen."""
    return _base_queryset().filter(organization=organization)


def card_queryset(organization: Organization) -> QuerySet[Task]:
    """Aufgaben mit allem, was die Kanban-Karte rendert (Labels, Checkliste, Anhänge)."""
    return _with_card_relations(tasks_for_organization(organization))


def panel_queryset(organization: Organization) -> QuerySet[Task]:
    """Aufgaben mit allen Relationen für das Slide-over-Panel inkl. Aktivitätsverlauf."""
    return _with_panel_relations(tasks_for_organization(organization))


def reload_for_card(task: Task) -> Task:
    """Lädt eine Aufgabe mit Karten-Relationen neu (nach Schreibzugriffen)."""
    return _with_card_relations(_base_queryset().filter(organization_id=task.organization_id)).get(id=task.id)


def reload_for_panel(task: Task) -> Task:
    """Lädt eine Aufgabe mit Panel-Relationen neu (nach Schreibzugriffen)."""
    return _with_panel_relations(_base_queryset().filter(organization_id=task.organization_id)).get(id=task.id)


def find_task(organization: Organization, task_id: Any) -> Task | None:
    """Aufgabe per ID innerhalb der Organisation, sonst ``None``."""
    return Task.objects.filter(id=task_id, organization=organization).first()


def visible_tasks(
    organization: Organization, membership: Membership, *, base: QuerySet[Task] | None = None
) -> QuerySet[Task]:
    """
    Alle Aufgaben der Organisation, die das Mitglied sehen darf.

    Sichtbar sind organisationsweite Aufgaben sowie eigene, zugewiesene und
    explizit geteilte Aufgaben.
    """
    qs = base if base is not None else Task.objects.filter(organization=organization)
    return qs.filter(
        Q(visibility="organization")
        | Q(created_by=membership)
        | Q(assigned_to=membership)
        | Q(shares__membership=membership)
    ).distinct()


def own_tasks(membership: Membership, *, base: QuerySet[Task]) -> QuerySet[Task]:
    """Ansicht "Meine Aufgaben": erstellt, zugewiesen oder mit dem Mitglied geteilt."""
    return base.filter(
        Q(assigned_to=membership) | Q(created_by=membership) | Q(shares__membership=membership)
    ).distinct()


def apply_board_filters(
    tasks: QuerySet[Task],
    *,
    search: str = "",
    priority: str = "",
    label_id: str = "",
    assignee_id: str = "",
    overdue_only: bool = False,
) -> QuerySet[Task]:
    """Such- und Detailfilter des Boards auf ein Queryset anwenden."""
    if search:
        tasks = tasks.filter(Q(title__icontains=search) | Q(description__icontains=search))
    if priority:
        tasks = tasks.filter(priority=priority)
    if label_id:
        tasks = tasks.filter(labels__id=label_id)
    if assignee_id:
        tasks = tasks.filter(assigned_to__id=assignee_id)
    if overdue_only:
        tasks = tasks.filter(due_date__lt=timezone.now().date(), status__in=OPEN_STATUSES)
    return tasks


def board_columns(tasks: QuerySet[Task], *, show_completed: bool) -> dict[str, QuerySet[Task]]:
    """Aufgaben nach Kanban-Spalten gruppiert; erledigte Aufgaben begrenzt."""
    done = tasks.filter(status="done")
    return {
        "todo": tasks.filter(status="todo").order_by("position", "-priority", "due_date"),
        "in_progress": tasks.filter(status="in_progress").order_by("position", "-priority", "due_date"),
        "done": done.order_by("-completed_at", "position")[:50]
        if show_completed
        else done.order_by("-completed_at")[:10],
    }


def board_stats(tasks: QuerySet[Task]) -> dict[str, int]:
    """Kennzahlen für die Kacheln über dem Board (auf sichtbarkeitsgefilterter Basis, Issue #6)."""
    return {
        "total": tasks.count(),
        "todo": tasks.filter(status="todo").count(),
        "in_progress": tasks.filter(status="in_progress").count(),
        "done": tasks.filter(status="done").count(),
        "overdue": tasks.filter(due_date__lt=timezone.now().date(), status__in=OPEN_STATUSES).count(),
    }


def column_counts(organization: Organization) -> dict[str, int]:
    """Anzahl der Aufgaben je Spalte (für die OOB-Zähler nach Schreibzugriffen)."""
    qs = Task.objects.filter(organization=organization)
    return {
        "todo_count": qs.filter(status="todo").count(),
        "in_progress_count": qs.filter(status="in_progress").count(),
        "done_count": qs.filter(status="done").count(),
    }


def next_position(organization: Organization, status: str) -> int:
    """Nächste freie Position am Ende einer Spalte."""
    return Task.objects.filter(organization=organization, status=status).count()


def position_counters(organization: Organization) -> dict[str, int]:
    """Nächste freie Position je Spalte (für Massen-Import)."""
    return {status: next_position(organization, status) for status in STATUSES}


def column_tasks(organization: Organization, status: str, *, exclude_id: Any = None) -> list[Task]:
    """Aufgaben einer Spalte in Positionsreihenfolge (für das Nachsortieren beim Verschieben)."""
    qs = Task.objects.filter(organization=organization, status=status)
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    return list(qs.order_by("position"))


def labels_for_organization(organization: Organization) -> QuerySet[TaskLabel]:
    """Alle Labels der Organisation."""
    return TaskLabel.objects.filter(organization=organization)


def label_rows(organization: Organization) -> list[dict[str, Any]]:
    """Labels als flache Dicts (JSON-Antwort der Label-Verwaltung)."""
    return [dict(row) for row in labels_for_organization(organization).values("id", "name", "color")]


def labels_by_name(organization: Organization) -> dict[str, TaskLabel]:
    """Labels der Organisation, indiziert nach kleingeschriebenem Namen."""
    return {label.name.lower(): label for label in labels_for_organization(organization)}


def active_members(organization: Organization) -> QuerySet[Membership]:
    """Aktive Mitglieder der Organisation (Zuweisungsliste)."""
    return organization.memberships.filter(is_active=True).select_related("user")


def memberships_by_email(organization: Organization) -> dict[str, Membership]:
    """Aktive Mitglieder, indiziert nach kleingeschriebener E-Mail (Import-Zuordnung)."""
    return {ms.user.email.lower(): ms for ms in active_members(organization)}


def visible_task_titles(organization: Organization, membership: Membership) -> set[str]:
    """Normalisierte Titel aller sichtbaren Aufgaben (Duplikat-Regel beim Import)."""
    return {
        str(title).strip().lower() for title in visible_tasks(organization, membership).values_list("title", flat=True)
    }


def task_shares(task: Task) -> QuerySet[TaskShare]:
    """Freigaben einer Aufgabe inkl. Mitglied."""
    return TaskShare.objects.filter(task=task).select_related("membership__user")


def task_label_ids(task: Task) -> list[Any]:
    """IDs der Labels einer Aufgabe."""
    return list(task.labels.values_list("id", flat=True))


def task_activities(task: Task) -> QuerySet[TaskActivity]:
    """Aktivitätsverlauf einer Aufgabe, chronologisch."""
    return task.activities.select_related("actor__user").order_by("created_at")


def has_label(task: Task, label_id: Any) -> bool:
    """Ob ein Label bereits an der Aufgabe hängt."""
    return task.labels.filter(id=label_id).exists()


def export_queryset(organization: Organization, membership: Membership) -> QuerySet[Task]:
    """Sichtbare Aufgaben mit allen Relationen für den Export, in Board-Reihenfolge."""
    return (
        visible_tasks(organization, membership)
        .select_related("assigned_to__user", "created_by__user")
        .prefetch_related("labels", "checklist_items")
        .order_by("status", "position", "created_at")
    )


def find_motion(organization: Organization, motion_id: Any) -> Motion | None:
    """Dokument der Organisation per ID (Prefill "Aufgabe aus Dokument"), sonst ``None``."""
    from django.core.exceptions import ValidationError

    from apps.work.motions.models import Motion

    try:
        return Motion.objects.get(id=motion_id, organization=organization)
    except (Motion.DoesNotExist, ValueError, ValidationError):
        return None


def find_protocol_entry(
    organization: Organization, entry_id: Any, *, action_only: bool = False
) -> FactionProtocolEntry | None:
    """Protokolleintrag einer Fraktionssitzung der Organisation, sonst ``None``."""
    from django.core.exceptions import ValidationError

    from apps.work.faction.models import FactionProtocolEntry

    filters: dict[str, Any] = {"id": entry_id, "meeting__organization": organization}
    if action_only:
        filters["entry_type"] = "action"
    try:
        return FactionProtocolEntry.objects.get(**filters)
    except (FactionProtocolEntry.DoesNotExist, ValueError, ValidationError):
        return None


def open_protocol_action_items(organization: Organization, *, limit: int = 50) -> QuerySet[FactionProtocolEntry]:
    """Offene Aufgaben-Einträge aus Fraktionsprotokollen, die noch nicht importiert wurden."""
    from apps.work.faction.models import FactionProtocolEntry

    imported_meetings = Task.objects.filter(organization=organization).values_list("related_faction_meeting", flat=True)
    return (
        FactionProtocolEntry.objects.filter(
            meeting__organization=organization,
            entry_type="action",
            action_completed=False,
        )
        .exclude(id__in=imported_meetings)
        .select_related("meeting", "agenda_item", "action_assignee__user")
        .order_by("-created_at")[:limit]
    )
