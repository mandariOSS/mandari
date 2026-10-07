# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verwaltung der Live-Übertragungen (Issue #915): Quellen mit Profil, Übertragungen, Abschnitte, Wortmeldungen."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from unfold.admin import ModelAdmin, TabularInline

from .models import Broadcast, BroadcastLog, BroadcastSection, BroadcastSource, BroadcastSpeech


@admin.register(BroadcastSource)
class BroadcastSourceAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ("organization", "body", "provider", "active", "interval_seconds", "updated_at")
    list_filter = ("active", "provider")
    raw_id_fields = ("body", "organization")
    readonly_fields = ("created_at", "updated_at")
    search_fields = ("identifier", "organization__name")


class BroadcastSectionInline(TabularInline):  # type: ignore[misc]
    model = BroadcastSection
    extra = 0
    fields = ("number", "agenda_item", "title_read", "title_similarity", "origin", "started_at", "ended_at")
    raw_id_fields = ("agenda_item",)


@admin.register(Broadcast)
class BroadcastAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ("meeting", "source", "status", "started_at", "ended_at", "frames_read", "frames_without_overlay")
    list_filter = ("status",)
    raw_id_fields = ("meeting", "source", "current_section", "current_speech")
    readonly_fields = (
        "last_checked_at",
        "last_status",
        "stream",
        "debounce",
        "frames_read",
        "frames_without_overlay",
        "created_at",
        "updated_at",
    )
    inlines = [BroadcastSectionInline]


@admin.register(BroadcastSection)
class BroadcastSectionAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ("number", "broadcast", "agenda_item", "title_similarity", "origin", "started_at", "ended_at")
    list_filter = ("origin",)
    raw_id_fields = ("broadcast", "agenda_item")


@admin.register(BroadcastSpeech)
class BroadcastSpeechAdmin(ModelAdmin):  # type: ignore[misc]
    """Wortmeldungen: Person korrigierbar (Zuordnung von Hand auf „eindeutig“ setzen)."""

    list_display = ("name_read", "faction_read", "function_read", "person", "assignment", "readings", "started_at")
    list_filter = ("assignment",)
    raw_id_fields = ("broadcast", "section", "person")
    readonly_fields = ("name_read", "faction_read", "function_read", "readings", "started_at")
    search_fields = ("name_read",)


@admin.register(BroadcastLog)
class BroadcastLogAdmin(ModelAdmin):  # type: ignore[misc]
    """Protokoll nur lesen."""

    list_display = ("at", "kind", "broadcast", "source")
    list_filter = ("kind",)
    readonly_fields = ("source", "broadcast", "at", "kind", "data")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
