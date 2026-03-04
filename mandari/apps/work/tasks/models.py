# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Task/To-Do models for the Work module.

Provides Trello-style task management with:
- Personal and organization tasks
- Priority levels and due dates
- Labels, checklists, attachments
- Activity log (replaces comments)
- Links to meetings, motions, etc.
"""

import uuid

from django.db import models


class TaskLabel(models.Model):
    """Farbiges Label für Aufgaben, pro Organisation."""

    COLOR_CHOICES = [
        ("red", "Rot"),
        ("orange", "Orange"),
        ("amber", "Gelb"),
        ("green", "Grün"),
        ("teal", "Türkis"),
        ("blue", "Blau"),
        ("indigo", "Indigo"),
        ("purple", "Lila"),
        ("pink", "Rosa"),
        ("gray", "Grau"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="task_labels",
        verbose_name="Organisation",
    )
    name = models.CharField(max_length=50, verbose_name="Name")
    color = models.CharField(max_length=20, choices=COLOR_CHOICES, default="blue", verbose_name="Farbe")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Aufgaben-Label"
        verbose_name_plural = "Aufgaben-Labels"
        unique_together = ["organization", "name"]
        ordering = ["name"]

    def __str__(self):
        return self.name


class Task(models.Model):
    """
    Task or to-do item.

    Can be personal (assigned_to = creator) or organizational (assigned to another member).
    Supports Kanban-style workflow with status columns.

    Visibility levels:
    - private: Only the creator can see it
    - shared: Specific people (via TaskShare) can see it
    - organization: Everyone in the organization can see it
    """

    VISIBILITY_CHOICES = [
        ("private", "Privat"),
        ("shared", "Geteilt"),
        ("organization", "Organisation"),
    ]

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
        verbose_name="Organisation",
    )

    # Task info
    title = models.CharField(max_length=200, verbose_name="Titel")
    description = models.TextField(blank=True, max_length=2000, verbose_name="Beschreibung")

    # Visibility
    visibility = models.CharField(
        max_length=20, choices=VISIBILITY_CHOICES, default="private", verbose_name="Sichtbarkeit"
    )

    # Priority and status
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default="medium", verbose_name="Priorität")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="todo", verbose_name="Status")
    is_completed = models.BooleanField(default=False, verbose_name="Erledigt")
    completed_at = models.DateTimeField(blank=True, null=True, verbose_name="Erledigt am")
    # Position within a status column for drag & drop ordering
    position = models.PositiveIntegerField(default=0, verbose_name="Position")

    # Timing
    due_date = models.DateField(blank=True, null=True, verbose_name="Fällig am")
    reminder_date = models.DateTimeField(blank=True, null=True, verbose_name="Erinnerung")

    # Assignment
    created_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="created_tasks",
        verbose_name="Erstellt von",
    )
    assigned_to = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="assigned_tasks",
        verbose_name="Zugewiesen an",
    )

    # Links to other objects
    related_meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_tasks",
        verbose_name="Sitzung",
    )
    related_motion = models.ForeignKey(
        "work.Motion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name="Antrag",
    )
    related_faction_meeting = models.ForeignKey(
        "work.FactionMeeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name="Fraktionssitzung",
    )
    related_agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_tasks",
        verbose_name="Tagesordnungspunkt",
    )
    related_faction_agenda_item = models.ForeignKey(
        "work.FactionAgendaItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name="Fraktions-TOP",
    )

    # Labels (M2M)
    labels = models.ManyToManyField(TaskLabel, blank=True, related_name="tasks", verbose_name="Labels")

    # Tags (legacy, will be migrated to labels)
    tags = models.JSONField(default=list, blank=True, verbose_name="Tags")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Aufgabe"
        verbose_name_plural = "Aufgaben"
        ordering = ["status", "position", "-priority", "due_date", "-created_at"]
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

    @property
    def checklist_progress(self) -> tuple[int, int]:
        """Return (completed, total) for checklist items. Uses prefetch cache if available."""
        try:
            items = self.checklist_items.all()
            total = len(items)
            completed = sum(1 for i in items if i.is_completed)
            return completed, total
        except Exception:
            return 0, 0

    @property
    def attachment_count(self) -> int:
        """Return number of attachments. Uses prefetch cache if available."""
        try:
            return self.attachments.count()
        except Exception:
            return 0

    def can_access(self, membership) -> bool:
        """
        Check if a membership can access this task.

        Access is granted if:
        - User is the creator
        - User is assigned to the task
        - Task visibility is 'organization'
        - Task visibility is 'shared' and user is in shares
        """
        # Creator always has access
        if self.created_by == membership:
            return True

        # Assigned user always has access
        if self.assigned_to == membership:
            return True

        # Check visibility
        if self.visibility == "private":
            return False

        if self.visibility == "organization":
            return membership.organization == self.organization

        if self.visibility == "shared":
            return self.shares.filter(membership=membership).exists()

        return False

    def can_edit(self, membership) -> bool:
        """Check if membership can edit this task."""
        # Creator can always edit
        if self.created_by == membership:
            return True
        # Assigned user can edit
        if self.assigned_to == membership:
            return True
        return False


class TaskShare(models.Model):
    """
    Share a task with a specific person.

    Used when visibility='shared' to define who can see the task.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="shares", verbose_name="Aufgabe")
    membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="shared_tasks",
        verbose_name="Mitglied",
    )
    shared_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="task_shares_given",
        verbose_name="Geteilt von",
    )
    shared_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Aufgaben-Freigabe"
        verbose_name_plural = "Aufgaben-Freigaben"
        unique_together = ["task", "membership"]

    def __str__(self):
        return f"{self.task.title} → {self.membership.user.email}"


class TaskComment(models.Model):
    """
    Comment on a task (legacy, replaced by TaskActivity).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments", verbose_name="Aufgabe")

    content = models.TextField(verbose_name="Kommentar")

    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="task_comments",
        verbose_name="Autor",
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


class TaskChecklistItem(models.Model):
    """Checklisten-Punkt einer Aufgabe."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="checklist_items", verbose_name="Aufgabe")
    title = models.CharField(max_length=300, verbose_name="Titel")
    is_completed = models.BooleanField(default=False, verbose_name="Erledigt")
    position = models.PositiveIntegerField(default=0, verbose_name="Position")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Checklisten-Punkt"
        verbose_name_plural = "Checklisten-Punkte"
        ordering = ["position", "created_at"]

    def __str__(self):
        check = "✓" if self.is_completed else "○"
        return f"{check} {self.title}"


class TaskAttachment(models.Model):
    """Datei-Anhang einer Aufgabe."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attachments", verbose_name="Aufgabe")
    file = models.FileField(upload_to="tasks/attachments/%Y/%m/", verbose_name="Datei")
    filename = models.CharField(max_length=255, verbose_name="Dateiname")
    mime_type = models.CharField(max_length=100, verbose_name="MIME-Typ")
    file_size = models.PositiveIntegerField(default=0, verbose_name="Dateigröße (Bytes)")
    uploaded_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="task_attachments",
        verbose_name="Hochgeladen von",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Aufgaben-Anhang"
        verbose_name_plural = "Aufgaben-Anhänge"
        ordering = ["-created_at"]

    def __str__(self):
        return self.filename

    @property
    def size_human(self) -> str:
        """Menschenlesbare Dateigröße."""
        size = self.file_size
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / (1024 * 1024):.1f} MB"

    @property
    def icon_name(self) -> str:
        """Lucide Icon-Name basierend auf MIME-Typ."""
        if self.mime_type.startswith("image/"):
            return "image"
        elif self.mime_type == "application/pdf":
            return "file-text"
        elif self.mime_type.startswith("video/"):
            return "film"
        elif self.mime_type.startswith("audio/"):
            return "music"
        elif "spreadsheet" in self.mime_type or "excel" in self.mime_type:
            return "table"
        elif "presentation" in self.mime_type or "powerpoint" in self.mime_type:
            return "presentation"
        return "file"


class TaskActivity(models.Model):
    """Aktivitätseintrag für eine Aufgabe (ersetzt TaskComment)."""

    ACTIVITY_TYPES = [
        ("created", "Erstellt"),
        ("status_changed", "Status geändert"),
        ("assigned", "Zugewiesen"),
        ("completed", "Erledigt"),
        ("reopened", "Wieder geöffnet"),
        ("comment", "Kommentar"),
        ("attachment_added", "Anhang hinzugefügt"),
        ("attachment_removed", "Anhang entfernt"),
        ("checklist_item_added", "Checklistenpunkt hinzugefügt"),
        ("checklist_item_completed", "Checklistenpunkt erledigt"),
        ("checklist_item_unchecked", "Checklistenpunkt geöffnet"),
        ("label_added", "Label hinzugefügt"),
        ("label_removed", "Label entfernt"),
        ("priority_changed", "Priorität geändert"),
        ("due_date_changed", "Fälligkeitsdatum geändert"),
        ("visibility_changed", "Sichtbarkeit geändert"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="activities", verbose_name="Aufgabe")
    actor = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="task_activities",
        verbose_name="Akteur",
    )
    activity_type = models.CharField(max_length=30, choices=ACTIVITY_TYPES, verbose_name="Typ")
    content = models.TextField(blank=True, verbose_name="Inhalt")
    details = models.JSONField(default=dict, blank=True, verbose_name="Details")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Aufgaben-Aktivität"
        verbose_name_plural = "Aufgaben-Aktivitäten"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["task", "created_at"]),
        ]

    def __str__(self):
        return f"{self.actor}: {self.get_activity_type_display()}"

    @property
    def description(self) -> str:
        """Menschenlesbare Beschreibung der Aktivität."""
        actor_name = self.actor.user.get_display_name()
        details = self.details or {}

        filename = details.get("filename", "Datei")
        title = details.get("title", "Punkt")
        label = details.get("label", "")

        descriptions = {
            "created": f"{actor_name} hat die Aufgabe erstellt",
            "comment": "",  # Content wird direkt angezeigt
            "completed": f"{actor_name} hat die Aufgabe als erledigt markiert",
            "reopened": f"{actor_name} hat die Aufgabe wieder geöffnet",
            "attachment_added": f'{actor_name} hat "{filename}" angehängt',
            "attachment_removed": f'{actor_name} hat "{filename}" entfernt',
            "checklist_item_added": f'{actor_name} hat "{title}" zur Checkliste hinzugefügt',
            "checklist_item_completed": f'{actor_name} hat "{title}" abgehakt',
            "checklist_item_unchecked": f'{actor_name} hat "{title}" wieder geöffnet',
            "label_added": f'{actor_name} hat Label "{label}" hinzugefügt',
            "label_removed": f'{actor_name} hat Label "{label}" entfernt',
        }

        if self.activity_type in descriptions:
            return descriptions[self.activity_type]

        # Field change types with old → new
        old_val = details.get("old", "—")
        new_val = details.get("new", "—")
        field_labels = {
            "status_changed": "Status",
            "assigned": "Zuweisung",
            "priority_changed": "Priorität",
            "due_date_changed": "Fälligkeitsdatum",
            "visibility_changed": "Sichtbarkeit",
        }
        label = field_labels.get(self.activity_type, self.get_activity_type_display())
        return f"{actor_name} hat {label} geändert: {old_val} → {new_val}"
