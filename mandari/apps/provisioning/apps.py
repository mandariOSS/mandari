# SPDX-License-Identifier: AGPL-3.0-or-later
from django.apps import AppConfig


class ProvisioningConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.provisioning"
    verbose_name = "Provisioning-API"
