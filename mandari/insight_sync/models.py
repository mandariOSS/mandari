# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Models für Sync-Verwaltung: Protokoll der Sync-Läufe (schreibt der Ingestor).
"""

from django.db import models


class SyncLog(models.Model):
    """Protokoll eines Sync-Laufs."""

    class SyncType(models.TextChoices):
        INCREMENTAL = "incremental", "Inkrementell"
        FULL = "full", "Vollständig"

    class Status(models.TextChoices):
        RUNNING = "running", "Läuft"
        SUCCESS = "success", "Erfolgreich"
        FAILED = "failed", "Fehlgeschlagen"

    source = models.ForeignKey(
        "insight_core.OParlSource",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sync_logs",
        verbose_name="Quelle",
        help_text="Leer = alle Quellen",
    )
    sync_type = models.CharField(
        max_length=20,
        choices=SyncType.choices,
        verbose_name="Sync-Typ",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RUNNING,
        verbose_name="Status",
    )
    started_at = models.DateTimeField(auto_now_add=True, verbose_name="Gestartet")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Beendet")
    duration_seconds = models.FloatField(null=True, blank=True, verbose_name="Dauer (s)")
    entities_synced = models.IntegerField(default=0, verbose_name="Entitäten")
    errors = models.JSONField(default=list, blank=True, verbose_name="Fehler")
    triggered_by = models.CharField(
        max_length=20,
        verbose_name="Ausgelöst durch",
        help_text="admin, daemon, cli",
    )
    details = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Details",
        help_text="Entitäten-Zähler pro Typ",
    )

    class Meta:
        verbose_name = "Sync-Protokoll"
        verbose_name_plural = "Sync-Protokolle"
        ordering = ["-started_at"]

    def __str__(self):
        source_name = self.source.name if self.source else "Alle Quellen"
        return f"{self.get_sync_type_display()} – {source_name} – {self.get_status_display()}"
