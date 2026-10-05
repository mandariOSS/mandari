# SPDX-License-Identifier: AGPL-3.0-or-later
"""App-Konfiguration des Vertragsregisters (ohne Modelle)."""

from django.apps import AppConfig


class ContractsConfig(AppConfig):
    name = "hub.contracts"
    label = "hub_contracts"
    verbose_name = "Verträge der Datendrehscheibe"

    def ready(self) -> None:
        from apps.events.datenschutz import set_person_fields_provider
        from apps.events.publishing import set_contract_validator

        from . import checks  # noqa: F401  (registriert die Systemprüfung)
        from .publishing import validate_published_event
        from .registry import person_fields_by_contract

        # Die Plattform kennt die Drehscheibe nicht: publish() prüft über diesen Einhängepunkt gegen
        # das Register (in Tests und bei DEBUG, siehe EVENTS_VALIDATE_CONTRACTS).
        set_contract_validator(validate_published_event)
        # Welche Felder personenbezogener Ereignisse eine Person nennen (``x-person``), für das
        # Neutralisieren nach einem ``redact`` (apps.events.datenschutz)
        set_person_fields_provider(person_fields_by_contract)
