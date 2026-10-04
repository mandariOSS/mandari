# SPDX-License-Identifier: AGPL-3.0-or-later
from django.apps import AppConfig


class InsightSyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "insight_sync"
    verbose_name = "Mandari Insight Sync"
