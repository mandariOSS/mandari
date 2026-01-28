# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tenants app configuration.

Provides multi-tenant architecture with dual grouping:
- Party hierarchy (e.g., Volt Deutschland → Volt NRW → Volt Münster)
- Regional grouping (via OParl Body)
"""

from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenants"
    label = "tenants"
    verbose_name = "Mandantenverwaltung"

    def ready(self):
        # Import signals to register them
        from . import signals  # noqa: F401
