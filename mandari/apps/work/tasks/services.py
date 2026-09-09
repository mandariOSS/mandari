# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schreibende Anwendungsfälle für das Aufgaben-Modul (Issue #160, Service-Layer).

Views parsen die Anfrage, prüfen Berechtigungen und rufen hier hinein; die
Funktionen kapseln Statuswechsel, Positionen, Aktivitätsprotokoll und
Benachrichtigungen. Pfade mit mehreren Schreibzugriffen laufen in
``transaction.atomic``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from apps.tenants.models import Membership, Organization

from . import selectors
from .activity import log_activity, log_field_change
from .models import Task, TaskActivity, TaskAttachment, TaskChecklistItem, TaskLabel, TaskShare

logger = logging.getLogger(__name__)

VALID_STATUSES = ("todo", "in_progress", "done")
VALID_VISIBILITIES = ("private", "shared", "organization")


class InvalidTaskStatusError(ValueError):
    """Unbekannter Spaltenstatus."""


def _hub() -> Any:
    """NotificationHub spät importieren (Zyklus work.notifications → work.tasks) und untypisiert durchreichen."""
    from apps.work.notifications.services import NotificationHub

    return NotificationHub


def can_edit_task(task: Task, membership: Membership) -> bool:
    """Ersteller, Zugewiesene und Mitglieder mit ``tasks.manage`` dürfen bearbeiten."""
    return task.created_by == membership or task.assigned_to == membership or membership.has_permission("tasks.manage")


def _sync_completion(task: Task, *, previous_status: str | None = None) -> None:
    """Hält ``is_completed``/``completed_at`` konsistent zum Status."""
    if previous_status is None:
        if task.status == "done" and not task.is_completed:
            task.is_completed = True
            task.completed_at = timezone.now()
        elif task.status != "done" and task.is_completed:
            task.is_completed = False
            task.completed_at = None
        return
    if task.status == "done" and previous_status != "done":
        task.is_completed = True
        task.completed_at = timezone.now()
    elif task.status != "done" and previous_status == "done":
        task.is_completed = False
        task.completed_at = None


# ---------------------------------------------------------------------------
# Anlegen, Sichtbarkeit, Labels, Protokoll-Import
# ---------------------------------------------------------------------------


@transaction.atomic
def create_task(
    task: Task, organization: Organization, membership: Membership, *, related_motion_id: str | None = None
) -> Task:
    """
    Speichert eine über das Formular vorbereitete Aufgabe (``form.save(commit=False)``).

    Setzt Organisation, Ersteller, Standard-Zuweisung, optionale Dokument-Verknüpfung und
    die Position am Spaltenende; protokolliert und benachrichtigt bei Fremdzuweisung.
    """
    task.organization = organization
    task.created_by = membership
    if not task.assigned_to:
        task.assigned_to = membership
    if related_motion_id:
        motion = selectors.find_motion(organization, related_motion_id)
        if motion is not None:
            task.related_motion = motion
    task.position = selectors.next_position(organization, task.status)
    task.save()
    log_activity(task, membership, "created")
    if task.assigned_to and task.assigned_to != membership:
        _hub().notify_task_assigned(task, task.assigned_to, membership)
    return task


@transaction.atomic
def update_visibility(task: Task, membership: Membership, visibility: str, share_with_ids: Sequence[str]) -> None:
    """Sichtbarkeit setzen und Freigaben angleichen (nur bei ``shared`` bleiben Freigaben bestehen)."""
    if visibility in VALID_VISIBILITIES:
        task.visibility = visibility
        task.save(update_fields=["visibility"])
    if visibility == "shared":
        TaskShare.objects.filter(task=task).exclude(membership_id__in=share_with_ids).delete()
        for member_id in share_with_ids:
            TaskShare.objects.get_or_create(task=task, membership_id=member_id, defaults={"shared_by": membership})
    else:
        TaskShare.objects.filter(task=task).delete()


def save_label(organization: Organization, label: TaskLabel) -> TaskLabel:
    """Label der Organisation zuordnen und speichern."""
    label.organization = organization
    label.save()
    return label


def delete_label(label: TaskLabel) -> None:
    """Label löschen (M2M-Zuordnungen fallen mit)."""
    label.delete()


@transaction.atomic
def import_protocol_entries(organization: Organization, membership: Membership, entry_ids: Iterable[str]) -> int:
    """Aufgaben aus Protokolleinträgen (Typ ``action``) anlegen; liefert die Anzahl."""
    created = 0
    for entry_id in entry_ids:
        entry = selectors.find_protocol_entry(organization, entry_id, action_only=True)
        if entry is None:
            continue
        task = Task.objects.create(
            organization=organization,
            title=entry.content[:500] if entry.content else "Protokoll-Aufgabe",
            created_by=membership,
            assigned_to=entry.action_assignee or membership,
            due_date=entry.action_due_date,
            status="todo",
            priority="medium",
            position=selectors.next_position(organization, "todo"),
            related_faction_meeting=entry.meeting,
        )
        log_activity(task, membership, "created")
        created += 1
    return created


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------


@transaction.atomic
def quick_add_task(
    organization: Organization, membership: Membership, *, title: str, status: str, priority: str
) -> Task:
    """Schnelleingabe: Aufgabe am Spaltenende anlegen, dem Ersteller zuweisen, protokollieren."""
    task = Task.objects.create(
        organization=organization,
        title=title,
        status=status,
        priority=priority,
        position=selectors.next_position(organization, status),
        created_by=membership,
        assigned_to=membership,
    )
    log_activity(task, membership, "created")
    return task


@transaction.atomic
def move_task(task: Task, membership: Membership, *, new_status: str, new_position: int) -> Task:
    """
    Drag-and-drop: Aufgabe in Spalte/Position verschieben.

    Aktualisiert Erledigt-Flags, protokolliert Statuswechsel, benachrichtigt bei
    Erledigung und sortiert Ziel- und Quellspalte lückenlos nach.
    """
    if new_status not in VALID_STATUSES:
        raise InvalidTaskStatusError(new_status)
    organization = task.organization
    old_status = task.status
    task.status = new_status
    task.position = new_position
    _sync_completion(task, previous_status=old_status)
    task.save()

    if old_status != new_status:
        if new_status == "done":
            log_activity(task, membership, "completed")
            _hub().notify_task_completed(task, membership)
        elif old_status == "done":
            log_activity(task, membership, "reopened")
        else:
            labels = dict(Task.STATUS_CHOICES)
            log_field_change(
                task,
                membership,
                "status",
                labels.get(old_status, old_status),
                labels.get(new_status, new_status),
                "status_changed",
            )

    for idx, other in enumerate(selectors.column_tasks(organization, new_status, exclude_id=task.id)):
        correct_pos = idx if idx < new_position else idx + 1
        if other.position != correct_pos:
            Task.objects.filter(id=other.id).update(position=correct_pos)

    if old_status != new_status:
        for idx, other in enumerate(selectors.column_tasks(organization, old_status)):
            if other.position != idx:
                Task.objects.filter(id=other.id).update(position=idx)
    return task


def set_status(task: Task, new_status: str) -> Task:
    """Status ohne Protokoll setzen (Board-Aktion ``update_status``)."""
    old_status = task.status
    task.status = new_status
    if new_status == "done" and old_status != "done":
        task.is_completed = True
        task.completed_at = timezone.now()
    elif new_status != "done":
        task.is_completed = False
        task.completed_at = None
    task.save()
    return task


@transaction.atomic
def toggle_completion(
    task: Task, membership: Membership, *, reopen_status: str = "in_progress", record_activity: bool = True
) -> Task:
    """
    Erledigt-Status umschalten.

    Board-Karte: ``reopen_status="todo"`` ohne Protokoll; Panel: ``in_progress`` mit
    Protokolleintrag und Benachrichtigung bei Erledigung.
    """
    if task.is_completed:
        task.is_completed = False
        task.completed_at = None
        task.status = reopen_status
        if record_activity:
            log_activity(task, membership, "reopened")
    else:
        task.is_completed = True
        task.completed_at = timezone.now()
        task.status = "done"
        if record_activity:
            log_activity(task, membership, "completed")
    task.save()
    if task.is_completed and record_activity:
        _hub().notify_task_completed(task, membership)
    return task


# ---------------------------------------------------------------------------
# Panel: Formular, Kommentare, Anhänge, Checkliste, Labels
# ---------------------------------------------------------------------------


def capture_old_values(task: Task) -> dict[str, Any]:
    """
    Alte Werte für das Aktivitätsprotokoll festhalten.

    Muss vor ``form.is_valid()`` aufgerufen werden, weil ein ModelForm die Instanz
    bereits bei der Validierung überschreibt.
    """
    return {
        "status": task.get_status_display(),
        "priority": task.get_priority_display(),
        "due_date": str(task.due_date) if task.due_date else "—",
        "assigned_to": task.assigned_to.user.get_display_name() if task.assigned_to else "—",
        "visibility": task.get_visibility_display(),
        "status_raw": task.status,
        "priority_raw": task.priority,
        "due_date_raw": task.due_date,
        "assigned_to_raw": task.assigned_to_id,
        "visibility_raw": task.visibility,
    }


def _log_changes(task: Task, membership: Membership, old: dict[str, Any]) -> None:
    """Feldänderungen protokollieren und Betroffene benachrichtigen."""
    hub = _hub()
    changes: list[tuple[str, str, str, str]] = []
    if old["status_raw"] != task.status:
        if task.status == "done":
            log_activity(task, membership, "completed")
            hub.notify_task_completed(task, membership)
        elif old["status_raw"] == "done":
            log_activity(task, membership, "reopened")
        else:
            changes.append(("status", old["status"], task.get_status_display(), "status_changed"))

    assignee_changed = old["assigned_to_raw"] != task.assigned_to_id
    if assignee_changed and task.assigned_to and task.assigned_to != membership:
        hub.notify_task_assigned(task, task.assigned_to, membership)
    if old["priority_raw"] != task.priority:
        changes.append(("priority", old["priority"], task.get_priority_display(), "priority_changed"))
    if old["due_date_raw"] != task.due_date:
        changes.append(("due_date", old["due_date"], str(task.due_date) if task.due_date else "—", "due_date_changed"))
    if assignee_changed:
        new_assigned = task.assigned_to.user.get_display_name() if task.assigned_to else "—"
        changes.append(("assigned_to", old["assigned_to"], new_assigned, "assigned"))
    if old["visibility_raw"] != task.visibility:
        changes.append(("visibility", old["visibility"], task.get_visibility_display(), "visibility_changed"))

    for field_name, old_val, new_val, activity_type in changes:
        log_field_change(task, membership, field_name, old_val, new_val, activity_type)


@transaction.atomic
def apply_panel_update(task: Task, membership: Membership, old_values: dict[str, Any]) -> Task:
    """
    Vom Panel-Formular geänderte Aufgabe (``form.save(commit=False)``) speichern.

    Gleicht die Erledigt-Flags an den Status an, protokolliert Feldänderungen und
    benachrichtigt Betroffene. ``old_values`` stammt aus :func:`capture_old_values`.
    """
    _sync_completion(task)
    task.save()
    _log_changes(task, membership, old_values)
    return task


def add_comment(task: Task, membership: Membership, content: str) -> TaskActivity:
    """Kommentar als Aktivität anlegen und Beteiligte benachrichtigen."""
    activity = log_activity(task, membership, "comment", content=content)
    _hub().notify_task_comment(task, activity, membership)
    return activity


def delete_task(task: Task) -> None:
    """Aufgabe mit allen abhängigen Objekten löschen."""
    task.delete()


@transaction.atomic
def add_attachment(
    task: Task, membership: Membership, attachment: TaskAttachment, uploaded: UploadedFile[Any]
) -> TaskAttachment:
    """Anhang (aus ``form.save(commit=False)``) mit Metadaten speichern und protokollieren."""
    import mimetypes

    attachment.task = task
    attachment.uploaded_by = membership
    attachment.filename = uploaded.name or ""
    attachment.mime_type = (
        uploaded.content_type or mimetypes.guess_type(uploaded.name or "")[0] or "application/octet-stream"
    )
    attachment.file_size = uploaded.size or 0
    attachment.save()
    log_activity(task, membership, "attachment_added", details={"filename": uploaded.name})
    return attachment


@transaction.atomic
def remove_attachment(task: Task, membership: Membership, attachment: TaskAttachment) -> str:
    """Anhang samt Datei löschen; liefert den Dateinamen für die Rückmeldung."""
    filename = attachment.filename
    attachment.file.delete(save=False)
    attachment.delete()
    log_activity(task, membership, "attachment_removed", details={"filename": filename})
    return filename


@transaction.atomic
def add_checklist_item(task: Task, membership: Membership, item: TaskChecklistItem) -> TaskChecklistItem:
    """Checklistenpunkt (aus ``form.save(commit=False)``) am Ende anhängen und protokollieren."""
    item.task = task
    item.position = task.checklist_items.count()
    item.save()
    log_activity(task, membership, "checklist_item_added", details={"title": item.title})
    return item


@transaction.atomic
def toggle_checklist_item(task: Task, membership: Membership, item: TaskChecklistItem) -> TaskChecklistItem:
    """Checklistenpunkt abhaken bzw. wieder öffnen und protokollieren."""
    item.is_completed = not item.is_completed
    item.save(update_fields=["is_completed"])
    activity_type = "checklist_item_completed" if item.is_completed else "checklist_item_unchecked"
    log_activity(task, membership, activity_type, details={"title": item.title})
    return item


def delete_checklist_item(item: TaskChecklistItem) -> None:
    """Checklistenpunkt löschen."""
    item.delete()


@transaction.atomic
def reorder_checklist(task: Task, order: Sequence[Any]) -> None:
    """Checklisten-Reihenfolge übernehmen (nur Punkte dieser Aufgabe)."""
    for idx, item_id in enumerate(order):
        TaskChecklistItem.objects.filter(id=item_id, task=task).update(position=idx)


@transaction.atomic
def toggle_label(task: Task, membership: Membership, label: TaskLabel) -> bool:
    """Label an-/abwählen; liefert ``True``, wenn es hinzugefügt wurde."""
    if selectors.has_label(task, label.id):
        task.labels.remove(label)
        log_activity(task, membership, "label_removed", details={"label": label.name})
        return False
    task.labels.add(label)
    log_activity(task, membership, "label_added", details={"label": label.name})
    return True
