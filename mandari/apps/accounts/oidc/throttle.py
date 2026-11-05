# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratenbegrenzung am Token-Endpunkt (Issue #1206).

Derselbe Mechanismus wie bei OParl-Schnittstelle und Anmeldung: ein Zähler je Adresse und fester Minute im
Django-Cache, IPv6 je /64-Netz (ein Anschluss verfügt meist über ein ganzes /64). Eine Abweisung steht wie eine
gedrosselte Anmeldung im Sicherheitsprotokoll, höchstens einmal je Adresse und Fenster
(:func:`apps.accounts.security_audit.record_throttled`).
"""

from __future__ import annotations

import hashlib
import ipaddress
import time

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest

from ..two_factor_policy import client_ip

WINDOW_SECONDS = 60
#: Schritt im Sicherheitsprotokoll
STEP_TOKEN_ENDPOINT = "OIDC-Token-Endpunkt"


def limit_per_minute() -> int:
    """Anfragen je Minute und Adresse (``OIDC_TOKEN_RATE_LIMIT``, 0 = aus)."""
    return int(getattr(settings, "OIDC_TOKEN_RATE_LIMIT", 30) or 0)


def scope_of(ip: str) -> str:
    """Zählbereich: IPv4 je Adresse, IPv6 je /64-Netz."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip or "unbekannt"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def seconds_until_next_window() -> int:
    return WINDOW_SECONDS - int(time.time()) % WINDOW_SECONDS


def exceeded(request: HttpRequest) -> bool:
    """Diese Anfrage zählen; ``True``, wenn die Adresse ihr Kontingent der laufenden Minute überschreitet."""
    limit = limit_per_minute()
    if limit <= 0:
        return False
    window = int(time.time() // WINDOW_SECONDS)
    marker = f"{scope_of(client_ip(request))}|{window}"
    key = "oidc-token:rl:" + hashlib.sha256(marker.encode()).hexdigest()
    if cache.add(key, 1, timeout=WINDOW_SECONDS * 2):
        return False
    try:
        count = int(cache.incr(key))
    except ValueError:  # Schlüssel zwischenzeitlich abgelaufen
        cache.add(key, 1, timeout=WINDOW_SECONDS * 2)
        return False
    return count > limit
