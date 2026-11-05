# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die Objekttypen von OParl 1.1: Eigenschaften je Typ mit ihrer Art und die Pflichtfelder.

Eine Quelle für zwei Zwecke:

- die Typprüfung der Ausgaben (``hub.api.tests.konformitaet``, auch im OParl-Validator der CI) und
- die Schemas der OParl-Typen in der gemeinsamen OpenAPI-Beschreibung (``hub.api.openapi.oparl``).

Arten: ``url``, ``text`` (nicht leer), ``datum`` (``yyyy-mm-dd``), ``zeitpunkt`` (mit Zeitzone), ``bool``,
``zahl`` (Ganzzahl), ``urls``, ``texte``, ``geojson`` sowie ``objekt:<Typ>`` und ``objekte:<Typ>`` für
eingebettete Objekte. Eigene Felder eines Herstellers tragen einen Namensraum (``präfix:name``).
"""

from __future__ import annotations

from typing import Final

#: Namensraum der Typen von OParl 1.1
SCHEMA: Final = "https://schema.oparl.org/1.1/"

#: Die sieben Werte von ``organizationType``
ORGANIZATION_TYPES: Final = frozenset(
    {
        "Gremium",
        "Partei",
        "Fraktion",
        "Verwaltungsbereich",
        "externes Gremium",
        "Institution",
        "Sonstiges",
    }
)

#: Eigenschaften, die jeder Objekttyp kennt
ALLGEMEIN: Final[dict[str, str]] = {
    "id": "url",
    "type": "text",
    "license": "url",
    "keyword": "texte",
    "created": "zeitpunkt",
    "modified": "zeitpunkt",
    "web": "url",
    "deleted": "bool",
}

#: Eigenschaften je Objekttyp (ohne ``ALLGEMEIN``) mit ihrer Art
FELDER: Final[dict[str, dict[str, str]]] = {
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

#: Pflichtfelder je Objekttyp (sonst ``id`` und ``type``)
PFLICHT: Final[dict[str, tuple[str, ...]]] = {
    "System": ("id", "type", "oparlVersion", "body"),
    "Body": ("id", "type", "name", "organization", "person", "meeting", "paper", "legislativeTerm"),
    "AgendaItem": ("id", "type", "order"),
    "File": ("id", "type", "accessUrl"),
}

#: Eigenschaften eines gelöschten Objekts (OParl 1.1 §2.8)
TOMBSTONE: Final = frozenset({"id", "type", "created", "modified", "deleted"})
