# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kosten- und Lastgrenzen der KI-Zusammenfassung im Bürgerportal.

Eine Zusammenfassung kostet KI-Tokens, unter Umständen OCR und hält einen Worker minutenlang.
Deshalb gilt: erzeugt wird nur auf ausdrücklichen Klick (POST), höchstens eine Erzeugung je
Vorgang gleichzeitig, je IP-Adresse wenige pro Stunde und Tag und insgesamt ein Tagesbudget.
Gespeicherte Zusammenfassungen kosten nichts; Vorgänge ohne auswertbaren Text werden für einige
Stunden nicht erneut versucht.
"""

from __future__ import annotations

from typing import Any

from django.core.cache import cache
from django.http import HttpRequest

from .. import throttle

LOCK_SECONDS = 10 * 60
NO_TEXT_SECONDS = 6 * 60 * 60


def _lock_key(paper_id: Any) -> str:
    return f"insight:summary:lock:{paper_id}"


def _no_text_key(paper_id: Any) -> str:
    return f"insight:summary:notext:{paper_id}"


def acquire(paper_id: Any) -> bool:
    """Erzeugung für diesen Vorgang beginnen; False, wenn sie schon läuft."""
    return bool(cache.add(_lock_key(paper_id), 1, timeout=LOCK_SECONDS))


def release(paper_id: Any) -> None:
    cache.delete(_lock_key(paper_id))


def remember_no_text(paper_id: Any) -> None:
    cache.set(_no_text_key(paper_id), 1, timeout=NO_TEXT_SECONDS)


def has_no_text(paper_id: Any) -> bool:
    return bool(cache.get(_no_text_key(paper_id)))


def budget_exceeded(request: HttpRequest) -> bool:
    """Grenzen je IP (Stunde, Tag) und das Tagesbudget prüfen; zählt die Erzeugung mit."""
    ip = throttle.client_ip(request)
    if throttle.hit("summary-ip-h", ip, limit=throttle.setting("INSIGHT_SUMMARY_PER_IP_HOUR"), window=throttle.HOUR):
        return True
    if throttle.hit("summary-ip-d", ip, limit=throttle.setting("INSIGHT_SUMMARY_PER_IP_DAY"), window=throttle.DAY):
        return True
    return throttle.hit(
        "summary-all", "alle", limit=throttle.setting("INSIGHT_SUMMARY_DAILY_LIMIT"), window=throttle.DAY
    )
