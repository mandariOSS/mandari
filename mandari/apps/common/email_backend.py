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

    Usage in settings.py:
        EMAIL_BACKEND = "apps.common.email_backend.SiteSettingsEmailBackend"
    """

    def __init__(self, **kwargs):
        # Get configuration from SiteSettings
        config = self._get_config()

        # Override any kwargs with SiteSettings values (if set)
        kwargs.setdefault("host", config.get("host"))
        kwargs.setdefault("port", config.get("port"))
        kwargs.setdefault("username", config.get("username"))
        kwargs.setdefault("password", config.get("password"))
        kwargs.setdefault("use_tls", config.get("use_tls"))
        kwargs.setdefault("use_ssl", config.get("use_ssl"))
        kwargs.setdefault("timeout", config.get("timeout"))

        super().__init__(**kwargs)

        # Log configuration (without password)
        logger.debug(
            f"SiteSettingsEmailBackend initialized: "
            f"host={self.host}, port={self.port}, "
            f"user={self.username}, tls={self.use_tls}, ssl={self.use_ssl}"
        )

    def _get_config(self) -> dict:
        """
        Get email configuration from SiteSettings with fallback to Django settings.
        """
        from django.conf import settings as django_settings

        try:
            from .models import SiteSettings
            site_settings = SiteSettings.get_settings()

            # Use SiteSettings if email_host is configured, otherwise fallback
            if site_settings.email_host:
                return {
                    "host": site_settings.email_host,
                    "port": site_settings.email_port,
                    "username": site_settings.email_host_user,
                    "password": site_settings.email_host_password,
                    "use_tls": site_settings.email_use_tls,
                    "use_ssl": site_settings.email_use_ssl,
                    "timeout": site_settings.email_timeout,
                }
        except Exception as e:
            logger.warning(f"Could not load SiteSettings: {e}")

        # Fallback to Django settings
        return {
            "host": getattr(django_settings, "EMAIL_HOST", ""),
            "port": getattr(django_settings, "EMAIL_PORT", 587),
            "username": getattr(django_settings, "EMAIL_HOST_USER", ""),
            "password": getattr(django_settings, "EMAIL_HOST_PASSWORD", ""),
            "use_tls": getattr(django_settings, "EMAIL_USE_TLS", True),
            "use_ssl": getattr(django_settings, "EMAIL_USE_SSL", False),
            "timeout": getattr(django_settings, "EMAIL_TIMEOUT", 30),
        }


class ConsoleOrSiteSettingsBackend(SiteSettingsEmailBackend):
    """
    Email backend that uses console in DEBUG mode, SMTP otherwise.

    In development (DEBUG=True), emails are printed to console.
    In production (DEBUG=False), emails are sent via SMTP using SiteSettings.
    """

    def __init__(self, **kwargs):
        from django.conf import settings as django_settings

        self.debug_mode = django_settings.DEBUG

        if self.debug_mode:
            # Don't initialize SMTP in debug mode
            self.connection = None
        else:
            super().__init__(**kwargs)

    def send_messages(self, email_messages):
        if self.debug_mode:
            # Print to console in debug mode
            from django.core.mail.backends.console import EmailBackend as ConsoleBackend
            console_backend = ConsoleBackend()
            return console_backend.send_messages(email_messages)
        else:
            return super().send_messages(email_messages)
