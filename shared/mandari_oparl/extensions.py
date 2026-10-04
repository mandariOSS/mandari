# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussfassung im kanonischen RIS-Modell: Erweiterungen am Tagesordnungspunkt und an der Sitzung.

OParl 1.1 kennt weder Abstimmung noch Beschlusskontrolle noch die Genehmigung der Niederschrift. Das kanonische
Modell führt sie als gekennzeichnete Erweiterungen im Namensraum ``mandari:`` (ADR
``docs/adr/20260929-kanonisches-modell.md``, Issue #525); mandari Session liefert sie in ihrer
OParl-Schnittstelle:

- ``AgendaItem`` ``mandari:resolutionNumber``: Beschlussnummer.
- ``AgendaItem`` ``mandari:vote``: Abstimmung als Summen ``{method, result, yes, no, abstain}``
  (dazu ``methodLabel``/``resultLabel``). Eine Abstimmung je Tagesordnungspunkt; im Bestand steht sie deshalb
  am Punkt. Ihre kanonische Kennung leitet sich aus der Adresse des Punkts ab (Zusatz ``voting``, wie in
  ``ris.voting.recorded``).
- ``AgendaItem`` ``mandari:rollCall``: Einzelstimmen ``[{name, vote}]``, ausschließlich bei namentlicher
  Abstimmung.
- ``AgendaItem`` ``mandari:implementation``: Umsetzungsstand des Beschlusses (Beschlusskontrolle)
  ``{status, deadline, note, modified}``, nur wenn die Verwaltung ihn veröffentlicht.
- ``Meeting`` ``mandari:protocolApproval``: Genehmigung der veröffentlichten Niederschrift
  ``{mode, date, meeting}``.

``agenda_item_columns()`` und ``meeting_columns()`` übersetzen die Erweiterungen eines OParl-Objekts in die
Spalten des RIS-Bestands; Ingestor und Django (Spiegel, Datenmigration) nutzen dieselben Funktionen. Was nicht
passt – unbekannter Code, falscher Typ, zu langer Wert, Einzelstimmen ohne namentliche Abstimmung –, wird
verworfen statt übernommen: Eine fremde Quelle schreibt so nichts Unerwartetes in den Bestand.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, Final

#: Abstimmungsart (Session ``SessionAgendaItem.VOTING_METHOD_CHOICES``, Vertrag ``ris.voting.recorded``)
VOTING_METHOD_LABELS: Final[dict[str, str]] = {
    "summary": "Nur Summen",
    "open": "Offen (einzeln erfasst)",
    "roll_call": "Namentlich",
    "secret": "Geheim",
}
#: Ergebnis der Beschlussfassung (Vertrag ``ris.resolution.adopted``); „ausstehend“ ist kein Ergebnis
RESULT_LABELS: Final[dict[str, str]] = {
    "approved": "Angenommen",
    "rejected": "Abgelehnt",
    "deferred": "Vertagt",
    "withdrawn": "Zurückgezogen",
    "noted": "Zur Kenntnis genommen",
}
#: Einzelstimme bei namentlicher Abstimmung (Befangenheit wie in der Niederschrift)
ROLL_CALL_VOTE_LABELS: Final[dict[str, str]] = {
    "yes": "Ja",
    "no": "Nein",
    "abstain": "Enthaltung",
    "excluded": "Befangen (Mitwirkungsverbot)",
}
#: Umsetzungsstand (Vertrag ``ris.resolution.implementation_changed``)
IMPLEMENTATION_LABELS: Final[dict[str, str]] = {
    "open": "Offen",
    "in_progress": "In Umsetzung",
    "done": "Erledigt",
    "deferred": "Zurückgestellt",
}
#: Genehmigungsweg der Niederschrift (Vertrag ``ris.protocol.approved``)
APPROVAL_MODE_LABELS: Final[dict[str, str]] = {
    "follow_up": "Genehmigt in der Folgesitzung",
    "direct": "Ohne Genehmigungsschritt veröffentlicht",
}

#: Längengrenzen der Spalten im RIS-Bestand
RESOLUTION_NUMBER_MAX: Final = 100
NAME_MAX: Final = 255
NOTE_MAX: Final = 10_000
URI_MAX: Final = 2_000
#: Höchstzahl der Einzelstimmen und der Stimmen je Summe (mehr hat kein Gremium)
ROLL_CALL_MAX: Final = 1_000
VOTES_MAX: Final = 100_000

#: Spalten des Tagesordnungspunkts, die ``agenda_item_columns`` liefert (immer alle, fehlend als ``None``)
AGENDA_ITEM_COLUMNS: Final[tuple[str, ...]] = (
    "resolution_number",
    "vote_method",
    "vote_result",
    "votes_yes",
    "votes_no",
    "votes_abstain",
    "roll_call",
    "implementation_status",
    "implementation_deadline",
    "implementation_public_note",
    "implementation_modified",
)
#: Spalten der Sitzung, die ``meeting_columns`` liefert
MEETING_COLUMNS: Final[tuple[str, ...]] = (
    "protocol_approval_mode",
    "protocol_approved_on",
    "protocol_approved_in_external_id",
)


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= limit else None


def _code(value: Any, allowed: Mapping[str, str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= VOTES_MAX else None


def _date(value: Any) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _datetime(value: Any) -> dt.datetime | None:
    """Zeitpunkt mit Zeitzone; ohne Zeitzone gilt er nicht (eine Quelle muss sie nennen)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _roll_call(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list) or len(value) > ROLL_CALL_MAX:
        return None
    entries: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        name = _text(entry.get("name"), NAME_MAX)
        vote = _code(entry.get("vote"), ROLL_CALL_VOTE_LABELS)
        if name and vote:
            entries.append({"name": name, "vote": vote})
    return entries or None


def agenda_item_columns(data: Mapping[str, Any]) -> dict[str, Any]:
    """
    Spalten der Beschlussfassung aus einem OParl-``AgendaItem`` (alle Schlüssel aus ``AGENDA_ITEM_COLUMNS``).

    Ohne Erweiterungen sind alle Werte ``None``. Einzelstimmen gibt es nur bei namentlicher Abstimmung; ein
    Umsetzungsstand ohne gültigen Code gilt als nicht angegeben.
    """
    vote = data.get("mandari:vote")
    vote = vote if isinstance(vote, Mapping) else {}
    method = _code(vote.get("method"), VOTING_METHOD_LABELS)
    implementation = data.get("mandari:implementation")
    implementation = implementation if isinstance(implementation, Mapping) else {}
    status = _code(implementation.get("status"), IMPLEMENTATION_LABELS)
    return {
        "resolution_number": _text(data.get("mandari:resolutionNumber"), RESOLUTION_NUMBER_MAX),
        "vote_method": method,
        "vote_result": _code(vote.get("result"), RESULT_LABELS),
        "votes_yes": _count(vote.get("yes")),
        "votes_no": _count(vote.get("no")),
        "votes_abstain": _count(vote.get("abstain")),
        "roll_call": _roll_call(data.get("mandari:rollCall")) if method == "roll_call" else None,
        "implementation_status": status,
        "implementation_deadline": _date(implementation.get("deadline")) if status else None,
        "implementation_public_note": _text(implementation.get("note"), NOTE_MAX) if status else None,
        "implementation_modified": _datetime(implementation.get("modified")) if status else None,
    }


def meeting_columns(data: Mapping[str, Any]) -> dict[str, Any]:
    """Spalten der Genehmigung der Niederschrift aus einem OParl-``Meeting`` (Schlüssel aus ``MEETING_COLUMNS``)."""
    approval = data.get("mandari:protocolApproval")
    approval = approval if isinstance(approval, Mapping) else {}
    mode = _code(approval.get("mode"), APPROVAL_MODE_LABELS)
    return {
        "protocol_approval_mode": mode,
        "protocol_approved_on": _date(approval.get("date")) if mode else None,
        "protocol_approved_in_external_id": _text(approval.get("meeting"), URI_MAX) if mode else None,
    }
