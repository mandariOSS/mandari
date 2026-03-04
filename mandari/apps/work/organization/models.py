# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization-level models for change requests and absence management.
"""

import uuid

from django.db import models
from django.utils import timezone


class MemberChangeRequest(models.Model):
    """
    Request from a member to change their roles, committees, or permissions.

    Admins can approve or reject these requests.
    """

    REQUEST_TYPE_CHOICES = [
        ("role_change", "Rollenänderung"),
        ("committee_change", "Gremienänderung"),
        ("permission_request", "Berechtigungsanfrage"),
    ]

    STATUS_CHOICES = [
        ("pending", "Ausstehend"),
        ("approved", "Genehmigt"),
        ("rejected", "Abgelehnt"),
        ("withdrawn", "Zurückgezogen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="change_requests",
        verbose_name="Organisation",
    )
    requester = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="change_requests",
        verbose_name="Antragsteller",
    )

    request_type = models.CharField(
        max_length=30,
        choices=REQUEST_TYPE_CHOICES,
        verbose_name="Antragstyp",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
        verbose_name="Status",
    )

    # Flexible JSON for request details
    # role_change: {"requested_roles": [uuid, ...]}
    # committee_change: {"add_committees": [uuid, ...], "remove_committees": [uuid, ...]}
    # permission_request: {"requested_permissions": ["perm.code", ...]}
    request_data = models.JSONField(default=dict, verbose_name="Antragsdaten")
    reason = models.TextField(verbose_name="Begründung")

    # Decision
    decided_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_change_requests",
        verbose_name="Entschieden von",
    )
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name="Entschieden am")
    decision_comment = models.TextField(blank=True, verbose_name="Entscheidungskommentar")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Änderungsantrag"
        verbose_name_plural = "Änderungsanträge"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "status", "-created_at"]),
            models.Index(fields=["requester", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.get_request_type_display()} von {self.requester} ({self.get_status_display()})"


class MemberAbsence(models.Model):
    """
    Absence period for a member with optional deputy assignment.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="member_absences",
        verbose_name="Organisation",
    )
    membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="absences",
        verbose_name="Mitglied",
    )

    start_date = models.DateField(verbose_name="Von")
    end_date = models.DateField(verbose_name="Bis")
    reason = models.CharField(max_length=500, blank=True, verbose_name="Grund")

    # Deputy
    deputy = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deputy_for",
        verbose_name="Stellvertreter",
    )

    auto_decline_meetings = models.BooleanField(default=True, verbose_name="Sitzungen automatisch absagen")
    notify_deputy = models.BooleanField(default=True, verbose_name="Stellvertreter benachrichtigen")
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Abwesenheit"
        verbose_name_plural = "Abwesenheiten"
        ordering = ["-start_date"]
        indexes = [
            models.Index(fields=["organization", "membership", "-start_date"]),
        ]

    def __str__(self):
        return f"{self.membership} abwesend {self.start_date} – {self.end_date}"

    @property
    def is_current(self):
        today = timezone.now().date()
        return self.is_active and self.start_date <= today <= self.end_date

    @property
    def is_future(self):
        today = timezone.now().date()
        return self.is_active and self.start_date > today

    @property
    def is_past(self):
        today = timezone.now().date()
        return self.end_date < today


class DataExport(models.Model):
    """
    Tracks async DSGVO data export requests with background processing.

    Exports are generated in the background and stored on disk for download.
    """

    STATUS_CHOICES = [
        ("pending", "Ausstehend"),
        ("processing", "Wird erstellt"),
        ("completed", "Fertig"),
        ("failed", "Fehlgeschlagen"),
    ]
    FORMAT_CHOICES = [("json", "JSON"), ("pdf", "PDF")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="data_exports",
        verbose_name="Organisation",
    )
    membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="data_exports",
        verbose_name="Mitglied",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
        verbose_name="Status",
    )
    export_format = models.CharField(
        max_length=10,
        choices=FORMAT_CHOICES,
        default="json",
        verbose_name="Format",
    )
    file_path = models.CharField(
        max_length=500,
        blank=True,
        verbose_name="Dateipfad",
    )
    file_size = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Dateigröße",
    )
    error_message = models.TextField(blank=True, verbose_name="Fehlermeldung")

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="Gestartet")
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name="Abgeschlossen")

    class Meta:
        verbose_name = "Datenexport"
        verbose_name_plural = "Datenexporte"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["membership", "-created_at"]),
            models.Index(fields=["organization", "status"]),
        ]

    def __str__(self):
        return f"Export {self.get_export_format_display()} ({self.get_status_display()})"

    @property
    def is_ready(self):
        return self.status == "completed"

    @property
    def is_in_progress(self):
        return self.status in ("pending", "processing")

    @property
    def file_size_human(self):
        if not self.file_size:
            return ""
        size = self.file_size
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def get_absolute_path(self):
        if not self.file_path:
            return None
        from django.conf import settings

        return settings.MEDIA_ROOT / self.file_path

    def delete_file(self):
        path = self.get_absolute_path()
        if path and path.exists():
            path.unlink()
