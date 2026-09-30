# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bausteine des kanonischen RIS-Modells (OParl 1.1).

Eine Stelle für das, was jede Abbildung und jede Ausgabe gleich machen muss: Typ-URLs, Datum und
Zeitpunkt, leere Felder weglassen, gekürzte Objekte für Gelöschtes. Aggregator (``oparl_api``) und
die Abbildung der Session-Objekte (``hub.ris.mapping.session``) nutzen dieselben Funktionen.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Final

from django.utils import timezone

#: Schema-Basis der OParl-1.1-Spezifikation
SCHEMA_BASE: Final = "https://schema.oparl.org/1.1"

#: Objekttyp (URL-Segment) -> Schema-Name
TYPE_SCHEMA: Final[dict[str, str]] = {
    "system": "System",
    "body": "Body",
    "organization": "Organization",
    "person": "Person",
    "membership": "Membership",
    "meeting": "Meeting",
    "agendaitem": "AgendaItem",
    "paper": "Paper",
    "consultation": "Consultation",
    "file": "File",
    "location": "Location",
    "legislativeterm": "LegislativeTerm",
}

#: OParl 1.1, ``Organization.organizationType``: „Mögliche Werte sind …“ – genau diese sieben
ORGANIZATION_TYPES: Final[tuple[str, ...]] = (
    "Gremium",
    "Partei",
    "Fraktion",
    "Verwaltungsbereich",
    "externes Gremium",
    "Institution",
    "Sonstiges",
)

Objekt = dict[str, Any]


def schema_type(kind: str) -> str:
    """Schema-URL des Objekttyps (Wert des ``type``-Felds)."""
    return f"{SCHEMA_BASE}/{TYPE_SCHEMA[kind]}"


def iso(dt: datetime | None) -> str | None:
    """Zeitpunkt -> ISO 8601 mit Zeitzone (None-sicher)."""
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def iso_date(d: date | None) -> str | None:
    """Datum -> ISO 8601 (None-sicher)."""
    return d.isoformat() if d else None


def iso_day(dt: datetime | None) -> str | None:
    """
    Zeitpunkt -> Datum ``yyyy-mm-dd`` (None-sicher), für Felder vom Typ ``date`` wie ``File.date``.

    Es gilt der Tag in der Zeitzone der Installation.
    """
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(timezone.get_current_timezone()).date().isoformat()


def clean(data: Objekt) -> Objekt:
    """Leere optionale Felder entfernen (``None``, leere Listen, leere Texte)."""
    return {key: value for key, value in data.items() if value is not None and value != [] and value != ""}


def as_list(value: Any) -> list[Any] | None:
    """Einzelwert in eine Liste hüllen (OParl erwartet z. B. ``email`` und ``title`` als Liste)."""
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return value
    return [value]


def tombstone(object_id: str, kind: str, created: datetime | None, modified: datetime | None) -> Objekt:
    """
    Gekürztes Objekt für Gelöschtes oder nicht mehr Öffentliches (OParl 1.1 §2.8).

    Nur ``id``, ``type``, ``created``, ``modified`` und ``deleted``; ``modified`` ist der Zeitpunkt
    der Löschung. Inhalte entfallen vollständig.
    """
    return {
        "id": object_id,
        "type": schema_type(kind),
        "created": iso(created),
        "modified": iso(modified),
        "deleted": True,
    }
