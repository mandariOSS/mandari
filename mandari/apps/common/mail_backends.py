# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mail-Backends zur Laufzeit aufbauen (Django ≥ 6.1, Issue #80).

Django 6.1 ersetzt ``get_connection()`` und das ``connection``-Argument durch die
``MAILERS``-Konfiguration mit festen Aliassen. mandari braucht daneben Backends mit
Zugangsdaten aus der Datenbank (SiteSettings, organisationseigenes SMTP), die erst zur
Laufzeit bekannt sind. Dafür wird die Backend-Klasse direkt instanziiert – das ist die
von Django vorgesehene, nicht deprecatete Schnittstelle (``BaseEmailBackend`` mit
``send_messages``).
"""

from __future__ import annotations

from typing import Any

from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.smtp import EmailBackend as SMTPEmailBackend
from django.utils.module_loading import import_string

SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
#: Alias für zur Laufzeit aufgebaute Backends (SiteSettings, Organisations-SMTP)
LAUFZEIT_ALIAS = "laufzeit"

# Djangos SMTP-Backend liest für jede Option, die None ist, das zugehörige EMAIL_*-Setting –
# und das wirft neben MAILERS einen AttributeError. Deshalb bekommt jedes SMTP-Backend
# vollständige, nie leere Optionen (Deploy-Rückfall 18.09.2026).
SMTP_DEFAULTS: dict[str, Any] = {
    "port": 587,
    "username": "",
    "password": "",
    "use_tls": True,
    "use_ssl": False,
    "timeout": 30,
    "ssl_keyfile": "",
    "ssl_certfile": "",
}


def smtp_options(**options: Any) -> dict[str, Any]:
    """Vollständiger Optionssatz für ein SMTP-Backend; ``host`` ist Pflicht, None-Werte werden ersetzt."""
    voll = dict(SMTP_DEFAULTS)
    voll.update({k: v for k, v in options.items() if v is not None})
    if not voll.get("host"):
        raise ValueError("SMTP-Host fehlt: weder SiteSettings noch EMAIL_HOST gesetzt")
    return voll


def build_backend(backend: str, **options: Any) -> BaseEmailBackend:
    """
    Backend-Klasse laden und mit den Optionen (host, port, username, …) instanziieren.

    SMTP-Optionen gehen nur an SMTP-Backends: Django 6.1 warnt (7.0: Fehler), wenn ein
    Backend wie locmem oder console unbekannte Argumente bekommt.
    """
    klasse = import_string(backend)
    if issubclass(klasse, SMTPEmailBackend):
        options = smtp_options(**options)
    else:
        options = {k: v for k, v in options.items() if k == "fail_silently"}
    # Mit Alias läuft Django ≥ 6.1 im MAILERS-Modus: keine EMAIL_*-Settings, keine Deprecation.
    options.setdefault("alias", LAUFZEIT_ALIAS)
    instanz: BaseEmailBackend = klasse(**options)
    return instanz


def send_with(backend: BaseEmailBackend, message: Any) -> int:
    """Eine Nachricht über ein bestimmtes Backend senden; liefert die Anzahl gesendeter Mails."""
    with backend:
        return int(backend.send_messages([message]) or 0)
