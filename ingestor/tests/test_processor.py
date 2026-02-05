"""
Tests for the OParl Entity Processor.

Uses sample data from the _OParl Muster Data folder.
"""

import json
from pathlib import Path
from uuid import UUID

import pytest

from src.sync.processor import (
    OParlProcessor,
    OParlType,
    ProcessedAgendaItem,
    ProcessedBody,
    ProcessedFile,
    ProcessedLocation,
    ProcessedMeeting,
    ProcessedOrganization,
    ProcessedPaper,
    ProcessedPerson,
)


# Path to sample data
SAMPLE_DATA_PATH = Path(__file__).parent.parent.parent.parent.parent / "_OParl Muster Data"


@pytest.fixture
def processor() -> OParlProcessor:
    """Create a processor instance."""
    return OParlProcessor()


@pytest.fixture
def sample_system() -> dict:
    """Load sample system.json."""
    with open(SAMPLE_DATA_PATH / "system.json") as f:
        return json.load(f)


@pytest.fixture
def sample_bodies() -> dict:
    """Load sample bodies.json."""
    with open(SAMPLE_DATA_PATH / "bodies.json") as f:
        return json.load(f)


@pytest.fixture
def sample_meetings() -> dict:
    """Load sample meetings.json."""
    with open(SAMPLE_DATA_PATH / "meetings.json") as f:
        return json.load(f)


@pytest.fixture
def sample_papers() -> dict:
    """Load sample papers.json."""
    with open(SAMPLE_DATA_PATH / "papers.json") as f:
        return json.load(f)


class TestTypeDetection:
    """Tests for OParl type detection."""

    def test_detect_body_type(self, processor: OParlProcessor) -> None:
        """Test detection of Body type."""
        data = {"type": "https://schema.oparl.org/1.1/Body"}
        assert processor.get_type(data) == OParlType.BODY

    def test_detect_meeting_type(self, processor: OParlProcessor) -> None:
        """Test detection of Meeting type."""
        data = {"type": "https://schema.oparl.org/1.1/Meeting"}
        assert processor.get_type(data) == OParlType.MEETING

    def test_detect_paper_type(self, processor: OParlProcessor) -> None:
        """Test detection of Paper type."""
        data = {"type": "https://schema.oparl.org/1.1/Paper"}
        assert processor.get_type(data) == OParlType.PAPER

    def test_detect_person_type(self, processor: OParlProcessor) -> None:
        """Test detection of Person type."""
        data = {"type": "https://schema.oparl.org/1.1/Person"}
        assert processor.get_type(data) == OParlType.PERSON

    def test_detect_organization_type(self, processor: OParlProcessor) -> None:
        """Test detection of Organization type."""
        data = {"type": "https://schema.oparl.org/1.1/Organization"}
        assert processor.get_type(data) == OParlType.ORGANIZATION

    def test_detect_unknown_type(self, processor: OParlProcessor) -> None:
        """Test detection of unknown type."""
        data = {"type": "https://schema.oparl.org/1.1/Unknown"}
        assert processor.get_type(data) is None


class TestUUIDGeneration:
    """Tests for deterministic UUID generation."""

    def test_uuid_is_deterministic(self, processor: OParlProcessor) -> None:
        """Test that UUID generation is deterministic."""
        external_id = "https://example.org/bodies/123"
        uuid1 = processor.generate_uuid(external_id)
        uuid2 = processor.generate_uuid(external_id)
        assert uuid1 == uuid2

    def test_different_ids_get_different_uuids(self, processor: OParlProcessor) -> None:
        """Test that different IDs get different UUIDs."""
        uuid1 = processor.generate_uuid("https://example.org/bodies/123")
        uuid2 = processor.generate_uuid("https://example.org/bodies/456")
        assert uuid1 != uuid2

    def test_uuid_is_valid(self, processor: OParlProcessor) -> None:
        """Test that generated UUID is valid."""
        external_id = "https://example.org/bodies/123"
        uuid = processor.generate_uuid(external_id)
        assert isinstance(uuid, UUID)


class TestDatetimeParsing:
    """Tests for datetime parsing."""

    def test_parse_iso_datetime_with_timezone(self, processor: OParlProcessor) -> None:
        """Test parsing ISO datetime with timezone."""
        dt = processor.parse_datetime("2026-02-10T16:00:00+01:00")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.day == 10
        assert dt.hour == 16

    def test_parse_iso_datetime_with_z(self, processor: OParlProcessor) -> None:
        """Test parsing ISO datetime with Z suffix."""
        dt = processor.parse_datetime("2026-02-10T16:00:00Z")
        assert dt is not None
        assert dt.year == 2026

    def test_parse_date_only(self, processor: OParlProcessor) -> None:
        """Test parsing date-only string."""
        dt = processor.parse_datetime("2026-02-10")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.day == 10

    def test_parse_none(self, processor: OParlProcessor) -> None:
        """Test parsing None value."""
        assert processor.parse_datetime(None) is None

    def test_parse_invalid(self, processor: OParlProcessor) -> None:
        """Test parsing invalid datetime."""
        assert processor.parse_datetime("not a date") is None


class TestBodyProcessing:
    """Tests for Body processing."""

    def test_process_body(self, processor: OParlProcessor, sample_bodies: dict) -> None:
        """Test processing a body from sample data."""
        body_data = sample_bodies["data"][0]
        body = processor.process_body(body_data)

        assert isinstance(body, ProcessedBody)
        assert body.external_id == body_data["id"]
        assert body.name == "Stadt Münster"
        assert body.oparl_type == OParlType.BODY
        assert body.raw_json == body_data

    def test_body_has_list_urls(self, processor: OParlProcessor, sample_bodies: dict) -> None:
        """Test that body has list URLs extracted."""
        body_data = sample_bodies["data"][0]
        body = processor.process_body(body_data)

        assert body.organization_list_url is not None
        assert body.person_list_url is not None
        assert body.meeting_list_url is not None
        assert body.paper_list_url is not None


class TestMeetingProcessing:
    """Tests for Meeting processing."""

    def test_process_meeting(self, processor: OParlProcessor, sample_meetings: dict) -> None:
        """Test processing a meeting from sample data."""
        meeting_data = sample_meetings["data"][0]
        meeting = processor.process_meeting(meeting_data, "https://example.org/body")

        assert isinstance(meeting, ProcessedMeeting)
        assert meeting.external_id == meeting_data["id"]
        assert meeting.name == "Sitzung"
        assert meeting.oparl_type == OParlType.MEETING

    def test_meeting_with_nested_location(self, processor: OParlProcessor, sample_meetings: dict) -> None:
        """Test that nested location is extracted."""
        # Find a meeting with location
        meeting_data = sample_meetings["data"][0]
        meeting = processor.process_meeting(meeting_data, "https://example.org/body")

        assert meeting.location_name is not None or meeting.location_external_id is not None

    def test_meeting_with_agenda_items(self, processor: OParlProcessor, sample_meetings: dict) -> None:
        """Test that nested agenda items are extracted."""
        # Find a meeting with agenda items
        for m in sample_meetings["data"]:
            if "agendaItem" in m:
                meeting = processor.process_meeting(m, "https://example.org/body")

                # Check nested entities
                agenda_items = [e for e in meeting.nested_entities if isinstance(e, ProcessedAgendaItem)]
                assert len(agenda_items) > 0
                break

    def test_meeting_with_files(self, processor: OParlProcessor, sample_meetings: dict) -> None:
        """Test that nested files are extracted."""
        # Find a meeting with files
        for m in sample_meetings["data"]:
            if "invitation" in m or "auxiliaryFile" in m:
                meeting = processor.process_meeting(m, "https://example.org/body")

                # Check nested entities
                files = [e for e in meeting.nested_entities if isinstance(e, ProcessedFile)]
                assert len(files) > 0
                break


class TestPaperProcessing:
    """Tests for Paper processing."""

    def test_process_paper(self, processor: OParlProcessor, sample_papers: dict) -> None:
        """Test processing a paper from sample data."""
        paper_data = sample_papers["data"][0]
        paper = processor.process_paper(paper_data, "https://example.org/body")

        assert isinstance(paper, ProcessedPaper)
        assert paper.external_id == paper_data["id"]
        assert paper.oparl_type == OParlType.PAPER

    def test_paper_with_nested_files(self, processor: OParlProcessor, sample_papers: dict) -> None:
        """Test that nested files are extracted from papers."""
        for p in sample_papers["data"]:
            if "mainFile" in p or "auxiliaryFile" in p:
                paper = processor.process_paper(p, "https://example.org/body")

                # Check nested entities
                files = [e for e in paper.nested_entities if isinstance(e, ProcessedFile)]
                assert len(files) > 0
                break


class TestGenericProcessing:
    """Tests for generic process() method."""

    def test_process_auto_detects_body(self, processor: OParlProcessor, sample_bodies: dict) -> None:
        """Test that process() auto-detects body type."""
        body_data = sample_bodies["data"][0]
        result = processor.process(body_data)

        assert isinstance(result, ProcessedBody)

    def test_process_auto_detects_meeting(self, processor: OParlProcessor, sample_meetings: dict) -> None:
        """Test that process() auto-detects meeting type."""
        meeting_data = sample_meetings["data"][0]
        result = processor.process(meeting_data, "https://example.org/body")

        assert isinstance(result, ProcessedMeeting)

    def test_process_returns_none_for_unknown(self, processor: OParlProcessor) -> None:
        """Test that process() returns None for unknown types."""
        data = {"type": "https://schema.oparl.org/1.1/Unknown", "id": "test"}
        result = processor.process(data)

        assert result is None


class TestAgendaItemProcessing:
    """Tests for AgendaItem processing."""

    def test_process_agenda_item(self, processor: OParlProcessor, sample_meetings: dict) -> None:
        """Test processing an agenda item."""
        # Find a meeting with agenda items
        for m in sample_meetings["data"]:
            if "agendaItem" in m and len(m["agendaItem"]) > 0:
                ai_data = m["agendaItem"][0]
                ai = processor.process_agenda_item(ai_data, "https://example.org/body")

                assert isinstance(ai, ProcessedAgendaItem)
                assert ai.external_id == ai_data["id"]
                assert ai.number == ai_data.get("number")
                assert ai.order == ai_data.get("order")
                assert ai.name == ai_data.get("name")
                break


class TestFileProcessing:
    """Tests for File processing."""

    def test_process_file(self, processor: OParlProcessor, sample_meetings: dict) -> None:
        """Test processing a file."""
        # Find a meeting with files
        for m in sample_meetings["data"]:
            if "invitation" in m and isinstance(m["invitation"], dict):
                file_data = m["invitation"]
                file = processor.process_file(file_data, "https://example.org/body")

                assert isinstance(file, ProcessedFile)
                assert file.external_id == file_data["id"]
                assert file.name == file_data.get("name")
                assert file.file_name == file_data.get("fileName")
                assert file.mime_type == file_data.get("mimeType")
                break


class TestLocationProcessing:
    """Tests for Location processing."""

    def test_process_location(self, processor: OParlProcessor, sample_meetings: dict) -> None:
        """Test processing a location."""
        # Find a meeting with location
        for m in sample_meetings["data"]:
            if "location" in m and isinstance(m["location"], dict):
                loc_data = m["location"]
                loc = processor.process_location(loc_data, "https://example.org/body")

                assert isinstance(loc, ProcessedLocation)
                assert loc.external_id == loc_data["id"]
                assert loc.room == loc_data.get("room")
                break
