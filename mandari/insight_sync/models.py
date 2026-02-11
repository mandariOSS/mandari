"""
Models für Sync-Verwaltung: Protokoll und Konfiguration.
"""

from django.conf import settings
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


class SyncConfig(models.Model):
    """Singleton: Daemon-Konfiguration für den Sync."""

    sync_enabled = models.BooleanField(
        default=True,
        verbose_name="Sync aktiv",
        help_text="Daemon pausieren/fortsetzen",
    )
    interval_minutes = models.PositiveIntegerField(
        default=15,
        verbose_name="Intervall (Minuten)",
        help_text="Minuten zwischen inkrementellen Syncs",
    )
    full_sync_hour = models.PositiveIntegerField(
        default=99,
        verbose_name="Full-Sync-Stunde",
        help_text="Stunde (0–23) für den täglichen Full Sync. Wert > 23 = deaktiviert.",
    )
    max_concurrent = models.PositiveIntegerField(
        default=10,
        verbose_name="Max. gleichzeitige Requests",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Aktualisiert")

    class Meta:
        verbose_name = "Sync-Einstellung"
        verbose_name_plural = "Sync-Einstellungen"

    def __str__(self):
        state = "aktiv" if self.sync_enabled else "pausiert"
        return f"Sync-Konfiguration ({state}, alle {self.interval_minutes} Min.)"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        """Liefert die Singleton-Instanz (erstellt sie ggf. mit Defaults aus settings)."""
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "interval_minutes": getattr(settings, "SYNC_INTERVAL_MINUTES", 15),
                "full_sync_hour": getattr(settings, "SYNC_FULL_HOUR", 99),
            },
        )
        return obj
