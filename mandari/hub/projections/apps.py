# SPDX-License-Identifier: AGPL-3.0-or-later
"""App-Konfiguration der Sichten der Datendrehscheibe (Schatten-Quelle des RIS-Projektors)."""

from django.apps import AppConfig


class ProjectionsConfig(AppConfig):
    name = "hub.projections"
    label = "hub_projections"
    verbose_name = "Sichten der Datendrehscheibe"
    default_auto_field = "django.db.models.BigAutoField"
