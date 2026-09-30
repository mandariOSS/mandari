# SPDX-License-Identifier: AGPL-3.0-or-later
"""App-Konfiguration des Vertragsregisters (ohne Modelle)."""

from django.apps import AppConfig


class ContractsConfig(AppConfig):
    name = "hub.contracts"
    label = "hub_contracts"
    verbose_name = "Verträge der Datendrehscheibe"

    def ready(self) -> None:
        from . import checks  # noqa: F401  (registriert die Systemprüfung)
