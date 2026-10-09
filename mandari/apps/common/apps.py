# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Common app configuration.

Provides shared utilities for encryption, permissions, and base mixins.
"""

from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"
    label = "common"
    verbose_name = "Gemeinsame Utilities"

    def ready(self) -> None:
        # Tracing nur, wenn OTEL_EXPORTER_OTLP_ENDPOINT gesetzt ist (sonst No-Op)
        from .observability import setup_opentelemetry

        setup_opentelemetry()

        # Message-ID und EHLO mit vollständigem Domainnamen statt der Container-ID (Issue #957); der Import
        # registriert zugleich die Systemprüfung der Einstellung
        from .mail_domain import apply as apply_mail_domain

        apply_mail_domain()
