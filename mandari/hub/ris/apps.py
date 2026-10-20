# SPDX-License-Identifier: AGPL-3.0-or-later
"""
App-Konfiguration des RIS-Bestands in der Drehscheibe: Abruf und Texterkennung der Dateien (Issue #919), Befehl
``dokumentkette``; Vergleichsschlüssel der Vorlagennummern in SQLite (Issue #1102).
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
        from django.db import connections
        from django.db.backends.signals import connection_created

        from . import abruf_kennzahlen, erkennung_kennzahlen, vorlagennummer

        abruf_kennzahlen.register()
        erkennung_kennzahlen.register()
        # SQLite kennt kein regexp_replace: den Schlüssel der Vorlagennummern als Python-Funktion je Verbindung
        connection_created.connect(vorlagennummer.sqlite_anmelden, dispatch_uid="hub_ris_vorlagennummer")
        for verbindung in connections.all(initialized_only=True):
            if verbindung.connection is not None:
                vorlagennummer.sqlite_anmelden(None, verbindung)
