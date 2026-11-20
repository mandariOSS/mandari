# SPDX-License-Identifier: AGPL-3.0-or-later
from django.apps import AppConfig


class InsightCoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "insight_core"
    verbose_name = "Mandari Insight Core"

    def ready(self) -> None:
        """Registriert Signals und die Statusprüfungen (Texterkennung, Datenabgleich) beim App-Start."""
        from apps.events.status import register_check

        # Import signals to register them
        from . import portal, signals  # noqa: F401  (portal: Systemprüfung PORTAL_HOSTS, Issue #317)
        from .services.datenabgleich import check_datenabgleich
        from .services.text_extraction_health import check_text_extraction

        # Statusseite: hängende bzw. nach wiederholtem Abbruch aufgegebene Texterkennung (Issue #817)
        register_check("texterkennung", check_text_extraction)
        # Statusseite: Kommune mit kritischem Datenabgleich (Issue #1525)
        register_check("datenabgleich", check_datenabgleich)
