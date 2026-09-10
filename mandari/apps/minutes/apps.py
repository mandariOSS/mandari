# SPDX-License-Identifier: AGPL-3.0-or-later
"""App-Konfiguration für die Protokoll-Pipeline."""

from django.apps import AppConfig


class MinutesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.minutes"
    label = "minutes"
    verbose_name = "Protokollierung"
