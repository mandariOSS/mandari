# SPDX-License-Identifier: AGPL-3.0-or-later
"""App-Konfiguration der Live-Übertragungen (Issue #915)."""

from django.apps import AppConfig


class LiveConfig(AppConfig):
    name = "hub.live"
    label = "hub_live"
    verbose_name = "Live-Übertragungen"
    default_auto_field = "django.db.models.BigAutoField"
