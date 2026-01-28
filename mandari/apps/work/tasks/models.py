# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Task/To-Do models for the Work module.

Simple task management with:
- Personal and organization tasks
- Priority levels
- Due dates
- Links to meetings, motions, etc.
"""

import uuid

from django.db import models


class Task(models.Model):
    """
    Task or to-do item.

    Can be personal (assigned_to = creator) or organizational (assigned to another member).
    Supports Kanban-style workflow with status columns.
    """

    PRIORITY_CHOICES = [
        ("urgent", "Dringend"),
        ("high", "Hoch"),
        ("medium", "Mittel"),
        ("low", "Niedrig"),
    ]

    STATUS_CHOICES = [
        ("todo", "Zu erledigen"),
        ("in_progress", "In Bearbeitung"),
        ("done", "Erledigt"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="tasks",
        verbose_name="Organisation"
    )

    # Task info
    title = models.CharField(max_length=200, verbose_name="Titel")
    description = models.TextField(blank=True, max_length=2000, verbose_name="Beschreibung")

    # Priority and status
    priority = models.CharField(
        max_length=10,
        choices=PRIORITY_CHOICES,
        default="medium",
        verbose_name="Priorität"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="todo",
        verbose_name="Status"
    )
    is_completed = models.BooleanField(
        default=False,
        verbose_name="Erledigt"
    )
    completed_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Erledigt am"
    )
    # Position within a status column for drag & drop ordering
    position = models.PositiveIntegerField(default=0, verbose_name="Position")

    # Timing
    due_date = models.DateField(
        blank=True,
        null=True,
        verbose_name="Fällig am"
    )
    reminder_date = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Erinnerung"
    )

    # Assignment
    created_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="created_tasks",
        verbose_name="Erstellt von"
    )
    assigned_to = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="assigned_tasks",
        verbose_name="Zugewiesen an"
    )

    # Links to other objects
    related_meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_tasks",
        verbose_name="Sitzung"
    )
    related_motion = models.ForeignKey(
        "work.Motion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name="Antrag"
    )
    related_faction_meeting = models.ForeignKey(
        "work.FactionMeeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name="Fraktionssitzung"
    )
    related_agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_tasks",
        verbose_name="Tagesordnungspunkt"
    )

    # Tags
    tags = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Tags"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Aufgabe"
        verbose_name_plural = "Aufgaben"
        ordering = [
            "status",
            "position",
            "-priority",
            "due_date",
            "-created_at"
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "is_completed"]),
            models.Index(fields=["assigned_to", "status"]),
            models.Index(fields=["assigned_to", "is_completed"]),
            models.Index(fields=["due_date"]),
            models.Index(fields=["status", "position"]),
        ]

    def __str__(self):
        return self.title

    @property
    def is_overdue(self) -> bool:
        """Check if task is overdue."""
        if self.is_completed or not self.due_date:
            return False
        from django.utils import timezone
        return self.due_date < timezone.now().date()

    @property
    def is_personal(self) -> bool:
        """Check if this is a personal task (assigned to creator)."""
        return self.assigned_to == self.created_by or self.assigned_to is None


class TaskComment(models.Model):
    """
    Comment on a task.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="comments",
        verbose_name="Aufgabe"
    )

    content = models.TextField(verbose_name="Kommentar")

    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="task_comments",
        verbose_name="Autor"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Aufgabenkommentar"
        verbose_name_plural = "Aufgabenkommentare"
        ordering = ["created_at"]

    def __str__(self):
        preview = self.content[:50] + "..." if len(self.content) > 50 else self.content
        return f"{self.author.user.email}: {preview}"
