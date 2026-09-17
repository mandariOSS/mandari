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


def build_backend(backend: str, **options: Any) -> BaseEmailBackend:
    """
    Backend-Klasse laden und mit den Optionen (host, port, username, …) instanziieren.

    SMTP-Optionen gehen nur an SMTP-Backends: Django 6.1 warnt (7.0: Fehler), wenn ein
    Backend wie locmem oder console unbekannte Argumente bekommt.
    """
    klasse = import_string(backend)
    if not issubclass(klasse, SMTPEmailBackend):
        options = {k: v for k, v in options.items() if k == "fail_silently"}
    instanz: BaseEmailBackend = klasse(**options)
    return instanz


def send_with(backend: BaseEmailBackend, message: Any) -> int:
    """Eine Nachricht über ein bestimmtes Backend senden; liefert die Anzahl gesendeter Mails."""
    with backend:
        return int(backend.send_messages([message]) or 0)
