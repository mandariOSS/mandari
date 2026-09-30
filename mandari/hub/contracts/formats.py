# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigene Prüfung der Kennungs- und Zeitformate.

``jsonschema`` prüft ``date-time``, ``time`` und ``duration`` nur, wenn optionale Pakete
installiert sind (``rfc3339-validator``, ``isoduration``); fehlen sie, gilt das Format als bloße
Anmerkung und jede Zeichenkette besteht. Das Freitextverbot (``rules.py``) lässt diese Formate aber
als Kennung zu. Deshalb prüft das Register sie hier selbst und unabhängig von der Umgebung;
``IDENTIFIER_FORMATS`` sind genau die Formate, die dieses Modul prüft.

Grundlage: RFC 3339 (``full-date``, ``full-time``, ``date-time``, Anhang A ``duration``) und
RFC 4122 (``uuid`` in der Schreibweise mit Bindestrichen).
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from typing import Final

_DATE = r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
_TIME = (
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})(?:\.[0-9]+)?"
    r"(?:[Zz]|[+-](?P<offset_hour>[0-9]{2}):(?P<offset_minute>[0-9]{2}))"
)
_DATE_RE: Final = re.compile(_DATE)
_TIME_RE: Final = re.compile(_TIME)
_DATE_TIME_RE: Final = re.compile(f"{_DATE}[Tt]{_TIME}")
_UUID_RE: Final = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# RFC 3339, Anhang A: duration = "P" (dur-date / dur-time / dur-week)
_DUR_TIME = r"T(?:[0-9]+H(?:[0-9]+M(?:[0-9]+S)?)?|[0-9]+M(?:[0-9]+S)?|[0-9]+S)"
_DUR_DATE = r"(?:[0-9]+Y(?:[0-9]+M(?:[0-9]+D)?)?|[0-9]+M(?:[0-9]+D)?|[0-9]+D)"
_DURATION_RE: Final = re.compile(f"P(?:{_DUR_DATE}(?:{_DUR_TIME})?|{_DUR_TIME}|[0-9]+W)")


def _date_valid(match: re.Match[str]) -> bool:
    try:
        datetime.date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError:
        return False
    return True


def _time_valid(match: re.Match[str]) -> bool:
    # Sekunde 60 für Schaltsekunden (RFC 3339, Abschnitt 5.7).
    if int(match["hour"]) > 23 or int(match["minute"]) > 59 or int(match["second"]) > 60:
        return False
    if match["offset_hour"] is not None:
        return int(match["offset_hour"]) <= 23 and int(match["offset_minute"]) <= 59
    return True


def is_uuid(instance: object) -> bool:
    return not isinstance(instance, str) or _UUID_RE.fullmatch(instance) is not None


def is_date(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    match = _DATE_RE.fullmatch(instance)
    return match is not None and _date_valid(match)


def is_time(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    match = _TIME_RE.fullmatch(instance)
    return match is not None and _time_valid(match)


def is_date_time(instance: object) -> bool:
    if not isinstance(instance, str):
        return True
    match = _DATE_TIME_RE.fullmatch(instance)
    return match is not None and _date_valid(match) and _time_valid(match)


def is_duration(instance: object) -> bool:
    return not isinstance(instance, str) or _DURATION_RE.fullmatch(instance) is not None


#: Format → Prüfung; Werte anderer Typen als Zeichenketten bestehen (wie in JSON Schema vorgesehen).
FORMAT_CHECKS: Final[dict[str, Callable[[object], bool]]] = {
    "uuid": is_uuid,
    "date": is_date,
    "time": is_time,
    "date-time": is_date_time,
    "duration": is_duration,
}

#: Formate, die als Kennung oder Zeitangabe gelten und damit kein Freitext sind.
IDENTIFIER_FORMATS: Final[frozenset[str]] = frozenset(FORMAT_CHECKS)
