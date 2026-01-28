# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Common app configuration.

Provides shared utilities for encryption, permissions, and base mixins.
"""

from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"
    label = "common"
    verbose_name = "Gemeinsame Utilities"
