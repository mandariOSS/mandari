# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bausteine des kanonischen RIS-Modells (OParl 1.1).

Eine Stelle für das, was jede Abbildung und jede Ausgabe gleich machen muss: Typ-URLs, Datum und
Zeitpunkt, leere Felder weglassen, gekürzte Objekte für Gelöschtes. Die Abbildungen des RIS-Bestands
(``hub.ris.mapping.bestand``) und der Session-Objekte (``hub.ris.mapping.session``) nutzen dieselben
Funktionen.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Final

from django.utils import timezone
from mandari_oparl.extensions import (
    IMPLEMENTATION_LABELS,
    RESULT_LABELS,
    ROLL_CALL_VOTE_LABELS,
    VOTING_METHOD_LABELS,
)

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


# -- Beschlussfassung (Erweiterungen des kanonischen Modells, Issue #525) --------------------------------------------
#
# Aus den Spalten des RIS-Bestands (``mandari_oparl.extensions``) entstehen dieselben Erweiterungen, die mandari Session
# ausgibt: Die offene Schnittstelle gibt sie weiter, das Bürgerportal liest sie über die Lese-Fassade.


def vote_extension(item: Any) -> Objekt | None:
    """``mandari:vote`` eines Tagesordnungspunkts: Art, Ergebnis und Summen; ``None`` ohne Abstimmung."""
    if not item.vote_method and not item.vote_result:
        return None
    return clean(
        {
            "method": item.vote_method,
            "methodLabel": VOTING_METHOD_LABELS.get(item.vote_method or ""),
            "result": item.vote_result,
            "resultLabel": RESULT_LABELS.get(item.vote_result or ""),
            "yes": item.votes_yes,
            "no": item.votes_no,
            "abstain": item.votes_abstain,
        }
    )


def roll_call_extension(item: Any) -> list[Objekt] | None:
    """``mandari:rollCall``: Einzelstimmen, ausschließlich bei namentlicher Abstimmung."""
    if item.vote_method != "roll_call" or not isinstance(item.roll_call, list):
        return None
    entries = [
        {"name": entry["name"], "vote": entry["vote"], "voteLabel": ROLL_CALL_VOTE_LABELS[entry["vote"]]}
        for entry in item.roll_call
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry.get("vote") in ROLL_CALL_VOTE_LABELS
    ]
    return entries or None


def implementation_extension(item: Any) -> Objekt | None:
    """``mandari:implementation``: veröffentlichter Umsetzungsstand des Beschlusses; ``None`` ohne Angabe."""
    if not item.implementation_status:
        return None
    return clean(
        {
            "status": item.implementation_status,
            "statusLabel": IMPLEMENTATION_LABELS.get(item.implementation_status),
            "deadline": iso_date(item.implementation_deadline),
            "note": item.implementation_public_note,
            "modified": iso(item.implementation_modified),
        }
    )


def protocol_approval_extension(meeting: Any, approved_in: str | None = None) -> Objekt | None:
    """
    ``mandari:protocolApproval`` einer Sitzung: Weg, Tag und (als Adresse ``approved_in``, sofern bekannt) die
    genehmigende Sitzung; ``None`` ohne Angabe.
    """
    if not meeting.protocol_approval_mode:
        return None
    return clean(
        {
            "mode": meeting.protocol_approval_mode,
            "date": iso_date(meeting.protocol_approved_on),
            "meeting": approved_in,
        }
    )
