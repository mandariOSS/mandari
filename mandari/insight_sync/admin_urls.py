"""Custom admin URL für den Sync-Trigger."""

from django.urls import path

from .admin import trigger_sync_view

urlpatterns = [
    path("", trigger_sync_view, name="insight_sync_trigger_sync"),
]
