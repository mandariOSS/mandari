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

        # Rollenzuweisungen (Issue #772): Spiegel von SessionUser.roles bei jeder Änderung und nach jedem migrate
        # nachführen (Änderungen eines älteren Images, Massenänderungen ohne Signal). Idempotent.
        from django.db.models.signals import m2m_changed

        from .models import SessionUser
        from .rechte.zuweisungen import post_migrate_abgleichen, rollen_geaendert

        m2m_changed.connect(rollen_geaendert, sender=SessionUser.roles.through, dispatch_uid="session_rollen_spiegel")
        post_migrate.connect(post_migrate_abgleichen, sender=self, dispatch_uid="session_rollen_post_migrate")
