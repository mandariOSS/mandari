# SPDX-License-Identifier: AGPL-3.0-or-later
"""App-Konfiguration des RIS-Bestands in der Drehscheibe: Abruf der Dateien (Issue #919), Befehl ``dokumentkette``."""

from django.apps import AppConfig


class RisConfig(AppConfig):
    name = "hub.ris"
    label = "hub_ris"
    verbose_name = "RIS-Bestand (Drehscheibe)"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Kennzahlen und Statusprüfung des Abrufs (``mandari_files_fetch_*``, ``dokumentabruf``)."""
        from . import abruf_kennzahlen

        abruf_kennzahlen.register()
