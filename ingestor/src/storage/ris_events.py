# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS-Ereignisse des Ingestors: aus dem alten und dem neuen Stand eines Objekts wird ein Ereignis.

Der Ingestor ist der Eingangsadapter der Datendrehscheibe für fremde Ratsinformationssysteme
(``docs/adr/20260929-ereignistechnik-postgres.md``, ``docs/adr/20260929-ereignisvertraege.md``). Er
meldet Änderungen mit denselben Verträgen wie mandari Session (``mandari/hub/contracts/schemas/``):

==============  =====================================================================
Objekt          Ereignis
==============  =====================================================================
Meeting         ``ris.meeting.scheduled`` (neu erkannt), ``ris.meeting.changed``
Paper           ``ris.paper.released`` (neu erkannt), ``ris.paper.changed``
AgendaItem      ``ris.agendaitem.changed`` (``added``, ``changed``, ``moved``)
Consultation    ``ris.consultation.changed`` (``added``, ``scheduled``, ``changed``)
File            ``ris.file.changed`` (``added``, ``replaced``, ``renamed``)
alle Typen      ``ris.object.depublished`` (Löschmarkierung der Quelle)
==============  =====================================================================

Für Gremien, Personen, Mitgliedschaften, Orte, Wahlperioden und Kommunen gibt es noch keinen
Vertrag für Änderungen; sie melden nur ihre Löschmarkierung.

Regeln:

- **Nur echte Änderungen.** Ein Upsert ohne inhaltliche Änderung ergibt kein Ereignis. Verglichen
  wird das Objekt der Quelle (``raw_json``) Feld für Feld. ``created``, ``modified`` und der
  Content-Hash zählen auf keiner Ebene: Manche Quellen stempeln sie bei jedem Abruf neu. Der
  Rückverweis auf das übergeordnete Objekt (``meeting`` am Tagesordnungspunkt, ``paper`` an der
  Beratung, ``paper``/``meeting``/``agendaItem`` an der Datei) zählt ebenfalls nicht: Eingebettet
  fehlt er, in der eigenen Liste steht er, und jeder Vollabgleich schriebe sonst beide Fassungen
  abwechselnd.
- **Nur Kennungen.** Die Nutzlast nennt Kennungen, Codes und die Namen geänderter Felder, nie
  Inhalte. Feldnamen folgen OParl (``name``, ``paperType``); Erweiterungen mit Namensraum erscheinen
  mit Unterstrich (``mandari:meetingFormat`` als ``mandari_meetingFormat``), weil der Vertrag nur
  Buchstaben, Ziffern und Unterstrich zulässt. Felder, die auch so nicht darstellbar sind, werden
  nicht genannt.
- **Kennungen.** Das Objekt selbst trägt die Kennung seiner Zeile im RIS-Bestand; der Ingestor
  vergibt sie als kanonische Kennung (``mandari_oparl.ids.canonical_id``). Bezüge auf andere
  Objekte, die nur als URL vorliegen, werden mit derselben Funktion gebildet.
- **Sichtbarkeit.** Was der Ingestor liest, hat die Quelle veröffentlicht: ``oeffentlich``.
  Ausnahme sind Tagesordnungspunkte, die die Quelle als nichtöffentlich kennzeichnet
  (``public: false``): Ihre Ereignisse sind ``nichtoeffentlich``. Wird ein bisher öffentlicher Punkt
  nichtöffentlich, meldet zusätzlich ``ris.object.depublished`` (Grund ``nichtoeffentlich``) die
  Rücknahme an öffentliche Empfänger; wird er öffentlich, erscheint er ihnen als ``added``.

Dieses Modul braucht nur die Standardbibliothek und ``mandari_oparl``; die Vertragstests der
Drehscheibe laden es ohne die übrige Umgebung des Ingestors
(``mandari/hub/contracts/tests/test_ingestor_events.py``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from mandari_oparl.ids import canonical_id

#: Schemaversion aller Ereignisse dieses Moduls.
VERSION: Final = 1

OEFFENTLICH: Final = "oeffentlich"
NICHTOEFFENTLICH: Final = "nichtoeffentlich"
UPSERT: Final = "upsert"
DELETE: Final = "delete"

#: Gründe einer Rücknahme (wie im Änderungsfeed).
REASON_DELETED_AT_SOURCE: Final = "quelle_geloescht"
REASON_NOT_PUBLIC: Final = "nichtoeffentlich"

#: Entity-Typ des Ingestors -> kanonischer Typ (``aggregate_type`` der Hülle).
AGGREGATE_TYPES: Final[dict[str, str]] = {
    "body": "Body",
    "organization": "Organization",
    "person": "Person",
    "membership": "Membership",
    "legislativeterm": "LegislativeTerm",
    "meeting": "Meeting",
    "agendaitem": "AgendaItem",
    "paper": "Paper",
    "consultation": "Consultation",
    "file": "File",
    "location": "Location",
}

#: Felder ohne Aussage über eine Änderung, auf jeder Ebene (wie ``VOLATILE_HASH_FIELDS`` der Scraper).
VOLATILE_FIELDS: Final = frozenset({"modified", "created", "mandari:contentHash"})
#: Rückverweise auf das übergeordnete Objekt; eingebettet fehlen sie.
AGENDA_ITEM_BACKREFS: Final = frozenset({"meeting"})
CONSULTATION_BACKREFS: Final = frozenset({"paper"})
FILE_BACKREFS: Final = frozenset({"paper", "meeting", "agendaItem"})
#: Felder einer Datei, deren Änderung eine neue Fassung bedeutet (``replaced``).
FILE_CONTENT_FIELDS: Final = frozenset(
    {
        "accessUrl",
        "downloadUrl",
        "externalServiceUrl",
        "size",
        "sha1Checksum",
        "sha512Checksum",
        "mimeType",
        "date",
        "text",
        "masterFile",
        "derivativeFile",
    }
)
FILE_NAME_FIELDS: Final = frozenset({"name", "fileName"})

#: Feldnamen, wie sie die Verträge in ``changed`` zulassen.
_FIELD_NAME: Final = re.compile(r"^[a-z][A-Za-z0-9_]{0,63}$")
MAX_CHANGED: Final = 64
MAX_ORGANIZATIONS: Final = 50


@dataclass(frozen=True)
class Prior:
    """Stand einer Zeile vor dem Upsert."""

    raw_json: Mapping[str, Any]
    deleted: bool = False
    #: Sitzung eines Tagesordnungspunkts vor dem Upsert
    meeting_id: UUID | None = None
    #: Kennzeichen ``public`` eines Tagesordnungspunkts vor dem Upsert
    public: bool = True


@dataclass(frozen=True)
class Draft:
    """Ein Ereignis ohne Angaben zur Herkunft; Mandant, Kommune und Korrelation ergänzt der Speicher."""

    type: str
    aggregate_type: str
    aggregate_id: UUID
    payload: dict[str, Any]
    visibility: str = OEFFENTLICH
    operation: str = UPSERT
    version: int = VERSION


# --- Vergleich -----------------------------------------------------------------------------------


def _stable(value: Any) -> Any:
    """Wert ohne die Felder, die keine Änderung anzeigen (auf jeder Ebene)."""
    if isinstance(value, Mapping):
        return {key: _stable(item) for key, item in value.items() if key not in VOLATILE_FIELDS}
    if isinstance(value, list | tuple):
        return [_stable(item) for item in value]
    return value


def differing_keys(
    old: Mapping[str, Any] | None, new: Mapping[str, Any] | None, ignore: Iterable[str] = ()
) -> list[str]:
    """Schlüssel der obersten Ebene, deren Wert sich geändert hat, sortiert (Schreibweise der Quelle)."""
    old, new = old or {}, new or {}
    skip = VOLATILE_FIELDS.union(ignore)
    return sorted(
        key for key in old.keys() | new.keys() if key not in skip and _stable(old.get(key)) != _stable(new.get(key))
    )


def field_name(key: str) -> str | None:
    """Feldname, wie ihn die Verträge zulassen, oder ``None``, wenn er nicht darstellbar ist."""
    name = key.replace(":", "_")
    return name if _FIELD_NAME.fullmatch(name) else None


def field_names(keys: Iterable[str]) -> list[str]:
    """Darstellbare Feldnamen zu geänderten Schlüsseln: eindeutig, sortiert, höchstens ``MAX_CHANGED``."""
    names = sorted({name for name in map(field_name, keys) if name is not None})
    return names[:MAX_CHANGED]


def reference(value: Any) -> str | None:
    """Kanonische Kennung zu einem Verweis: URL oder eingebettetes Objekt mit ``id``."""
    if isinstance(value, Mapping):
        value = value.get("id")
    if isinstance(value, str) and value:
        return str(canonical_id(value))
    return None


def references(value: Any) -> list[str]:
    """Kanonische Kennungen zu einem Verweis oder einer Liste von Verweisen, ohne Doppelte."""
    items = value if isinstance(value, list | tuple) else [value]
    found: list[str] = []
    for item in items:
        ref = reference(item)
        if ref is not None and ref not in found:
            found.append(ref)
    return found


def _first_reference(value: Any) -> str | None:
    refs = references(value)
    return refs[0] if refs else None


# --- Ereignisse je Objekt --------------------------------------------------------------------------


def meeting_events(meeting_id: UUID, raw: Mapping[str, Any], prior: Prior | None) -> list[Draft]:
    """``ris.meeting.scheduled`` für eine neu erkannte, ``ris.meeting.changed`` für eine geänderte Sitzung."""
    payload: dict[str, Any] = {"meeting": str(meeting_id)}
    organizations = references(raw.get("organization"))
    if prior is None or prior.deleted:
        if 0 < len(organizations) <= MAX_ORGANIZATIONS:
            payload["organizations"] = organizations
        return [Draft("ris.meeting.scheduled", "Meeting", meeting_id, payload)]
    names = field_names(differing_keys(prior.raw_json, raw))
    if not names:
        return []
    payload["changed"] = names
    payload["cancelled"] = raw.get("cancelled") is True
    if 0 < len(organizations) <= MAX_ORGANIZATIONS:
        payload["organizations"] = organizations
    return [Draft("ris.meeting.changed", "Meeting", meeting_id, payload)]


def paper_events(paper_id: UUID, raw: Mapping[str, Any], prior: Prior | None) -> list[Draft]:
    """``ris.paper.released`` für eine neu erkannte, ``ris.paper.changed`` für eine geänderte Vorlage."""
    payload: dict[str, Any] = {"paper": str(paper_id)}
    if prior is None or prior.deleted:
        return [Draft("ris.paper.released", "Paper", paper_id, payload)]
    names = field_names(differing_keys(prior.raw_json, raw))
    if not names:
        return []
    payload["changed"] = names
    return [Draft("ris.paper.changed", "Paper", paper_id, payload)]


def agenda_item_events(
    item_id: UUID, raw: Mapping[str, Any], prior: Prior | None, *, meeting_id: UUID, public: bool
) -> list[Draft]:
    """``ris.agendaitem.changed``; bei Wechsel in den nichtöffentlichen Teil zusätzlich die Rücknahme."""
    visibility = OEFFENTLICH if public else NICHTOEFFENTLICH
    payload: dict[str, Any] = {"agenda_item": str(item_id), "meeting": str(meeting_id)}
    if prior is None or prior.deleted or (public and not prior.public):
        # Neu, nach einer Löschmarkierung wieder geliefert oder jetzt öffentlich: Für die Empfänger
        # dieser Sichtbarkeit gibt es den Punkt erst ab hier.
        payload["change"] = "added"
        return [Draft("ris.agendaitem.changed", "AgendaItem", item_id, payload, visibility)]

    keys = differing_keys(prior.raw_json, raw, AGENDA_ITEM_BACKREFS)
    moved = prior.meeting_id is not None and prior.meeting_id != meeting_id
    if not keys and not moved:
        return []
    payload["change"] = "moved" if moved else "changed"
    names = field_names(keys)
    if names:
        payload["changed"] = names
    if moved:
        payload["previous_meeting"] = str(prior.meeting_id)
    drafts = [Draft("ris.agendaitem.changed", "AgendaItem", item_id, payload, visibility)]
    if prior.public and not public:
        # Öffentliche Empfänger sehen das nichtöffentliche Ereignis nicht; ihnen gilt der Punkt als
        # zurückgenommen.
        drafts.insert(0, depublished_draft("agendaitem", item_id, REASON_NOT_PUBLIC))
    return drafts


def consultation_events(
    consultation_id: UUID,
    raw: Mapping[str, Any],
    prior: Prior | None,
    *,
    paper_id: UUID | None = None,
    paper_external_id: str | None = None,
) -> list[Draft]:
    """``ris.consultation.changed``; ohne bekannte Vorlage lässt sich die Beratung nicht melden."""
    paper = str(paper_id) if paper_id is not None else reference(paper_external_id or raw.get("paper"))
    if paper is None:
        return []
    payload: dict[str, Any] = {"consultation": str(consultation_id), "paper": paper}
    if prior is None or prior.deleted:
        payload["change"] = "added"
    else:
        keys = differing_keys(prior.raw_json, raw, CONSULTATION_BACKREFS)
        if not keys:
            return []
        newly_scheduled = not prior.raw_json.get("meeting") and bool(raw.get("meeting"))
        payload["change"] = "scheduled" if newly_scheduled else "changed"
        names = field_names(keys)
        if names:
            payload["changed"] = names
    for name, value in (
        ("organization", raw.get("organization")),
        ("meeting", raw.get("meeting")),
        ("agenda_item", raw.get("agendaItem")),
    ):
        ref = _first_reference(value)
        if ref is not None:
            payload[name] = ref
    return [Draft("ris.consultation.changed", "Consultation", consultation_id, payload)]


def file_events(
    file_id: UUID,
    raw: Mapping[str, Any],
    prior: Prior | None,
    *,
    paper_id: UUID | None = None,
    meeting_id: UUID | None = None,
) -> list[Draft]:
    """``ris.file.changed``: ``added``, ``replaced`` (neue Fassung) oder ``renamed``."""
    payload: dict[str, Any] = {"file": str(file_id)}
    if prior is None or prior.deleted:
        payload["change"] = "added"
    else:
        keys = differing_keys(prior.raw_json, raw, FILE_BACKREFS)
        if FILE_CONTENT_FIELDS.intersection(keys):
            payload["change"] = "replaced"
        elif FILE_NAME_FIELDS.intersection(keys):
            payload["change"] = "renamed"
        else:
            # Übrige Angaben (z. B. Lizenz) kennt der Vertrag nicht als Änderung der Datei.
            return []
    paper = str(paper_id) if paper_id is not None else _first_reference(raw.get("paper"))
    meeting = str(meeting_id) if meeting_id is not None else _first_reference(raw.get("meeting"))
    agenda_item = _first_reference(raw.get("agendaItem"))
    for name, ref in (("paper", paper), ("meeting", meeting), ("agenda_item", agenda_item)):
        if ref is not None:
            payload[name] = ref
    return [Draft("ris.file.changed", "File", file_id, payload)]


def depublished_draft(entity_type: str, object_id: UUID, reason: str = REASON_DELETED_AT_SOURCE) -> Draft:
    """``ris.object.depublished``: Das Objekt ist nicht mehr öffentlich (Operation ``delete``)."""
    aggregate_type = AGGREGATE_TYPES[entity_type]
    payload = {"object_type": aggregate_type, "object": str(object_id), "reason": reason}
    return Draft("ris.object.depublished", aggregate_type, object_id, payload, OEFFENTLICH, DELETE)


def depublished_events(entity_type: str, object_id: UUID) -> list[Draft]:
    """Löschmarkierung der Quelle; unbekannte Typen ergeben kein Ereignis."""
    if entity_type not in AGGREGATE_TYPES:
        return []
    return [depublished_draft(entity_type, object_id)]
