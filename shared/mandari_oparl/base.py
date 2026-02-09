"""
Base OParl Entity Model

Pydantic base class for all processed OParl entities.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from .enums import OParlType


class ProcessedEntity(BaseModel):
    """Base class for all processed OParl entities."""

    id: UUID
    external_id: str
    oparl_type: OParlType
    raw_json: dict[str, Any]
    body_external_id: str | None = None
    oparl_created: datetime | None = None
    oparl_modified: datetime | None = None

    # Extracted nested entities
    nested_entities: list["ProcessedEntity"] = Field(default_factory=list)

    # Extracted references (external IDs)
    references: dict[str, list[str]] = Field(default_factory=dict)
