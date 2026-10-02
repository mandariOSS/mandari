"""
mandari-oparl: Shared OParl types for the Mandari platform.

Single source of truth for OParl Pydantic schemas used by both
the Ingestor and Django.
"""

from .base import ProcessedEntity
from .entities import (
    ProcessedAgendaItem,
    ProcessedBody,
    ProcessedConsultation,
    ProcessedFile,
    ProcessedLegislativeTerm,
    ProcessedLocation,
    ProcessedMeeting,
    ProcessedMembership,
    ProcessedOrganization,
    ProcessedPaper,
    ProcessedPerson,
)
from .enums import OPARL_TYPE_MAP, OParlType
from .ids import (
    NS_MANDARI_RIS,
    SOURCE_ID_ADDRESS_KEY,
    SOURCE_ID_BASE_KEY,
    SOURCE_ID_RULES_KEY,
    IdBases,
    canonical_id,
    canonical_uri,
    id_rules,
    source_id_address,
    source_id_base,
    source_id_rules,
)
from .utils import generate_uuid, parse_date, parse_datetime

__all__ = [
    # Enums
    "OParlType",
    "OPARL_TYPE_MAP",
    # Base
    "ProcessedEntity",
    # Entities
    "ProcessedAgendaItem",
    "ProcessedBody",
    "ProcessedConsultation",
    "ProcessedFile",
    "ProcessedLegislativeTerm",
    "ProcessedLocation",
    "ProcessedMeeting",
    "ProcessedMembership",
    "ProcessedOrganization",
    "ProcessedPaper",
    "ProcessedPerson",
    # Kanonische Kennungen
    "NS_MANDARI_RIS",
    "SOURCE_ID_ADDRESS_KEY",
    "SOURCE_ID_BASE_KEY",
    "SOURCE_ID_RULES_KEY",
    "IdBases",
    "canonical_id",
    "canonical_uri",
    "id_rules",
    "source_id_address",
    "source_id_base",
    "source_id_rules",
    # Utils
    "generate_uuid",
    "parse_date",
    "parse_datetime",
]
