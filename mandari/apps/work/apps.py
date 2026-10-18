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
        # Änderungshistorie (Audit) für Fraktionssitzungen registrieren (Issue #66)
        from apps.work.faction import audit as faction_audit

        faction_audit.register()

        # Dokument-Freigaben beim Entfernen einer Mitgliedschaft aufräumen
        from apps.work.motions import signals as motion_signals

        motion_signals.register()

        # Entzug von Freigaben, Mitgliedschaften und Rechten wirkt sofort in geöffneten Bearbeitungen (Issue #582)
        from apps.work.motions import freigaben

        freigaben.register()

        # Mitgliedschaft gelöscht: persönliche Einträge weg, Organisationsinhalte bleiben (Issue #420)
        from apps.work.organization import member_data

        member_data.register()

        # Verknüpfungen mit Tagesordnungspunkten: fachlichen Anker beim Verknüpfen festhalten (Issue #547)
        from apps.work.ris import verknuepfungen

        verknuepfungen.register()
