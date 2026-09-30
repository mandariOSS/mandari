# SPDX-License-Identifier: AGPL-3.0-or-later
"""App-Konfiguration der Ereignistechnik."""

from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.events"
    label = "events"
    verbose_name = "Ereignistechnik"

    def ready(self) -> None:
        from . import metrics, task_metrics

        metrics.register()
        task_metrics.register()
