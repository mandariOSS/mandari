# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Custom email backend that reads SMTP settings from SiteSettings.

This backend is used automatically by Django's email functions (including
password reset) and reads configuration from the database via SiteSettings.
"""

import logging
import threading

from django.core.mail.backends.smtp import EmailBackend as SMTPBackend

logger = logging.getLogger(__name__)

# Thread-local storage for connection settings
_local = threading.local()


class SiteSettingsEmailBackend(SMTPBackend):
    """
    SMTP email backend that reads settings from SiteSettings model.

    Falls back to Django settings if SiteSettings doesn't have values configured.

    Usage in settings.py (Django ≥ 6.1, Issue #80):
        MAILERS = {"default": {"BACKEND": "apps.common.email_backend.SiteSettingsEmailBackend"}}
    """

    def __init__(self, **kwargs):
        # Get configuration from SiteSettings
        config = self._get_config()

        # WICHTIG: setdefault() reicht NICHT. Django reicht aus MAILERS/OPTIONS bzw.
        # beim Aufbau username=None, password=None durch – der Schlüssel EXISTIERT
        # dann mit Wert None, setdefault() greift nicht und die Verbindung liefe
        # ohne SMTP-Login (Server verweigert das Relay). Deshalb: explizite
        # None-Werte durch die SiteSettings ersetzen.
        for key in ("host", "port", "username", "password", "use_tls", "use_ssl", "timeout"):
            if kwargs.get(key) is None:
                kwargs[key] = config.get(key)
        # Alle Optionen müssen gesetzt sein: Für None liest Django settings.EMAIL_*, das es neben
        # MAILERS nicht mehr gibt (Deploy-Rückfall 18.09.2026, Issue #80).
        from apps.common.mail_backends import LAUFZEIT_ALIAS, smtp_options

        kwargs.setdefault("alias", LAUFZEIT_ALIAS)  # über MAILERS kommt "default" herein
        super().__init__(**smtp_options(**kwargs))

        # Log configuration (without password)
        logger.debug(
            f"SiteSettingsEmailBackend initialized: "
            f"host={self.host}, port={self.port}, "
            f"user={self.username}, tls={self.use_tls}, ssl={self.use_ssl}"
        )

    def _get_config(self) -> dict:
        """Zugang der Plattform aus dem Mail-Dienst (Systemeinstellungen, sonst Umgebung)."""
        from django.conf import settings as django_settings

        try:
            from apps.common.mail.config import platform_config

            config = platform_config()
            return {
                "host": config.host,
                "port": config.port,
                "username": config.username,
                "password": config.password,
                "use_tls": config.use_tls,
                "use_ssl": config.use_ssl,
                "timeout": config.timeout,
            }
        except Exception as e:
            logger.warning("Systemeinstellungen nicht lesbar, Zugang aus der Umgebung (%s)", type(e).__name__)

        # Fallback: SMTP-Zugang aus der Umgebung (settings.SMTP_FALLBACK, Issue #80)
        fallback = dict(getattr(django_settings, "SMTP_FALLBACK", {}) or {})
        return {
            "host": fallback.get("host", ""),
            "port": fallback.get("port", 587),
            "username": fallback.get("username", ""),
            "password": fallback.get("password", ""),
            "use_tls": fallback.get("use_tls", True),
            "use_ssl": fallback.get("use_ssl", False),
            "timeout": fallback.get("timeout", 30),
        }
