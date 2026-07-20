# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsamer ICS-/iCalendar-Baustein (Issue #29).

Erzeugt RFC-5545-konforme Kalenderanhänge (VCALENDAR/VEVENT) für
Einladungs-E-Mails — wiederverwendbar für Session-Ladungen und
Work-Fraktionssitzungen.
"""

from datetime import UTC, datetime

from django.utils import timezone

PRODID = "-//mandari//Sitzungsdienst//DE"


def _escape(value: str) -> str:
    """Text nach RFC 5545 escapen (Backslash, Semikolon, Komma, Zeilenumbruch)."""
    if not value:
        return ""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Zeilen länger als 75 Oktette falten (RFC 5545, Fortsetzung mit Leerzeichen)."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts = []
    current = b""
    for char in line:
        char_bytes = char.encode("utf-8")
        limit = 75 if not parts else 74  # Fortsetzungszeilen beginnen mit Leerzeichen
        if len(current) + len(char_bytes) > limit:
            parts.append(current.decode("utf-8"))
            current = char_bytes
        else:
            current += char_bytes
    if current:
        parts.append(current.decode("utf-8"))
    return parts[0] + "\r\n" + "\r\n".join(" " + p for p in parts[1:])


def _format_dt(value: datetime) -> str:
    """Zeitstempel als UTC im ICS-Format (YYYYMMDDTHHMMSSZ)."""
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_ics_event(
    *,
    uid: str,
    summary: str,
    start: datetime,
    end: datetime | None = None,
    description: str = "",
    location: str = "",
    organizer_name: str = "",
    organizer_email: str = "",
    method: str = "PUBLISH",
    sequence: int = 0,
) -> bytes:
    """
    Einzelnes VEVENT als vollständige ICS-Datei erzeugen.

    Args:
        uid: stabile, weltweit eindeutige Ereignis-ID (z. B. "<meeting-uuid>@mandari")
        summary: Titel des Termins
        start/end: Beginn/Ende (aware oder naiv in lokaler Zeit)
        description: Beschreibungstext
        location: Ort
        organizer_name/organizer_email: optionaler Organisator
        method: PUBLISH (Standard) oder REQUEST
        sequence: Versionszähler bei Aktualisierungen

    Returns:
        bytes: ICS-Inhalt (UTF-8, CRLF-Zeilenenden)
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{_escape(uid)}",
        f"DTSTAMP:{_format_dt(timezone.now())}",
        f"DTSTART:{_format_dt(start)}",
    ]
    if end is not None:
        lines.append(f"DTEND:{_format_dt(end)}")
    lines.append(f"SUMMARY:{_escape(summary)}")
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    if organizer_email:
        cn = f";CN={_escape(organizer_name)}" if organizer_name else ""
        lines.append(f"ORGANIZER{cn}:mailto:{organizer_email}")
    lines.extend(
        [
            f"SEQUENCE:{sequence}",
            "STATUS:CONFIRMED",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    return ("\r\n".join(_fold(line) for line in lines) + "\r\n").encode("utf-8")
