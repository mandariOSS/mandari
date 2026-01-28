# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session app configuration."""

from django.apps import AppConfig


class SessionConfig(AppConfig):
    """Configuration for the Session RIS app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.session"
    verbose_name = "Session RIS"
    verbose_name_plural = "Session RIS"

    def ready(self):
        """Initialize app when Django starts."""
        # Import signals to register them
        try:
            from . import signals  # noqa: F401
        except ImportError:
            pass
