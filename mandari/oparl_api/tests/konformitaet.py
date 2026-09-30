# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Testhilfe: prüft OParl-Objekte gegen die Feldtypen von OParl 1.1.

Die Tabelle ``FELDER`` nennt je Objekttyp die Eigenschaften der Spezifikation mit ihrem Typ, ``PFLICHT``
die zwingenden Eigenschaften. ``pruefe`` läuft ein Objekt samt eingebetteter Objekte ab und liefert die
Abweichungen als Liste von Sätzen; eine leere Liste heißt konform. Beide Ausgaben (Aggregator und
Session-Schnittstelle) prüfen ihre Antworten damit.

Geprüft wird:

- Pflichtfelder vorhanden, ``type`` passt zum Objekttyp
- Typen: URL, Text, Datum (``yyyy-mm-dd``), Zeitpunkt mit Zeitzone, Wahrheitswert, Ganzzahl, Listen,
  eingebettete Objekte
- keine leeren Texte, keine unbekannten Eigenschaften ohne Namensraum (eigene Felder heißen ``präfix:name``)
- ``organizationType`` ist einer der sieben Werte der Spezifikation
- gelöschte Objekte tragen nur ``id``, ``type``, ``created``, ``modified``, ``deleted``
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

SCHEMA = "https://schema.oparl.org/1.1/"

ORGANIZATION_TYPES = {
    "Gremium",
    "Partei",
    "Fraktion",
    "Verwaltungsbereich",
    "externes Gremium",
    "Institution",
    "Sonstiges",
}

#: Eigenschaften, die jeder Objekttyp kennt
_ALLGEMEIN = {
    "id": "url",
    "type": "text",
    "license": "url",
    "keyword": "texte",
    "created": "zeitpunkt",
    "modified": "zeitpunkt",
    "web": "url",
    "deleted": "bool",
}

FELDER: dict[str, dict[str, str]] = {
    "System": {
        "oparlVersion": "text",
        "otherOparlVersions": "urls",
        "body": "url",
        "name": "text",
        "contactEmail": "text",
        "contactName": "text",
        "website": "url",
        "vendor": "url",
        "product": "url",
    },
    "Body": {
        "system": "url",
        "shortName": "text",
        "name": "text",
        "website": "url",
        "licenseValidSince": "zeitpunkt",
        "oparlSince": "zeitpunkt",
        "ags": "text",
        "rgs": "text",
        "equivalent": "urls",
        "contactEmail": "text",
        "contactName": "text",
        "organization": "url",
        "person": "url",
        "meeting": "url",
        "paper": "url",
        "legislativeTerm": "objekte:LegislativeTerm",
        "agendaItem": "url",
        "consultation": "url",
        "file": "url",
        "locationList": "url",
        "legislativeTermList": "url",
        "membership": "url",
        "classification": "text",
        "location": "objekt:Location",
        "mainOrganization": "url",
    },
    "LegislativeTerm": {"body": "url", "name": "text", "startDate": "datum", "endDate": "datum"},
    "Organization": {
        "body": "url",
        "name": "text",
        "membership": "urls",
        "meeting": "url",
        "consultation": "url",
        "shortName": "text",
        "post": "texte",
        "subOrganizationOf": "url",
        "organizationType": "text",
        "classification": "text",
        "startDate": "datum",
        "endDate": "datum",
        "website": "url",
        "location": "objekt:Location",
        "externalBody": "url",
        "memberCount": "zahl",
        "votingMemberCount": "zahl",
    },
    "Person": {
        "body": "url",
        "name": "text",
        "familyName": "text",
        "givenName": "text",
        "formOfAddress": "text",
        "affix": "text",
        "title": "texte",
        "gender": "text",
        "phone": "texte",
        "email": "texte",
        "location": "url",
        "locationObject": "objekt:Location",
        "status": "texte",
        "membership": "objekte:Membership",
        "image": "objekt:File",
        "life": "text",
        "lifeSource": "text",
    },
    "Membership": {
        "person": "url",
        "organization": "url",
        "role": "text",
        "votingRight": "bool",
        "startDate": "datum",
        "endDate": "datum",
        "onBehalfOf": "url",
    },
    "Meeting": {
        "name": "text",
        "meetingState": "text",
        "cancelled": "bool",
        "start": "zeitpunkt",
        "end": "zeitpunkt",
        "location": "objekt:Location",
        "organization": "urls",
        "participant": "urls",
        "invitation": "objekt:File",
        "resultsProtocol": "objekt:File",
        "verbatimProtocol": "objekt:File",
        "auxiliaryFile": "objekte:File",
        "agendaItem": "objekte:AgendaItem",
    },
    "AgendaItem": {
        "meeting": "url",
        "number": "text",
        "order": "zahl",
        "name": "text",
        "public": "bool",
        "consultation": "url",
        "result": "text",
        "resolutionText": "text",
        "resolutionFile": "objekt:File",
        "auxiliaryFile": "objekte:File",
        "start": "zeitpunkt",
        "end": "zeitpunkt",
    },
    "Paper": {
        "body": "url",
        "name": "text",
        "reference": "text",
        "date": "datum",
        "paperType": "text",
        "relatedPaper": "urls",
        "superordinatedPaper": "urls",
        "subordinatedPaper": "urls",
        "mainFile": "objekt:File",
        "auxiliaryFile": "objekte:File",
        "location": "objekte:Location",
        "originatorPerson": "urls",
        "underDirectionOf": "urls",
        "originatorOrganization": "urls",
        "consultation": "objekte:Consultation",
    },
    "Consultation": {
        "paper": "url",
        "agendaItem": "url",
        "meeting": "url",
        "organization": "urls",
        "authoritative": "bool",
        "role": "text",
    },
    "File": {
        "name": "text",
        "fileName": "text",
        "mimeType": "text",
        "date": "datum",
        "size": "zahl",
        "sha1Checksum": "text",
        "sha512Checksum": "text",
        "text": "text",
        "accessUrl": "url",
        "downloadUrl": "url",
        "externalServiceUrl": "url",
        "masterFile": "url",
        "derivativeFile": "urls",
        "fileLicense": "url",
        "meeting": "urls",
        "agendaItem": "urls",
        "person": "url",
        "paper": "urls",
    },
    "Location": {
        "description": "text",
        "geojson": "geojson",
        "streetAddress": "text",
        "room": "text",
        "postalCode": "text",
        "subLocality": "text",
        "locality": "text",
        "bodies": "urls",
        "organizations": "urls",
        "persons": "urls",
        "meetings": "urls",
        "papers": "urls",
    },
}

PFLICHT: dict[str, tuple[str, ...]] = {
    "System": ("id", "type", "oparlVersion", "body"),
    "Body": ("id", "type", "name", "organization", "person", "meeting", "paper", "legislativeTerm"),
    "AgendaItem": ("id", "type", "order"),
    "File": ("id", "type", "accessUrl"),
}

_DATUM = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOMBSTONE = {"id", "type", "created", "modified", "deleted"}


def _ist_url(wert: Any) -> bool:
    return isinstance(wert, str) and wert.startswith(("http://", "https://")) and " " not in wert


def _ist_datum(wert: Any) -> bool:
    if not isinstance(wert, str) or not _DATUM.match(wert):
        return False
    try:
        date.fromisoformat(wert)
    except ValueError:
        return False
    return True


def _ist_zeitpunkt(wert: Any) -> bool:
    if not isinstance(wert, str) or "T" not in wert:
        return False
    try:
        return datetime.fromisoformat(wert).tzinfo is not None
    except ValueError:
        return False


def _pruefe_wert(pfad: str, art: str, wert: Any, probleme: list[str]) -> None:
    if art.startswith("objekt:"):
        if isinstance(wert, dict):
            probleme.extend(pruefe(wert, art.split(":", 1)[1], pfad=pfad))
        else:
            probleme.append(f"{pfad}: eingebettetes Objekt erwartet, erhalten {wert!r}")
        return
    if art.startswith("objekte:"):
        if not isinstance(wert, list) or not all(isinstance(eintrag, dict) for eintrag in wert):
            probleme.append(f"{pfad}: Liste eingebetteter Objekte erwartet, erhalten {wert!r}")
            return
        for nummer, eintrag in enumerate(wert):
            probleme.extend(pruefe(eintrag, art.split(":", 1)[1], pfad=f"{pfad}[{nummer}]"))
        return
    pruefungen = {
        "url": _ist_url(wert),
        "text": isinstance(wert, str) and wert != "",
        "datum": _ist_datum(wert),
        "zeitpunkt": _ist_zeitpunkt(wert),
        "bool": isinstance(wert, bool),
        "zahl": isinstance(wert, int) and not isinstance(wert, bool),
        "urls": isinstance(wert, list) and all(_ist_url(eintrag) for eintrag in wert),
        "texte": isinstance(wert, list) and all(isinstance(e, str) and e != "" for e in wert),
        "geojson": isinstance(wert, dict),
    }
    if not pruefungen[art]:
        probleme.append(f"{pfad}: {art} erwartet, erhalten {wert!r}")


def pruefe(objekt: dict[str, Any], typ: str, *, pfad: str = "") -> list[str]:
    """Abweichungen eines OParl-Objekts vom Typ ``typ`` (samt eingebetteter Objekte); leer = konform."""
    pfad = pfad or typ
    probleme: list[str] = []
    if objekt.get("type") != f"{SCHEMA}{typ}":
        probleme.append(f"{pfad}.type: {SCHEMA}{typ} erwartet, erhalten {objekt.get('type')!r}")
    if objekt.get("deleted") is True:
        ueberzaehlig = sorted(set(objekt) - _TOMBSTONE)
        if ueberzaehlig:
            probleme.append(f"{pfad}: gelöschtes Objekt trägt weitere Eigenschaften {ueberzaehlig}")
        pflicht: tuple[str, ...] = ("id", "type")
    else:
        pflicht = PFLICHT.get(typ, ("id", "type"))
    for name in pflicht:
        if name not in objekt:
            probleme.append(f"{pfad}.{name}: Pflichtfeld fehlt")
    felder = {**_ALLGEMEIN, **FELDER[typ]}
    for name, wert in objekt.items():
        if ":" in name:
            continue  # eigene Felder im Namensraum eines Herstellers
        art = felder.get(name)
        if art is None:
            probleme.append(f"{pfad}.{name}: unbekannte Eigenschaft ohne Namensraum")
            continue
        if wert is None:
            continue
        _pruefe_wert(f"{pfad}.{name}", art, wert, probleme)
    if typ == "Organization" and "organizationType" in objekt and objekt["organizationType"] not in ORGANIZATION_TYPES:
        probleme.append(f"{pfad}.organizationType: kein Wert der Spezifikation: {objekt['organizationType']!r}")
    return probleme


def pruefe_liste(liste: dict[str, Any], typ: str) -> list[str]:
    """Abweichungen einer externen Objektliste (Hülle ``data``/``pagination``/``links`` und alle Objekte)."""
    probleme: list[str] = []
    for name in ("data", "pagination", "links"):
        if name not in liste:
            probleme.append(f"Liste {typ}: {name} fehlt")
    for nummer, objekt in enumerate(liste.get("data", [])):
        probleme.extend(pruefe(objekt, typ, pfad=f"{typ}[{nummer}]"))
    for name, wert in liste.get("links", {}).items():
        if not _ist_url(wert):
            probleme.append(f"Liste {typ}: links.{name} ist keine URL: {wert!r}")
    return probleme
