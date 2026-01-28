# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Accounts app configuration.

Provides user management, authentication, and 2FA support.
"""

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "Benutzerverwaltung"
