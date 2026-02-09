"""
OParl Utility Functions

Deterministic UUID generation, datetime/date parsing for OParl data.
"""

from datetime import date, datetime, timezone
from uuid import UUID, NAMESPACE_URL, uuid5


def generate_uuid(external_id: str) -> UUID:
    """
    Generate a deterministic UUID from an external ID.

    Uses UUID5 with URL namespace for consistency.
    """
    return uuid5(NAMESPACE_URL, external_id)


def parse_datetime(value: str | None) -> datetime | None:
    """Parse OParl datetime string to datetime object."""
    if not value:
        return None
    try:
        # Handle various ISO 8601 formats
        value = value.replace("Z", "+00:00")
        # Handle dates without time
        if "T" not in value:
            return datetime.fromisoformat(f"{value}T00:00:00+00:00")
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_date(value: str | None) -> date | None:
    """Parse OParl date string to date object (date-only, no time)."""
    if not value:
        return None
    try:
        # Strip time part if present
        date_str = value.split("T")[0]
        return date.fromisoformat(date_str)
    except ValueError:
        return None
