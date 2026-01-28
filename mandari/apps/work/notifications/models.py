# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Notification models for the Work module.

Provides a centralized notification system with:
- Multiple notification types (meetings, tasks, motions, etc.)
- Read/unread status tracking
- Optional email delivery
- User preferences per notification type
"""

import uuid

from django.db import models
from django.utils import timezone


class NotificationType(models.TextChoices):
    """Available notification types."""

    # Meetings
    MEETING_REMINDER = "meeting_reminder", "Sitzungserinnerung"
    MEETING_UPDATED = "meeting_updated", "Sitzung aktualisiert"
    MEETING_CANCELLED = "meeting_cancelled", "Sitzung abgesagt"

    # Tasks
    TASK_ASSIGNED = "task_assigned", "Aufgabe zugewiesen"
    TASK_DUE_SOON = "task_due_soon", "Aufgabe fällig"
    TASK_COMPLETED = "task_completed", "Aufgabe erledigt"
    TASK_COMMENT = "task_comment", "Kommentar zur Aufgabe"

    # Motions
    MOTION_SHARED = "motion_shared", "Antrag geteilt"
    MOTION_COMMENT = "motion_comment", "Kommentar zum Antrag"
    MOTION_STATUS = "motion_status", "Antragsstatus geändert"

    # Faction
    FACTION_MEETING_REMINDER = "faction_reminder", "Fraktionssitzung"
    FACTION_MEETING_UPDATED = "faction_updated", "Fraktionssitzung aktualisiert"

    # Organization
    MEMBER_JOINED = "member_joined", "Neues Mitglied"
    ROLE_CHANGED = "role_changed", "Rolle geändert"

    # Support
    SUPPORT_TICKET_CREATED = "support_created", "Support-Ticket erstellt"
    SUPPORT_TICKET_REPLY = "support_reply", "Neue Antwort auf Ticket"
    SUPPORT_TICKET_STATUS = "support_status", "Ticketstatus geändert"
    SUPPORT_TICKET_RESOLVED = "support_resolved", "Ticket gelöst"
    SUPPORT_TICKET_ESCALATED = "support_escalated", "Ticket eskaliert"

    # System
    SYSTEM_MESSAGE = "system", "Systemnachricht"
    ANNOUNCEMENT = "announcement", "Ankündigung"


class Notification(models.Model):
    """
    A notification for a user.

    Notifications are organization-scoped and can be triggered by various
    events throughout the Work module.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Target user (via membership)
    recipient = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name="Empfänger"
    )

    # Notification content
    notification_type = models.CharField(
        max_length=30,
        choices=NotificationType.choices,
        default=NotificationType.SYSTEM_MESSAGE,
        verbose_name="Typ"
    )
    title = models.CharField(max_length=255, verbose_name="Titel")
    message = models.TextField(verbose_name="Nachricht")

    # Optional link to related object
    link = models.CharField(
        max_length=500,
        blank=True,
        verbose_name="Link"
    )

    # Actor (who triggered this notification, if any)
    actor = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_notifications",
        verbose_name="Auslöser"
    )

    # Status
    is_read = models.BooleanField(default=False, verbose_name="Gelesen")
    read_at = models.DateTimeField(null=True, blank=True, verbose_name="Gelesen am")

    # Email delivery
    email_sent = models.BooleanField(default=False, verbose_name="E-Mail gesendet")
    email_sent_at = models.DateTimeField(null=True, blank=True, verbose_name="E-Mail gesendet am")

    # Metadata (flexible storage for type-specific data)
    metadata = models.JSONField(default=dict, blank=True, verbose_name="Metadaten")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Benachrichtigung"
        verbose_name_plural = "Benachrichtigungen"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "is_read", "-created_at"]),
            models.Index(fields=["recipient", "notification_type"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.get_notification_type_display()})"

    def mark_as_read(self):
        """Mark this notification as read."""
        if not self.is_read:
            self.is_read = True
            self.read_at = timezone.now()
            self.save(update_fields=["is_read", "read_at"])

    @property
    def icon(self) -> str:
        """Return the Lucide icon name for this notification type."""
        icons = {
            NotificationType.MEETING_REMINDER: "calendar-clock",
            NotificationType.MEETING_UPDATED: "calendar-check",
            NotificationType.MEETING_CANCELLED: "calendar-x",
            NotificationType.TASK_ASSIGNED: "clipboard-list",
            NotificationType.TASK_DUE_SOON: "alarm-clock",
            NotificationType.TASK_COMPLETED: "check-circle",
            NotificationType.TASK_COMMENT: "message-square",
            NotificationType.MOTION_SHARED: "share-2",
            NotificationType.MOTION_COMMENT: "message-circle",
            NotificationType.MOTION_STATUS: "git-branch",
            NotificationType.FACTION_MEETING_REMINDER: "users",
            NotificationType.FACTION_MEETING_UPDATED: "users-cog",
            NotificationType.MEMBER_JOINED: "user-plus",
            NotificationType.ROLE_CHANGED: "shield",
            NotificationType.SUPPORT_TICKET_CREATED: "ticket",
            NotificationType.SUPPORT_TICKET_REPLY: "message-circle-reply",
            NotificationType.SUPPORT_TICKET_STATUS: "refresh-cw",
            NotificationType.SUPPORT_TICKET_RESOLVED: "check-circle-2",
            NotificationType.SUPPORT_TICKET_ESCALATED: "alert-triangle",
            NotificationType.SYSTEM_MESSAGE: "info",
            NotificationType.ANNOUNCEMENT: "megaphone",
        }
        return icons.get(self.notification_type, "bell")

    @property
    def color(self) -> str:
        """Return the color class for this notification type."""
        colors = {
            NotificationType.MEETING_REMINDER: "blue",
            NotificationType.MEETING_UPDATED: "green",
            NotificationType.MEETING_CANCELLED: "red",
            NotificationType.TASK_ASSIGNED: "purple",
            NotificationType.TASK_DUE_SOON: "orange",
            NotificationType.TASK_COMPLETED: "green",
            NotificationType.TASK_COMMENT: "gray",
            NotificationType.MOTION_SHARED: "indigo",
            NotificationType.MOTION_COMMENT: "gray",
            NotificationType.MOTION_STATUS: "blue",
            NotificationType.FACTION_MEETING_REMINDER: "purple",
            NotificationType.FACTION_MEETING_UPDATED: "purple",
            NotificationType.MEMBER_JOINED: "green",
            NotificationType.ROLE_CHANGED: "yellow",
            NotificationType.SUPPORT_TICKET_CREATED: "blue",
            NotificationType.SUPPORT_TICKET_REPLY: "indigo",
            NotificationType.SUPPORT_TICKET_STATUS: "yellow",
            NotificationType.SUPPORT_TICKET_RESOLVED: "green",
            NotificationType.SUPPORT_TICKET_ESCALATED: "red",
            NotificationType.SYSTEM_MESSAGE: "gray",
            NotificationType.ANNOUNCEMENT: "blue",
        }
        return colors.get(self.notification_type, "gray")


class NotificationPreference(models.Model):
    """
    User preferences for notification delivery.

    Each user can configure per-type settings for in-app and email notifications.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # User (via membership for organization context)
    membership = models.OneToOneField(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="notification_preferences",
        verbose_name="Mitgliedschaft"
    )

    # Global settings
    email_enabled = models.BooleanField(
        default=True,
        verbose_name="E-Mail-Benachrichtigungen aktiviert"
    )
    push_enabled = models.BooleanField(
        default=True,
        verbose_name="Push-Benachrichtigungen aktiviert"
    )

    # Per-type settings (JSON for flexibility)
    # Format: {"type_name": {"in_app": true, "email": true}}
    type_settings = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Typ-Einstellungen"
    )

    # Quiet hours
    quiet_hours_enabled = models.BooleanField(
        default=False,
        verbose_name="Ruhezeiten aktiviert"
    )
    quiet_hours_start = models.TimeField(
        null=True,
        blank=True,
        default="22:00",
        verbose_name="Ruhezeit Start"
    )
    quiet_hours_end = models.TimeField(
        null=True,
        blank=True,
        default="07:00",
        verbose_name="Ruhezeit Ende"
    )

    # Email digest
    email_digest = models.CharField(
        max_length=20,
        choices=[
            ("instant", "Sofort"),
            ("daily", "Täglich"),
            ("weekly", "Wöchentlich"),
            ("never", "Nie"),
        ],
        default="instant",
        verbose_name="E-Mail-Zusammenfassung"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Benachrichtigungseinstellung"
        verbose_name_plural = "Benachrichtigungseinstellungen"

    def __str__(self):
        return f"Notification preferences for {self.membership}"

    def is_type_enabled(self, notification_type: str, channel: str = "in_app") -> bool:
        """
        Check if a notification type is enabled for a specific channel.

        Args:
            notification_type: The notification type to check
            channel: Either "in_app" or "email"

        Returns:
            True if the notification type is enabled for the channel
        """
        # Check global email setting
        if channel == "email" and not self.email_enabled:
            return False

        # Check type-specific setting
        type_config = self.type_settings.get(notification_type, {})

        # Default to True if not configured
        return type_config.get(channel, True)

    def get_default_settings(self) -> dict:
        """Return default settings for all notification types."""
        return {
            ntype.value: {"in_app": True, "email": True}
            for ntype in NotificationType
        }
