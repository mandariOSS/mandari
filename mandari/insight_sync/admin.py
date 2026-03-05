"""
Admin-Konfiguration für Sync-Verwaltung.

- SyncLogAdmin: Readonly-Protokoll aller Sync-Läufe
- SyncConfigAdmin: Singleton-Editor für Daemon-Einstellungen
- trigger-sync/: POST-Endpoint zum Starten eines Syncs vom Dashboard
"""

import threading

from django.contrib import admin, messages
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.urls import reverse
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin

from .models import SyncConfig, SyncLog


@admin.register(SyncLog)
class SyncLogAdmin(ModelAdmin):
    list_display = [
        "status_badge",
        "source_display",
        "sync_type",
        "duration_display",
        "entities_synced",
        "started_at",
        "triggered_by",
    ]
    list_filter = ["status", "sync_type", "triggered_by"]
    search_fields = ["source__name"]
    readonly_fields = [
        "source",
        "sync_type",
        "status",
        "started_at",
        "finished_at",
        "duration_seconds",
        "entities_synced",
        "errors",
        "triggered_by",
        "details",
    ]
    ordering = ["-started_at"]
    date_hierarchy = "started_at"
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            SyncLog.Status.RUNNING: ("#2563eb", "sync"),
            SyncLog.Status.SUCCESS: ("#16a34a", "check_circle"),
            SyncLog.Status.FAILED: ("#dc2626", "error"),
        }
        color, icon = colors.get(obj.status, ("#64748b", "help"))
        label = obj.get_status_display()
        return mark_safe(
            f'<span style="color: {color}; font-weight: 600;">'
            f'<span class="material-symbols-outlined" style="font-size: 16px; vertical-align: middle;">{icon}</span> '
            f"{label}</span>"
        )

    @admin.display(description="Quelle")
    def source_display(self, obj):
        return obj.source.name if obj.source else "Alle Quellen"

    @admin.display(description="Dauer")
    def duration_display(self, obj):
        if obj.duration_seconds is None:
            return "-"
        if obj.duration_seconds < 60:
            return f"{obj.duration_seconds:.1f} s"
        minutes = int(obj.duration_seconds // 60)
        seconds = obj.duration_seconds % 60
        return f"{minutes} min {seconds:.0f} s"


@admin.register(SyncConfig)
class SyncConfigAdmin(ModelAdmin):
    list_display = [
        "sync_enabled",
        "interval_minutes",
        "full_sync_hour",
        "max_concurrent",
        "updated_at",
    ]
    readonly_fields = ["updated_at"]

    def has_add_permission(self, request):
        # Singleton: nur 1 Objekt erlaubt
        return not SyncConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        """Direkt zum einzigen Objekt weiterleiten."""
        config = SyncConfig.get()
        return HttpResponseRedirect(reverse("admin:insight_sync_syncconfig_change", args=[config.pk]))


def trigger_sync_view(request):
    """POST-Endpoint: Startet einen Sync vom Dashboard."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    if not request.user.is_staff:
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden()

    # Prüfe ob bereits ein Sync läuft
    if SyncLog.objects.filter(status=SyncLog.Status.RUNNING).exists():
        messages.warning(request, "Ein Sync läuft bereits.")
        return HttpResponseRedirect(reverse("admin:index"))

    full = request.POST.get("full") == "1"

    def _run():
        from .tasks import run_sync_with_logging

        run_sync_with_logging(full=full, triggered_by="admin")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    sync_label = "Vollständiger" if full else "Inkrementeller"
    messages.success(request, f"{sync_label} Sync gestartet. Läuft im Hintergrund.")
    return HttpResponseRedirect(reverse("admin:index"))
