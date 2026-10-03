# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session app configuration."""

import contextlib

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
        with contextlib.suppress(ImportError):
            from . import signals  # noqa: F401

        # Körperschaften (Issue #756): Nach jedem migrate Gremien und Vorlagen ohne Körperschaft zuordnen –
        # Nachzügler eines älteren Images (Containerwechsel, Rückfall). Idempotent.
        from django.db.models.signals import post_migrate

        from .services.body_service import post_migrate_assign

        post_migrate.connect(post_migrate_assign, sender=self, dispatch_uid="session_bodies_post_migrate")
