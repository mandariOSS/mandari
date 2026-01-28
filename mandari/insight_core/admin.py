"""
Django Admin Konfiguration für OParl-Models.

Verwendet Django Unfold für modernes Admin-Interface.
"""

import asyncio
import threading
from django.contrib import admin, messages
from django.utils import timezone
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin
from unfold.decorators import action

from .models import (
    OParlSource,
    OParlBody,
    OParlOrganization,
    OParlPerson,
    OParlMeeting,
    OParlPaper,
    OParlAgendaItem,
    OParlFile,
    OParlMembership,
    OParlLocation,
    OParlConsultation,
    OParlLegislativeTerm,
)


def run_sync_in_thread(source_url: str, full: bool = False):
    """Run sync in a separate thread to not block the admin."""
    def sync_task():
        try:
            from insight_sync.tasks import sync_source
            result = sync_source(source_url, full=full)
            print(f"Sync completed: {result}")
        except Exception as e:
            print(f"Sync error: {e}")

    thread = threading.Thread(target=sync_task, daemon=True)
    thread.start()


@admin.register(OParlSource)
class OParlSourceAdmin(ModelAdmin):
    list_display = ["name", "url", "is_active", "sync_status_display", "last_sync_ago", "body_count"]
    list_filter = ["is_active"]
    search_fields = ["name", "url"]
    readonly_fields = ["id", "created_at", "updated_at", "last_sync", "last_full_sync"]
    actions_detail = ["sync_incremental_action", "sync_full_action"]
    actions = ["sync_all_incremental", "sync_all_full"]

    @admin.display(description="Sync-Status")
    def sync_status_display(self, obj):
        if not obj.last_sync:
            return mark_safe('<span style="color: #dc2626;">Nie synchronisiert</span>')

        age = timezone.now() - obj.last_sync
        hours = age.total_seconds() / 3600

        if hours < 0.5:
            return mark_safe('<span style="color: #16a34a;">Aktuell</span>')
        elif hours < 2:
            return mark_safe('<span style="color: #65a30d;">OK</span>')
        elif hours < 24:
            return mark_safe('<span style="color: #ca8a04;">Veraltet</span>')
        else:
            return mark_safe('<span style="color: #dc2626;">Sehr alt</span>')

    @admin.display(description="Letzter Sync")
    def last_sync_ago(self, obj):
        if not obj.last_sync:
            return "-"

        age = timezone.now() - obj.last_sync
        if age.days > 0:
            return f"vor {age.days} Tag(en)"
        hours = int(age.total_seconds() / 3600)
        if hours > 0:
            return f"vor {hours} Std."
        minutes = int(age.total_seconds() / 60)
        return f"vor {minutes} Min."

    @admin.display(description="Bodies")
    def body_count(self, obj):
        return obj.bodies.count()

    @action(description="Inkrementeller Sync", url_path="sync-incremental")
    def sync_incremental_action(self, request, object_id):
        obj = self.model.objects.get(pk=object_id)
        run_sync_in_thread(obj.url, full=False)
        messages.success(request, f"Inkrementeller Sync für '{obj.name}' gestartet. Läuft im Hintergrund.")
        return None

    @action(description="Vollständiger Sync", url_path="sync-full")
    def sync_full_action(self, request, object_id):
        obj = self.model.objects.get(pk=object_id)
        run_sync_in_thread(obj.url, full=True)
        messages.success(request, f"Vollständiger Sync für '{obj.name}' gestartet. Läuft im Hintergrund.")
        return None

    @admin.action(description="Inkrementeller Sync (ausgewählte)")
    def sync_all_incremental(self, request, queryset):
        for source in queryset:
            run_sync_in_thread(source.url, full=False)
        messages.success(request, f"Inkrementeller Sync für {queryset.count()} Quellen gestartet.")

    @admin.action(description="Vollständiger Sync (ausgewählte)")
    def sync_all_full(self, request, queryset):
        for source in queryset:
            run_sync_in_thread(source.url, full=True)
        messages.success(request, f"Vollständiger Sync für {queryset.count()} Quellen gestartet.")


@admin.register(OParlBody)
class OParlBodyAdmin(ModelAdmin):
    list_display = ["get_display_name", "name", "short_name", "display_name", "has_logo", "has_geo_data", "source"]
    list_filter = ["source", "classification"]
    search_fields = ["name", "short_name", "display_name"]
    readonly_fields = ["id", "external_id", "created_at", "updated_at", "oparl_created", "oparl_modified"]
    list_editable = ["display_name"]  # Direkt in der Liste bearbeitbar

    fieldsets = (
        ("Anzeige im Frontend", {
            "fields": ("display_name", "logo"),
            "description": "Diese Felder bestimmen, wie die Kommune im Frontend angezeigt wird."
        }),
        ("OParl-Daten", {
            "fields": ("name", "short_name", "classification", "website"),
        }),
        ("Geografische Daten (für Karte)", {
            "fields": (
                ("latitude", "longitude"),
                ("bbox_north", "bbox_south"),
                ("bbox_east", "bbox_west"),
                "osm_relation_id",
            ),
            "description": (
                "Zentrum und Bounding Box der Kommune für die Kartenanzeige. "
                "OSM Relation ID findest du auf https://www.openstreetmap.org/ - Suche nach der Stadt und kopiere die Relation ID aus der URL."
            ),
        }),
        ("Quelle", {
            "fields": ("source", "external_id"),
        }),
        ("Zeitstempel", {
            "fields": ("oparl_created", "oparl_modified", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
        ("Rohdaten", {
            "fields": ("raw_json",),
            "classes": ("collapse",),
        }),
    )

    @admin.display(boolean=True, description="Geo")
    def has_geo_data(self, obj):
        return bool(obj.latitude and obj.longitude and obj.bbox_north)

    @admin.display(description="Anzeigename")
    def get_display_name(self, obj):
        return obj.get_display_name()

    @admin.display(boolean=True, description="Logo")
    def has_logo(self, obj):
        return bool(obj.logo)


@admin.register(OParlOrganization)
class OParlOrganizationAdmin(ModelAdmin):
    list_display = [
        "name",
        "short_name",
        "organization_type",
        "start_date",
        "end_date",
        "is_active",
    ]
    list_filter = ["body", "organization_type"]
    search_fields = ["name", "short_name"]
    readonly_fields = ["id", "external_id", "created_at", "updated_at"]


@admin.register(OParlPerson)
class OParlPersonAdmin(ModelAdmin):
    list_display = ["display_name", "email", "body", "created_at"]
    list_filter = ["body"]
    search_fields = ["name", "family_name", "given_name", "email"]
    readonly_fields = ["id", "external_id", "created_at", "updated_at"]


@admin.register(OParlMeeting)
class OParlMeetingAdmin(ModelAdmin):
    list_display = ["name", "start", "location_name", "cancelled", "body"]
    list_filter = ["body", "cancelled", "meeting_state"]
    search_fields = ["name", "location_name"]
    date_hierarchy = "start"
    readonly_fields = ["id", "external_id", "created_at", "updated_at"]


@admin.register(OParlPaper)
class OParlPaperAdmin(ModelAdmin):
    list_display = ["reference", "name", "paper_type", "date", "body"]
    list_filter = ["body", "paper_type"]
    search_fields = ["name", "reference"]
    date_hierarchy = "date"
    readonly_fields = ["id", "external_id", "created_at", "updated_at"]


@admin.register(OParlAgendaItem)
class OParlAgendaItemAdmin(ModelAdmin):
    list_display = ["number", "name", "meeting", "public"]
    list_filter = ["public", "meeting__body"]
    search_fields = ["name", "number"]
    readonly_fields = ["id", "external_id", "created_at", "updated_at"]


@admin.register(OParlFile)
class OParlFileAdmin(ModelAdmin):
    list_display = ["name", "file_name", "mime_type", "size_human", "paper"]
    list_filter = ["mime_type"]
    search_fields = ["name", "file_name"]
    readonly_fields = ["id", "external_id", "created_at", "updated_at"]


@admin.register(OParlMembership)
class OParlMembershipAdmin(ModelAdmin):
    list_display = ["person", "organization", "role", "start_date", "end_date", "is_active"]
    list_filter = ["organization__body", "role", "voting_right"]
    search_fields = ["person__name", "organization__name", "role"]
    readonly_fields = ["id", "external_id", "created_at", "updated_at"]


@admin.register(OParlLocation)
class OParlLocationAdmin(ModelAdmin):
    list_display = ["description", "room", "street_address", "locality", "body"]
    list_filter = ["body", "locality"]
    search_fields = ["description", "room", "street_address", "locality"]
    readonly_fields = ["id", "external_id", "created_at", "updated_at"]


@admin.register(OParlConsultation)
class OParlConsultationAdmin(ModelAdmin):
    list_display = ["paper", "role", "authoritative", "meeting_external_id", "body"]
    list_filter = ["body", "authoritative"]
    search_fields = ["role", "paper__name", "paper__reference"]
    readonly_fields = ["id", "external_id", "created_at", "updated_at"]


@admin.register(OParlLegislativeTerm)
class OParlLegislativeTermAdmin(ModelAdmin):
    list_display = ["name", "start_date", "end_date", "is_current", "body"]
    list_filter = ["body"]
    search_fields = ["name"]
    readonly_fields = ["id", "external_id", "created_at", "updated_at"]


# =============================================================================
# Location Mapping Admin
# =============================================================================

from .models import LocationMapping, TileCache


@admin.register(TileCache)
class TileCacheAdmin(ModelAdmin):
    list_display = ["tile_coords", "content_type", "fetched_from", "created_at", "updated_at"]
    list_filter = ["z", "fetched_from"]
    search_fields = ["z", "x", "y"]
    readonly_fields = ["z", "x", "y", "tile_data", "content_type", "fetched_from", "created_at", "updated_at"]
    ordering = ["-updated_at"]

    @admin.display(description="Tile")
    def tile_coords(self, obj):
        return f"{obj.z}/{obj.x}/{obj.y}"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(LocationMapping)
class LocationMappingAdmin(ModelAdmin):
    list_display = ["location_name", "body", "latitude", "longitude", "address"]
    list_filter = ["body"]
    search_fields = ["location_name", "address", "description"]
    readonly_fields = ["id", "created_at", "updated_at"]
    autocomplete_fields = ["body"]

    fieldsets = (
        (None, {
            "fields": ("body", "location_name")
        }),
        ("Koordinaten", {
            "fields": ("latitude", "longitude"),
            "description": "Koordinaten im Dezimalformat (z.B. 51.9617867, 7.6281645)"
        }),
        ("Zusatzinformationen", {
            "fields": ("address", "description"),
            "classes": ("collapse",)
        }),
        ("Metadaten", {
            "fields": ("id", "created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )
