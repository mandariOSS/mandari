# SPDX-License-Identifier: AGPL-3.0-or-later
"""
App-Konfiguration des RIS-Bestands in der Drehscheibe: Abruf und Texterkennung der Dateien (Issue #919), Befehl
``dokumentkette``.
"""

from django.apps import AppConfig


class RisConfig(AppConfig):
    name = "hub.ris"
    label = "hub_ris"
    verbose_name = "RIS-Bestand (Drehscheibe)"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """
        Kennzahlen und Statusprüfungen von Abruf und Texterkennung (``mandari_files_fetch_*``, ``dokumentabruf``,
        ``mandari_files_stored_without_text``, ``mandari_files_text_outdated``, ``dokumenttext``).
        """
        from . import abruf_kennzahlen, erkennung_kennzahlen

        abruf_kennzahlen.register()
        erkennung_kennzahlen.register()
