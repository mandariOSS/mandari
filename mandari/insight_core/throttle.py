# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Drosselung öffentlicher Insight-Endpunkte (Zähler mit festem Zeitfenster im Django-Cache).

Die Grenzen stehen als Einstellungen mit Vorgabewerten bereit (``setting``); ein Wert ``0``
schaltet die jeweilige Grenze ab. Schlüssel mit personenbezogenen Teilen (E-Mail-Adressen)
landen nur als Hash im Cache.
"""

from __future__ import annotations

import hashlib
import time

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest

#: Vorgaben, überschreibbar in den Einstellungen
DEFAULTS: dict[str, int] = {
    # KI-Zusammenfassung
    "INSIGHT_SUMMARY_PER_IP_HOUR": 6,
    "INSIGHT_SUMMARY_PER_IP_DAY": 20,
    "INSIGHT_SUMMARY_DAILY_LIMIT": 300,
    # Formulare, die E-Mails auslösen (Kontakt, Abos, Beschluss-Abos, Ratsfragen)
    "INSIGHT_MAILS_PER_IP_HOUR": 10,
    "INSIGHT_MAILS_PER_ADDRESS_DAY": 3,
    # Kachel-Proxy (nur Abrufe bei OpenStreetMap, Kacheln aus dem Cache sind frei)
    "INSIGHT_TILE_FETCHES_PER_IP_MINUTE": 300,
    "INSIGHT_TILE_FETCHES_PER_MINUTE": 1200,
    "INSIGHT_TILE_CACHE_MAX_TILES": 500_000,
    # Dateivorschau (nur Abrufe beim Quell-RIS, Dateien aus dem Cache sind frei)
    "FILE_PROXY_FETCHES_PER_IP_MINUTE": 20,
    "FILE_PROXY_MAX_CONCURRENT": 4,
    "FILE_PROXY_TOTAL_SECONDS": 60,
}

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


def setting(name: str) -> int:
    return int(getattr(settings, name, DEFAULTS[name]))


def client_ip(request: HttpRequest) -> str:
    """Client-Adresse; der vorgelagerte Reverse-Proxy setzt ``X-Forwarded-For``."""
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR", ""))
    if forwarded:
        return forwarded.split(",")[0].strip()
    return str(request.META.get("REMOTE_ADDR", "") or "unbekannt")


def _key(scope: str, key: str, window: int) -> str:
    digest = hashlib.sha256(key.strip().lower().encode()).hexdigest()[:32]
    return f"insight:rl:{scope}:{digest}:{int(time.time() // window)}"


def hit(scope: str, key: str, *, limit: int, window: int) -> bool:
    """Einen Aufruf zählen. True, wenn damit die Grenze überschritten ist (``limit <= 0``: nie)."""
    if limit <= 0:
        return False
    cache_key = _key(scope, key, window)
    if cache.add(cache_key, 1, timeout=window + 5):
        return False  # erster Aufruf im Zeitfenster, limit ist mindestens 1
    try:
        count = int(cache.incr(cache_key))
    except ValueError:  # zwischen add und incr abgelaufen
        cache.set(cache_key, 1, timeout=window + 5)
        count = 1
    return count > limit


def peek(scope: str, key: str, *, window: int) -> int:
    """Aktueller Zählerstand ohne zu zählen."""
    return int(cache.get(_key(scope, key, window)) or 0)


def mail_ip_exceeded(request: HttpRequest) -> bool:
    """Formulare, die E-Mails auslösen: Grenze je IP-Adresse und Stunde überschritten? (zählt mit)"""
    return hit("mail-ip", client_ip(request), limit=setting("INSIGHT_MAILS_PER_IP_HOUR"), window=HOUR)


def mail_address_exceeded(address: str) -> bool:
    """
    Grenze je Empfängeradresse und Tag überschritten? (zählt mit)

    Bestätigungsmails gehen an Adressen, die jemand in ein Formular eingetragen hat. Ohne diese
    Grenze ließe sich das Portal als Mailbombe gegen fremde Postfächer nutzen.
    """
    return hit("mail-an", address, limit=setting("INSIGHT_MAILS_PER_ADDRESS_DAY"), window=DAY)
