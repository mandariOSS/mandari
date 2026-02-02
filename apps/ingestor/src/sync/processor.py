"""
OParl Entity Processor

Processes, validates, and normalizes OParl entities.
Handles nested objects and extracts relationships.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid5, NAMESPACE_URL

from pydantic import BaseModel, Field, field_validator


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


class ProcessedBody(ProcessedEntity):
    """Processed OParl Body."""

    name: str
    short_name: str | None = None
    website: str | None = None
    license: str | None = None
    classification: str | None = None

    # List URLs for fetching
    organization_list_url: str | None = None
    person_list_url: str | None = None
    meeting_list_url: str | None = None
    paper_list_url: str | None = None
    membership_list_url: str | None = None
    location_list_url: str | None = None
    agenda_item_list_url: str | None = None
    consultation_list_url: str | None = None
    file_list_url: str | None = None
    legislative_term_list_url: str | None = None


class ProcessedMeeting(ProcessedEntity):
    """Processed OParl Meeting."""

    name: str | None = None
    meeting_state: str | None = None
    cancelled: bool = False
    start: datetime | None = None
    end: datetime | None = None
    location_external_id: str | None = None
    location_name: str | None = None
    location_address: str | None = None


class ProcessedPaper(ProcessedEntity):
    """Processed OParl Paper."""

    name: str | None = None
    reference: str | None = None
    paper_type: str | None = None
    date: datetime | None = None


class ProcessedPerson(ProcessedEntity):
    """Processed OParl Person."""

    name: str | None = None
    family_name: str | None = None
    given_name: str | None = None
    title: str | None = None
    gender: str | None = None
    email: str | None = None
    phone: str | None = None


class ProcessedOrganization(ProcessedEntity):
    """Processed OParl Organization."""

    name: str | None = None
    short_name: str | None = None
    organization_type: str | None = None
    classification: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    website: str | None = None


class ProcessedAgendaItem(ProcessedEntity):
    """Processed OParl AgendaItem."""

    number: str | None = None
    order: int | None = None
    name: str | None = None
    public: bool = True
    result: str | None = None
    resolution_text: str | None = None
    meeting_external_id: str | None = None


class ProcessedFile(ProcessedEntity):
    """Processed OParl File."""

    name: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    access_url: str | None = None
    download_url: str | None = None
    date: datetime | None = None

    # Back-references (from standalone files fetched via /files endpoint)
    # OParl spec: File objects contain 'paper' and 'meeting' arrays
    # when fetched individually (not embedded)
    paper_external_ids: list[str] = field(default_factory=list)
    meeting_external_ids: list[str] = field(default_factory=list)


class ProcessedLocation(ProcessedEntity):
    """Processed OParl Location."""

    description: str | None = None
    street_address: str | None = None
    room: str | None = None
    postal_code: str | None = None
    locality: str | None = None
    geojson: dict[str, Any] | None = None


class ProcessedConsultation(ProcessedEntity):
    """Processed OParl Consultation."""

    paper_external_id: str | None = None
    meeting_external_id: str | None = None
    agenda_item_external_id: str | None = None
    role: str | None = None
    authoritative: bool = False


class ProcessedMembership(ProcessedEntity):
    """Processed OParl Membership."""

    person_external_id: str | None = None
    organization_external_id: str | None = None
    role: str | None = None
    voting_right: bool = True
    start_date: datetime | None = None
    end_date: datetime | None = None


class ProcessedLegislativeTerm(ProcessedEntity):
    """Processed OParl LegislativeTerm."""

    name: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None


class OParlProcessor:
    """
    Processes OParl data into normalized entities.

    Handles:
    - Type detection
    - Datetime parsing
    - Nested entity extraction
    - Reference extraction
    - UUID generation from external IDs
    """

    def __init__(self) -> None:
        self._id_cache: dict[str, UUID] = {}

    def generate_uuid(self, external_id: str) -> UUID:
        """
        Generate a deterministic UUID from external ID.

        Uses UUID5 with URL namespace for consistency.
        """
        if external_id in self._id_cache:
            return self._id_cache[external_id]

        uuid = uuid5(NAMESPACE_URL, external_id)
        self._id_cache[external_id] = uuid
        return uuid

    def parse_datetime(self, value: str | None) -> datetime | None:
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

    def get_type(self, data: dict[str, Any]) -> OParlType | None:
        """Get OParl type from data."""
        type_url = data.get("type", "")
        return OPARL_TYPE_MAP.get(type_url)

    def process(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedEntity | None:
        """
        Process any OParl object.

        Automatically detects type and delegates to specific processor.
        """
        oparl_type = self.get_type(data)
        if not oparl_type:
            return None

        processors = {
            OParlType.BODY: self.process_body,
            OParlType.MEETING: self.process_meeting,
            OParlType.PAPER: self.process_paper,
            OParlType.PERSON: self.process_person,
            OParlType.ORGANIZATION: self.process_organization,
            OParlType.AGENDA_ITEM: self.process_agenda_item,
            OParlType.FILE: self.process_file,
            OParlType.LOCATION: self.process_location,
            OParlType.CONSULTATION: self.process_consultation,
            OParlType.MEMBERSHIP: self.process_membership,
            OParlType.LEGISLATIVE_TERM: self.process_legislative_term,
        }

        processor = processors.get(oparl_type)
        if processor:
            return processor(data, body_external_id)

        # Generic processing for unknown types
        return self._process_base(data, oparl_type, body_external_id)

    def _process_base(
        self,
        data: dict[str, Any],
        oparl_type: OParlType,
        body_external_id: str | None = None,
    ) -> ProcessedEntity:
        """Process base entity fields."""
        external_id = data.get("id", "")
        return ProcessedEntity(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=oparl_type,
            raw_json=data,
            body_external_id=body_external_id or data.get("body"),
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
        )

    def process_body(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedBody:
        """Process an OParl Body."""
        external_id = data.get("id", "")

        body = ProcessedBody(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=OParlType.BODY,
            raw_json=data,
            body_external_id=external_id,  # Body references itself
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
            name=data.get("name", "Unknown"),
            short_name=data.get("shortName"),
            website=data.get("website"),
            license=data.get("license"),
            classification=data.get("classification"),
            # List URLs - support both naming conventions used by different OParl servers
            organization_list_url=data.get("organization"),
            person_list_url=data.get("person"),
            meeting_list_url=data.get("meeting"),
            paper_list_url=data.get("paper"),
            membership_list_url=data.get("membership"),
            # Different servers use different field names - support all variants
            location_list_url=data.get("locationList"),  # Münster & Bonn use "locationList"
            agenda_item_list_url=data.get("agendaItem"),  # Both use "agendaItem"
            consultation_list_url=data.get("consultation") or data.get("consultations"),  # Bonn: "consultation", Münster: "consultations"
            file_list_url=data.get("file") or data.get("files"),  # Bonn: "file", Münster: "files"
            legislative_term_list_url=data.get("legislativeTermList"),  # Both use "legislativeTermList"
        )

        # Process embedded legislative terms
        for lt_data in data.get("legislativeTerm", []):
            if isinstance(lt_data, dict):
                lt = self.process_legislative_term(lt_data, external_id)
                body.nested_entities.append(lt)

        return body

    def process_meeting(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedMeeting:
        """Process an OParl Meeting."""
        external_id = data.get("id", "")

        meeting = ProcessedMeeting(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=OParlType.MEETING,
            raw_json=data,
            body_external_id=body_external_id or self._extract_body_id(data),
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
            name=data.get("name"),
            meeting_state=data.get("meetingState"),
            cancelled=data.get("cancelled", False),
            start=self.parse_datetime(data.get("start")),
            end=self.parse_datetime(data.get("end")),
        )

        # Extract location
        location = data.get("location")
        if isinstance(location, dict):
            meeting.location_external_id = location.get("id")
            meeting.location_name = location.get("room") or location.get("description")
            meeting.location_address = location.get("streetAddress")
            # Process nested location
            loc_entity = self.process_location(location, body_external_id)
            meeting.nested_entities.append(loc_entity)
        elif isinstance(location, str):
            meeting.location_external_id = location

        # Extract organization references
        orgs = data.get("organization", [])
        if orgs:
            meeting.references["organization"] = [
                o if isinstance(o, str) else o.get("id", "")
                for o in orgs
            ]

        # Process nested agenda items
        for ai_data in data.get("agendaItem", []):
            if isinstance(ai_data, dict):
                ai = self.process_agenda_item(ai_data, body_external_id)
                ai.meeting_external_id = external_id
                meeting.nested_entities.append(ai)

        # Process nested files (invitation, resultsProtocol, etc.)
        self._extract_files(meeting, data, body_external_id)

        return meeting

    def process_paper(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedPaper:
        """Process an OParl Paper."""
        external_id = data.get("id", "")

        # Truncate name to 500 chars to fit database column
        name = data.get("name")
        if name and len(name) > 500:
            name = name[:497] + "..."

        paper = ProcessedPaper(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=OParlType.PAPER,
            raw_json=data,
            body_external_id=body_external_id or self._extract_body_id(data),
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
            name=name,
            reference=data.get("reference"),
            paper_type=data.get("paperType"),
            date=self.parse_datetime(data.get("date")),
        )

        # Process nested files
        self._extract_files(paper, data, body_external_id)

        # Process nested consultations
        for cons_data in data.get("consultation", []):
            if isinstance(cons_data, dict):
                cons = self.process_consultation(cons_data, body_external_id)
                cons.paper_external_id = external_id
                paper.nested_entities.append(cons)

        # Extract references
        if data.get("originatorPerson"):
            paper.references["originator_person"] = self._extract_refs(data["originatorPerson"])
        if data.get("originatorOrganization"):
            paper.references["originator_organization"] = self._extract_refs(data["originatorOrganization"])
        if data.get("underDirectionOf"):
            paper.references["under_direction_of"] = self._extract_refs(data["underDirectionOf"])

        return paper

    def _normalize_string_field(self, value: Any) -> str | None:
        """Normalize a field that should be a string but might be a list."""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value:
            # Take the first value if it's a list
            return str(value[0]) if value[0] else None
        return str(value) if value else None

    def process_person(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedPerson:
        """Process an OParl Person."""
        external_id = data.get("id", "")

        return ProcessedPerson(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=OParlType.PERSON,
            raw_json=data,
            body_external_id=body_external_id or self._extract_body_id(data),
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
            name=data.get("name"),
            family_name=data.get("familyName"),
            given_name=data.get("givenName"),
            title=self._normalize_string_field(data.get("title")),
            gender=data.get("gender"),
            email=self._normalize_string_field(data.get("email")),
            phone=self._normalize_string_field(data.get("phone")),
        )

    def process_organization(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedOrganization:
        """Process an OParl Organization."""
        external_id = data.get("id", "")

        return ProcessedOrganization(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=OParlType.ORGANIZATION,
            raw_json=data,
            body_external_id=body_external_id or self._extract_body_id(data),
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
            name=data.get("name"),
            short_name=data.get("shortName"),
            organization_type=data.get("organizationType"),
            classification=data.get("classification"),
            start_date=self.parse_datetime(data.get("startDate")),
            end_date=self.parse_datetime(data.get("endDate")),
            website=data.get("website"),
        )

    def process_agenda_item(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedAgendaItem:
        """Process an OParl AgendaItem."""
        external_id = data.get("id", "")

        item = ProcessedAgendaItem(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=OParlType.AGENDA_ITEM,
            raw_json=data,
            body_external_id=body_external_id,
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
            number=data.get("number"),
            order=data.get("order"),
            name=data.get("name"),
            public=data.get("public", True),
            result=data.get("result"),
            resolution_text=data.get("resolutionText"),
            meeting_external_id=data.get("meeting") if isinstance(data.get("meeting"), str) else None,
        )

        # Extract consultation reference
        cons = data.get("consultation")
        if isinstance(cons, str):
            item.references["consultation"] = [cons]
        elif isinstance(cons, dict):
            item.references["consultation"] = [cons.get("id", "")]

        return item

    def process_file(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedFile:
        """Process an OParl File."""
        external_id = data.get("id", "")

        # Truncate strings to fit database columns
        name = data.get("name")
        if name and len(name) > 500:
            name = name[:497] + "..."
        file_name = data.get("fileName")
        if file_name and len(file_name) > 255:
            file_name = file_name[:252] + "..."

        # Extract paper and meeting back-references (OParl spec: standalone files have these)
        paper_external_ids = self._extract_refs(data.get("paper", []))
        meeting_external_ids = self._extract_refs(data.get("meeting", []))

        return ProcessedFile(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=OParlType.FILE,
            raw_json=data,
            body_external_id=body_external_id,
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
            name=name,
            file_name=file_name,
            mime_type=data.get("mimeType"),
            size=data.get("size"),
            access_url=data.get("accessUrl"),
            download_url=data.get("downloadUrl"),
            date=self.parse_datetime(data.get("date")),
            paper_external_ids=paper_external_ids,
            meeting_external_ids=meeting_external_ids,
        )

    def process_location(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedLocation:
        """Process an OParl Location."""
        external_id = data.get("id", "")

        return ProcessedLocation(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=OParlType.LOCATION,
            raw_json=data,
            body_external_id=body_external_id,
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
            description=data.get("description"),
            street_address=data.get("streetAddress"),
            room=data.get("room"),
            postal_code=data.get("postalCode"),
            locality=data.get("locality"),
            geojson=data.get("geojson"),
        )

    def process_consultation(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedConsultation:
        """Process an OParl Consultation."""
        external_id = data.get("id", "")

        cons = ProcessedConsultation(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=OParlType.CONSULTATION,
            raw_json=data,
            body_external_id=body_external_id,
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
            paper_external_id=data.get("paper") if isinstance(data.get("paper"), str) else None,
            meeting_external_id=data.get("meeting") if isinstance(data.get("meeting"), str) else None,
            agenda_item_external_id=data.get("agendaItem") if isinstance(data.get("agendaItem"), str) else None,
            role=data.get("role"),
            authoritative=data.get("authoritative", False),
        )

        # Extract organization references
        orgs = data.get("organization", [])
        if orgs:
            cons.references["organization"] = self._extract_refs(orgs)

        return cons

    def process_membership(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedMembership:
        """Process an OParl Membership."""
        external_id = data.get("id", "")

        return ProcessedMembership(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=OParlType.MEMBERSHIP,
            raw_json=data,
            body_external_id=body_external_id,
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
            person_external_id=data.get("person") if isinstance(data.get("person"), str) else None,
            organization_external_id=data.get("organization") if isinstance(data.get("organization"), str) else None,
            role=data.get("role"),
            voting_right=data.get("votingRight", True),
            start_date=self.parse_datetime(data.get("startDate")),
            end_date=self.parse_datetime(data.get("endDate")),
        )

    def process_legislative_term(
        self,
        data: dict[str, Any],
        body_external_id: str | None = None,
    ) -> ProcessedLegislativeTerm:
        """Process an OParl LegislativeTerm."""
        external_id = data.get("id", "")

        return ProcessedLegislativeTerm(
            id=self.generate_uuid(external_id),
            external_id=external_id,
            oparl_type=OParlType.LEGISLATIVE_TERM,
            raw_json=data,
            body_external_id=body_external_id or self._extract_body_id(data),
            oparl_created=self.parse_datetime(data.get("created")),
            oparl_modified=self.parse_datetime(data.get("modified")),
            name=data.get("name"),
            start_date=self.parse_datetime(data.get("startDate")),
            end_date=self.parse_datetime(data.get("endDate")),
        )

    def _extract_body_id(self, data: dict[str, Any]) -> str | None:
        """Extract body external ID from data."""
        body = data.get("body")
        if isinstance(body, str):
            return body
        elif isinstance(body, dict):
            return body.get("id")
        return None

    def _extract_refs(self, items: list[Any]) -> list[str]:
        """Extract list of external IDs from references."""
        refs = []
        for item in items:
            if isinstance(item, str):
                refs.append(item)
            elif isinstance(item, dict) and "id" in item:
                refs.append(item["id"])
        return refs

    def _extract_files(
        self,
        entity: ProcessedEntity,
        data: dict[str, Any],
        body_external_id: str | None,
    ) -> None:
        """Extract and process nested file objects."""
        file_fields = [
            "mainFile",
            "auxiliaryFile",
            "invitation",
            "resultsProtocol",
            "verbatimProtocol",
            "derivativeFile",
        ]

        file_refs: list[str] = []

        for field in file_fields:
            files = data.get(field)
            if files is None:
                continue

            # Single file
            if isinstance(files, dict):
                file_entity = self.process_file(files, body_external_id)
                entity.nested_entities.append(file_entity)
                file_refs.append(files.get("id", ""))

            # List of files
            elif isinstance(files, list):
                for f in files:
                    if isinstance(f, dict):
                        file_entity = self.process_file(f, body_external_id)
                        entity.nested_entities.append(file_entity)
                        file_refs.append(f.get("id", ""))
                    elif isinstance(f, str):
                        file_refs.append(f)

        if file_refs:
            entity.references["files"] = file_refs
