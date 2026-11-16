# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bekanntes Gerät: Die Sperre nach Fehlversuchen je Konto trifft keine Browser, die das Konto schon nachgewiesen hat.

Die Anmeldung zählt Fehlversuche je Konto über alle Adressen hinweg. Das bremst verteiltes Raten, lässt sich aber
von jedem auslösen, der nur die E-Mail-Adresse kennt. Deshalb bekommt ein Browser nach einer abgeschlossenen
Anmeldung und nach einem eingelösten Link „Passwort zurücksetzen“ ein signiertes Cookie. Mit ihm gilt für dieses
Konto statt der Zählung über alle Adressen eine eigene, gleich hohe Zählung für die bekannten Geräte des Kontos; die
Schwellen je Adresse und Netz gelten unverändert.

Die bekannten Geräte eines Kontos teilen sich diese Zählung. Zählte jedes Cookie für sich, vervielfachte jede weitere
Anmeldung die Versuche: Wer das Passwort einmal kannte, sammelte Cookies und riete nach einem Passwortwechsel mit
jedem davon bis zur Schwelle.

Das Cookie meldet niemanden an und ersetzt weder Passwort noch zweiten Faktor. Es ist an das Konto gebunden (Hash der
Kennung, nicht die Adresse) und an den Hauptschlüssel der Installation (Signatur). Wer es kopiert oder mehrere davon
hat, gewinnt je Zeitfenster zusammen so viele Versuche, wie die Sperre je Konto ohnehin zulässt.
"""

from __future__ import annotations

import contextlib
import secrets
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils.crypto import constant_time_compare

from . import security_audit

COOKIE_NAME = "mandari_geraet"
COOKIE_SALT = "accounts.bekanntes-geraet"
COOKIE_MAX_AGE = 60 * 60 * 24 * 180


def device_id(request: Any, identifier: str) -> str:
    """Kennung dieses Geräts, wenn es das Konto zu ``identifier`` schon nachgewiesen hat; sonst ``""``."""
    if not identifier:
        return ""
    value = str(request.get_signed_cookie(COOKIE_NAME, default="", salt=COOKIE_SALT, max_age=COOKIE_MAX_AGE) or "")
    account, _, device = value.partition(":")
    if device and constant_time_compare(account, security_audit.identifier_hash(identifier)):
        return device
    return ""


def remember(request: Any, response: Any, identifier: str) -> Any:
    """Gerät für das Konto vormerken (nach abgeschlossener Anmeldung oder eingelöstem Rücksetzlink)."""
    device = device_id(request, identifier) or secrets.token_urlsafe(16)
    response.set_signed_cookie(
        COOKIE_NAME,
        f"{security_audit.identifier_hash(identifier)}:{device}",
        salt=COOKIE_SALT,
        max_age=COOKIE_MAX_AGE,
        # Nur an die Seiten der Anmeldung, nie an Skripte
        path=reverse("accounts:login").removesuffix("login/"),
        secure=bool(settings.SESSION_COOKIE_SECURE),
        httponly=True,
        samesite="Lax",
    )
    return response


def _failure_key(identifier: str) -> str:
    # Je Konto, nicht je Gerät: Alle bekannten Geräte des Kontos teilen sich die Versuche
    return f"login-failures:known-devices:{security_audit.identifier_hash(identifier)}"


def failures(identifier: str) -> int:
    """Fehlversuche aller bekannten Geräte des Kontos zu ``identifier`` im laufenden Zeitfenster."""
    return int(cache.get(_failure_key(identifier), 0))


def count_failure(identifier: str, window_seconds: int) -> None:
    """Fehlversuch eines bekannten Geräts zählen (gemeinsam für alle bekannten Geräte des Kontos)."""
    key = _failure_key(identifier)
    cache.add(key, 0, window_seconds)
    with contextlib.suppress(ValueError):  # zwischenzeitlich abgelaufen
        cache.incr(key)
