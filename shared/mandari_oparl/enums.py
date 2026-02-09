"""
OParl Type Enums and Mappings

Defines OParl entity types and maps OParl type URLs (1.0 + 1.1) to enum values.
"""

from enum import Enum


class OParlType(str, Enum):
    """OParl entity types."""

    SYSTEM = "System"
    BODY = "Body"
    ORGANIZATION = "Organization"
    PERSON = "Person"
    MEETING = "Meeting"
    AGENDA_ITEM = "AgendaItem"
    PAPER = "Paper"
    CONSULTATION = "Consultation"
    FILE = "File"
    LOCATION = "Location"
    MEMBERSHIP = "Membership"
    LEGISLATIVE_TERM = "LegislativeTerm"


# Map OParl type URLs to enum
OPARL_TYPE_MAP: dict[str, OParlType] = {
    "https://schema.oparl.org/1.0/System": OParlType.SYSTEM,
    "https://schema.oparl.org/1.1/System": OParlType.SYSTEM,
    "https://schema.oparl.org/1.0/Body": OParlType.BODY,
    "https://schema.oparl.org/1.1/Body": OParlType.BODY,
    "https://schema.oparl.org/1.0/Organization": OParlType.ORGANIZATION,
    "https://schema.oparl.org/1.1/Organization": OParlType.ORGANIZATION,
    "https://schema.oparl.org/1.0/Person": OParlType.PERSON,
    "https://schema.oparl.org/1.1/Person": OParlType.PERSON,
    "https://schema.oparl.org/1.0/Meeting": OParlType.MEETING,
    "https://schema.oparl.org/1.1/Meeting": OParlType.MEETING,
    "https://schema.oparl.org/1.0/AgendaItem": OParlType.AGENDA_ITEM,
    "https://schema.oparl.org/1.1/AgendaItem": OParlType.AGENDA_ITEM,
    "https://schema.oparl.org/1.0/Paper": OParlType.PAPER,
    "https://schema.oparl.org/1.1/Paper": OParlType.PAPER,
    "https://schema.oparl.org/1.0/Consultation": OParlType.CONSULTATION,
    "https://schema.oparl.org/1.1/Consultation": OParlType.CONSULTATION,
    "https://schema.oparl.org/1.0/File": OParlType.FILE,
    "https://schema.oparl.org/1.1/File": OParlType.FILE,
    "https://schema.oparl.org/1.0/Location": OParlType.LOCATION,
    "https://schema.oparl.org/1.1/Location": OParlType.LOCATION,
    "https://schema.oparl.org/1.0/Membership": OParlType.MEMBERSHIP,
    "https://schema.oparl.org/1.1/Membership": OParlType.MEMBERSHIP,
    "https://schema.oparl.org/1.0/LegislativeTerm": OParlType.LEGISLATIVE_TERM,
    "https://schema.oparl.org/1.1/LegislativeTerm": OParlType.LEGISLATIVE_TERM,
}
