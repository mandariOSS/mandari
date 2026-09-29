# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zweiter Faktor: Zählung und Protokoll der Code-Eingaben, gemeinsame Bestätigung der Einrichtung.

Alle Wege, auf denen ein Code der Authenticator-App geprüft wird – zweiter Anmeldeschritt,
Einrichtung unter ``/accounts/zwei-faktor/einrichten/`` und Einrichtung im Work-Profil –, nutzen
dieselbe Zählung (``LoginAttempt`` mit ``invalid_2fa`` je Konto) und dasselbe Protokoll
(``LoginAttempt`` und Sicherheits- bzw. Mandantenprotokoll über :mod:`apps.accounts.security_audit`).
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from typing import Any

from django.utils import timezone

from .models import LoginAttempt
from .services import TwoFactorService

#: Fehlversuche je Konto im Zeitfenster, ab denen weitere Eingaben abgewiesen werden
MAX_FAILURES = 5
FAILURE_WINDOW_MINUTES = 15
FAILURE_REASON = "invalid_2fa"

# Ergebnisse von confirm_setup
CONFIRMED = "confirmed"
INVALID = "invalid"
LOCKED = "locked"


def recent_failures(user: Any) -> int:
    """Falsche Codes des Kontos im Zeitfenster (über alle Wege)."""
    return LoginAttempt.objects.filter(
        email=user.email,
        was_successful=False,
        failure_reason=FAILURE_REASON,
        timestamp__gte=timezone.now() - timedelta(minutes=FAILURE_WINDOW_MINUTES),
    ).count()


def is_locked(user: Any) -> bool:
    return recent_failures(user) >= MAX_FAILURES


def client_ip(request: Any) -> str:
    """Adresse wie bei der Anmeldung gezählt (erster Eintrag aus X-Forwarded-For, sonst REMOTE_ADDR)."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return str(forwarded).split(",")[0].strip()
    return str(request.META.get("REMOTE_ADDR", ""))


def log_attempt(request: Any, user: Any, *, success: bool) -> None:
    """Code-Eingabe festhalten; ein Fehler beim Protokollieren verhindert nie den Vorgang."""
    with contextlib.suppress(Exception):
        LoginAttempt.objects.create(
            email=user.email,
            ip_address=client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
            was_successful=success,
            failure_reason="" if success else FAILURE_REASON,
        )
    if not success:
        # Revisionsprotokoll (Issue #221); fehlertolerant, die erfolgreiche Anmeldung meldet das Signal
        from .security_audit import log_second_factor_failed

        log_second_factor_failed(request, user)


def confirm_setup(request: Any, user: Any, code: str) -> str:
    """Einrichtung mit dem ersten Code bestätigen: ``CONFIRMED``, ``INVALID`` oder ``LOCKED``.

    Gesperrt wird vor der Prüfung (auch ein richtiger Code hilft dann erst nach Ablauf des
    Zeitfensters); ein falscher Code wird gezählt und protokolliert.
    """
    if is_locked(user):
        return LOCKED
    if TwoFactorService().confirm_2fa(user, code):
        return CONFIRMED
    log_attempt(request, user, success=False)
    return INVALID
