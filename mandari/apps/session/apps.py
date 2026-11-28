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

        # Demo-Organisationen (Issue #1406): Ein Konto mit Session-Zugang bleibt trotz abgelaufener Work-Demo
        # anmeldbar
        from apps.tenants import demo

        demo.weiteren_zugang_melden(_hat_session_zugang)

        # Abgeschaltet (MODUL_SESSION=aus, Issue #1594): keine Befehle, keine Bereiche der Schnittstelle
        from apps.common import module

        if not module.aktiv(module.SESSION):
            return

        # Befehle beim Dispatcher der Installation registrieren (submission.submit, Issue #540)
        from . import commands  # noqa: F401 – der Import registriert die Handler

        # Befehle der Befehlsschicht (Issue #1588): Mandantenbereich der Schnittstelle und Befehle von Session
        from .befehle import register as befehle_anmelden

        befehle_anmelden()

        # Bereiche der gemeinsamen Schnittstelle anmelden: OParl je Mandant, Session-API v1 (Issue #1205)
        from .api import schnittstelle

        schnittstelle.register()

        # Bindung von Diensten an einen Mandanten (Profil Nichtöffentliches, Issue #565): die Plattform speichert nur
        # die fachfreie Kennung, Session löst Kurznamen auf und nennt den Mandanten
        from .api import nichtoeffentlich

        nichtoeffentlich.register()


def _hat_session_zugang(user) -> bool:
    """True, wenn das Konto in einem aktiven Mandanten aktiv ist (Demo-Organisationen, Issue #1406)."""
    from .models import SessionUser

    return SessionUser.objects.filter(user=user, is_active=True, tenant__is_active=True).exists()
