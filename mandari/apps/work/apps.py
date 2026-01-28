# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Work module app configuration.

The Work module provides a collaborative workspace for political organizations
including meetings preparation, motion management, faction meetings, and tasks.
"""

from django.apps import AppConfig


class WorkConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.work"
    label = "work"
    verbose_name = "Work Portal"

    def ready(self):
        # Import submodules to register their models
        pass
