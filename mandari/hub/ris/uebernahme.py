# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Übernahme von OParl-Objekten in den RIS-Bestand: welche Spalte welchen Wert bekommt.

Eine Stelle für die Schreiber des Bestands in Django, die OParl-Objekte übernehmen: den Spiegel der
Session-Schnittstelle (``insight_sync.session_mirror``) und den RIS-Projektor für Session-Mandanten
(``hub.projections.ris_session``, Issue #536). Beide lesen dieselben Objekte – die Abbildung
``hub.ris.mapping.session`` – und schreiben damit dieselben Spalten. Der Ingestor rechnet für fremde Quellen
ebenso (``ingestor/src/sync/processor.py``).

Je Objekttyp eine Funktion. Sie liefert die fachlichen Spalten ohne Bezüge auf andere Zeilen (Kommune, Sitzung,
Vorlage, Gremium, Person) und ohne die Spalten, die jede Zeile trägt (``base``: Zeitstempel der Quelle, Rohdaten,
Löschmarkierung). ``SPALTEN`` nennt je Typ genau die Schlüssel, die die Funktion liefert.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any, Final

from mandari_oparl.extensions import AGENDA_ITEM_COLUMNS, MEETING_COLUMNS, agenda_item_columns, meeting_columns

Daten = Mapping[str, Any]


def parse_dt(value: Any) -> datetime | None:
    """Zeitpunkt lesen; ein reines Datum (OParl ``File.date``) gilt wie im Ingestor als Mitternacht UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def location_text(data: Daten) -> tuple[str | None, str | None]:
    """
    Ort und Anschrift einer Sitzung als Text – gleiche Abbildung wie der Ingestor
    (``ingestor/src/sync/processor.py``, ``process_meeting``).

    Der Ort gehört zur Sitzung und steht im Bestand als Text an ihr, nicht als eigenes Location-Objekt;
    so verschwindet er mit der Sitzung. Die Textfelder ``mandari:location*`` gelten, wo sie vorhanden
    sind (abgekündigt); ohne sie ergibt das eingebettete Location-Objekt denselben Text: Gebäude vor
    Raum, Anschrift mit Postleitzahl und Ort.
    """
    location = data.get("location")
    if not isinstance(location, Mapping):
        location = {}
    locality = " ".join(part for part in (location.get("postalCode"), location.get("locality")) if part)
    name = (
        data.get("mandari:locationName")
        or data.get("mandari:locationRoom")
        or location.get("description")
        or location.get("room")
    )
    address = data.get("mandari:locationAddress") or ", ".join(
        part for part in (location.get("streetAddress"), locality) if part
    )
    return name or None, address or None


def base(data: Daten) -> dict[str, Any]:
    """Spalten jeder Zeile: Zeitstempel der Quelle, Rohdaten; eine übernommene Zeile ist nicht (mehr) gelöscht."""
    return {
        "oparl_created": parse_dt(data.get("created")),
        "oparl_modified": parse_dt(data.get("modified")),
        "raw_json": data,
        "deleted": False,
        "deleted_at": None,
        "deletion_reason": None,
    }


def body(data: Daten) -> dict[str, Any]:
    return {
        "name": data.get("name") or "Unbekannt",
        "short_name": data.get("shortName"),
        "website": data.get("website"),
        "classification": data.get("classification"),
        "organization_list_url": data.get("organization"),
        "person_list_url": data.get("person"),
        "meeting_list_url": data.get("meeting"),
        "paper_list_url": data.get("paper"),
        "membership_list_url": data.get("membership"),
    }


def legislative_term(data: Daten) -> dict[str, Any]:
    return {
        "name": data.get("name"),
        "start_date": parse_date(data.get("startDate")),
        "end_date": parse_date(data.get("endDate")),
    }


def organization(data: Daten) -> dict[str, Any]:
    return {
        "name": data.get("name"),
        "short_name": data.get("shortName"),
        "organization_type": data.get("organizationType"),
        "classification": data.get("classification"),
        "start_date": parse_date(data.get("startDate")),
        "end_date": parse_date(data.get("endDate")),
        "website": data.get("website"),
    }


def person(data: Daten) -> dict[str, Any]:
    title = data.get("title")
    if isinstance(title, list):
        title = " ".join(str(t) for t in title if t)
    email = data.get("email")
    if isinstance(email, list):
        email = email[0] if email else None
    return {
        "name": data.get("name"),
        "family_name": data.get("familyName"),
        "given_name": data.get("givenName"),
        "title": title,
        "email": email,
    }


def membership(data: Daten) -> dict[str, Any]:
    return {
        "role": data.get("role"),
        "voting_right": bool(data.get("votingRight", True)),
        "start_date": parse_date(data.get("startDate")),
        "end_date": parse_date(data.get("endDate")),
    }


def meeting(data: Daten) -> dict[str, Any]:
    location_name, location_address = location_text(data)
    return {
        "name": data.get("name"),
        "meeting_state": data.get("meetingState"),
        "cancelled": bool(data.get("cancelled", False)),
        "start": parse_dt(data.get("start")),
        "end": parse_dt(data.get("end")),
        "location_name": location_name,
        "location_address": location_address,
        # Genehmigung der Niederschrift (Issue #525)
        **meeting_columns(data),
    }


def agenda_item(data: Daten) -> dict[str, Any]:
    return {
        "number": data.get("number"),
        "order": data.get("order"),
        "name": data.get("name"),
        "public": bool(data.get("public", True)),
        "result": data.get("result"),
        "resolution_text": data.get("resolutionText"),
        # Beschlussfassung: Nummer, Abstimmung, Einzelstimmen, Umsetzung (Issue #525)
        **agenda_item_columns(data),
    }


def paper(data: Daten) -> dict[str, Any]:
    return {
        "name": data.get("name"),
        "reference": data.get("reference"),
        "paper_type": data.get("paperType"),
        "date": parse_date(data.get("date")),
    }


def file(data: Daten) -> dict[str, Any]:
    return {
        "name": data.get("name"),
        "file_name": data.get("fileName"),
        "mime_type": data.get("mimeType"),
        "size": data.get("size"),
        "access_url": data.get("accessUrl"),
        "download_url": data.get("downloadUrl"),
        "file_date": parse_dt(data.get("date")),
    }


def consultation(data: Daten) -> dict[str, Any]:
    meeting_ref = data.get("meeting")
    item_ref = data.get("agendaItem")
    return {
        "paper_external_id": data.get("paper"),
        "meeting_external_id": meeting_ref if isinstance(meeting_ref, str) else None,
        "agenda_item_external_id": item_ref if isinstance(item_ref, str) else None,
        "role": data.get("role"),
        "authoritative": bool(data.get("authoritative", False)),
    }


#: Objekttyp (Segment der OParl-Adresse) -> Spalten aus einem OParl-Objekt
FUNKTIONEN: Final[dict[str, Callable[[Daten], dict[str, Any]]]] = {
    "body": body,
    "legislativeterm": legislative_term,
    "organization": organization,
    "person": person,
    "membership": membership,
    "meeting": meeting,
    "agendaitem": agenda_item,
    "paper": paper,
    "file": file,
    "consultation": consultation,
}

#: Objekttyp -> Namen der Spalten, die ``FUNKTIONEN[typ]`` liefert (in dieser Reihenfolge)
SPALTEN: Final[dict[str, tuple[str, ...]]] = {
    "body": (
        "name",
        "short_name",
        "website",
        "classification",
        "organization_list_url",
        "person_list_url",
        "meeting_list_url",
        "paper_list_url",
        "membership_list_url",
    ),
    "legislativeterm": ("name", "start_date", "end_date"),
    "organization": (
        "name",
        "short_name",
        "organization_type",
        "classification",
        "start_date",
        "end_date",
        "website",
    ),
    "person": ("name", "family_name", "given_name", "title", "email"),
    "membership": ("role", "voting_right", "start_date", "end_date"),
    "meeting": (
        "name",
        "meeting_state",
        "cancelled",
        "start",
        "end",
        "location_name",
        "location_address",
        *MEETING_COLUMNS,
    ),
    "agendaitem": ("number", "order", "name", "public", "result", "resolution_text", *AGENDA_ITEM_COLUMNS),
    "paper": ("name", "reference", "paper_type", "date"),
    "file": ("name", "file_name", "mime_type", "size", "access_url", "download_url", "file_date"),
    "consultation": (
        "paper_external_id",
        "meeting_external_id",
        "agenda_item_external_id",
        "role",
        "authoritative",
    ),
}
