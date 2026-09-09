# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Service- und Selector-Tests der Sitzungsvorbereitung (Issue #160, Service-Layer).

Geprüft werden die Fachregeln unabhängig von den Views: Org-Grenze über die
Körperschaften, partielle Saves, abgeleiteter Vorbereitungsstatus, Thread-Routing
(PaperComment vs. AgendaItemNote), Anlagen-Anker und Datei-Anmerkungen.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.http import Http404
from django.utils import timezone

from apps.work.meetings import selectors, services
from apps.work.meetings.models import (
    AgendaItemNote,
    AgendaItemPosition,
    AgendaSpeechNote,
    AgendaSupplementaryDocument,
    FileAnnotation,
    MeetingPreparation,
    PaperComment,
)
from apps.work.meetings.serializers import build_prepare_config, decrypted, serialize_position
from apps.work.meetings.services import PreparationError
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)


@pytest.fixture
def body(org: Any) -> OParlBody:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    return body


@pytest.fixture
def meeting(body: OParlBody) -> OParlMeeting:
    return OParlMeeting.objects.create(
        external_id="https://ris.example.org/meeting/1",
        body=body,
        name="Rat",
        start=timezone.now() + timedelta(days=3),
    )


@pytest.fixture
def item(meeting: OParlMeeting) -> OParlAgendaItem:
    return OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/1", meeting=meeting, number="1", name="Haushalt", order=1
    )


@pytest.fixture
def paper_item(body: OParlBody, meeting: OParlMeeting) -> tuple[OParlAgendaItem, OParlPaper]:
    """TOP mit beratener Vorlage (Consultation-Verknüpfung über external_id)."""
    agenda_item = OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/2", meeting=meeting, number="2", name="Spielplatz", order=2
    )
    paper = OParlPaper.objects.create(
        external_id="https://ris.example.org/paper/1", body=body, name="Antrag Spielplatz", reference="V/2026/1"
    )
    OParlConsultation.objects.create(
        external_id="https://ris.example.org/consultation/1",
        body=body,
        paper=paper,
        agenda_item_external_id=agenda_item.external_id,
        meeting_external_id=meeting.external_id,
        role="Entscheidung",
        authoritative=True,
    )
    return agenda_item, paper


@pytest.fixture
def member(org: Any, make_member: Any) -> Any:
    return make_member(org, ["meetings.prepare"], email="vorbereiter@example.org")


@pytest.fixture
def other_member(org: Any, make_member: Any) -> Any:
    return make_member(org, ["meetings.prepare"], email="kollegin@example.org")


# ---------------------------------------------------------------------------
# Selectors: Org-Grenze
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_organization_bodies_is_none_without_linked_body(org: Any) -> None:
    assert selectors.organization_bodies(org) is None
    assert selectors.organization_bodies(None) is None


@pytest.mark.django_db
def test_get_meeting_or_404_respects_body_boundary(org: Any, body: OParlBody, meeting: OParlMeeting) -> None:
    bodies = selectors.organization_bodies(org)
    assert bodies is not None
    assert selectors.get_meeting_or_404(bodies, meeting.id) == meeting

    foreign_body = OParlBody.objects.create(
        external_id="https://ris.example.org/body/2", source=body.source, name="Andere Stadt"
    )
    foreign_meeting = OParlMeeting.objects.create(external_id="https://ris.example.org/meeting/2", body=foreign_body)
    with pytest.raises(Http404):
        selectors.get_meeting_or_404(bodies, foreign_meeting.id)


@pytest.mark.django_db
def test_meetings_for_list_filters_by_time_window(body: OParlBody, meeting: OParlMeeting) -> None:
    bodies = OParlBody.objects.filter(pk=body.pk)
    past = OParlMeeting.objects.create(
        external_id="https://ris.example.org/meeting/past",
        body=body,
        name="Alte Sitzung",
        start=timezone.now() - timedelta(days=10),
    )
    now = timezone.now()
    assert selectors.meetings_for_list(bodies, "upcoming", now) == [meeting]
    assert selectors.meetings_for_list(bodies, "past", now) == [past]
    assert set(selectors.meetings_for_list(bodies, "all", now)) == {meeting, past}

    filtered = selectors.filter_meetings([meeting, past], committee_ids=[], committee_filter="", search_query="alte")
    assert filtered == [past]


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_save_position_partial_update_and_derived_status(
    org: Any, meeting: OParlMeeting, item: OParlAgendaItem, member: Any
) -> None:
    payload = services.save_position(org, meeting, item, member, {"position": "for", "reasoning": "Passt."})

    assert payload["position"] == "for"
    assert payload["position_display"] == "Zustimmung"
    assert payload["reasoning"] == "Passt."
    assert payload["set_by"] == member.user.get_display_name()
    assert "success" not in payload

    position = AgendaItemPosition.objects.get(organization=org, agenda_item=item)
    assert decrypted(position, "reasoning") == "Passt."
    assert serialize_position(position)["outcome_display"] == ""

    # Erster inhaltlicher Save markiert die Vorbereitung (abgeleiteter Status)
    preparation = MeetingPreparation.objects.get(organization=org, meeting=meeting)
    assert preparation.is_prepared is True
    assert preparation.prepared_by == member

    # Partieller Save ändert nur das übergebene Feld
    services.save_position(org, meeting, item, member, {"is_final": "on"})
    position.refresh_from_db()
    assert position.is_final is True
    assert position.position == "for"


@pytest.mark.django_db
def test_save_position_rejects_invalid_values_without_side_effects(
    org: Any, meeting: OParlMeeting, item: OParlAgendaItem, member: Any
) -> None:
    with pytest.raises(PreparationError) as excinfo:
        services.save_position(org, meeting, item, member, {"position": "maybe"})
    assert excinfo.value.status == 400
    with pytest.raises(PreparationError):
        services.save_position(org, meeting, item, member, {"outcome": "unknown"})
    assert not AgendaItemPosition.objects.filter(agenda_item=item).exists()


# ---------------------------------------------------------------------------
# Private Notiz und Redebeitrag
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_save_private_note_is_per_member_and_encrypted(
    org: Any, meeting: OParlMeeting, item: OParlAgendaItem, member: Any, other_member: Any
) -> None:
    services.save_private_note(org, item, member, "   ")
    assert not MeetingPreparation.objects.filter(organization=org, meeting=meeting).exists()

    note = services.save_private_note(org, item, member, "Nachfragen zum Budget")
    assert note.content_encrypted != "Nachfragen zum Budget"
    assert decrypted(note, "content") == "Nachfragen zum Budget"
    assert selectors.get_private_note(member, item.id) == note
    assert selectors.get_private_note(other_member, item.id) is None
    assert MeetingPreparation.objects.filter(organization=org, meeting=meeting, is_prepared=True).exists()


@pytest.mark.django_db
def test_save_speech_note_rejects_inaccessible_document_before_creating(
    org: Any, item: OParlAgendaItem, member: Any, other_member: Any
) -> None:
    from apps.work.motions.models import Motion

    private_doc = Motion.objects.create(organization=org, author=other_member, title="Privat", visibility="private")

    with pytest.raises(PreparationError) as excinfo:
        services.save_speech_note(org, item, member, {"linked_document": str(private_doc.id), "title": "x"})
    assert excinfo.value.status == 403
    assert not AgendaSpeechNote.objects.filter(author=member).exists()

    note = services.save_speech_note(
        org, item, member, {"content": "<p>Rede</p>", "estimated_duration": "abc", "is_shared": "true"}
    )
    assert decrypted(note, "content") == "<p>Rede</p>"
    assert note.estimated_duration == 0
    assert note.is_shared is True
    assert list(selectors.shared_speeches(org, item, exclude_author=other_member)) == [note]
    assert list(selectors.shared_speeches(org, item, exclude_author=member)) == []

    services.delete_speech_note(member, item.id)
    assert selectors.get_own_speech(member, item) is None


# ---------------------------------------------------------------------------
# Diskussions-Thread
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_thread_note_routes_to_agenda_note_without_paper(
    org: Any, meeting: OParlMeeting, item: OParlAgendaItem, member: Any, other_member: Any
) -> None:
    with pytest.raises(PreparationError):
        services.create_thread_note(org, meeting, item, member, {"content": "  "})

    serialized = services.create_thread_note(
        org, meeting, item, member, {"content": "Diskussion", "visibility": "unbekannt", "is_decision": "1"}
    )
    assert serialized["source"] == "agenda_note"
    assert serialized["visibility"] == "organization"
    assert serialized["is_decision"] is True
    note = AgendaItemNote.objects.get(id=serialized["id"])
    assert decrypted(note, "content") == "Diskussion"
    assert selectors.thread_notes(org, item.id) == [note]

    # Nur der Autor darf löschen; fremde IDs liefern False (-> 404 in der View)
    assert services.delete_thread_note(other_member, note.id) is False
    assert services.delete_thread_note(member, note.id) is True
    assert selectors.thread_notes(org, item.id) == []


@pytest.mark.django_db
def test_create_thread_note_uses_paper_comment_for_items_with_paper(
    org: Any, meeting: OParlMeeting, paper_item: tuple[OParlAgendaItem, OParlPaper], member: Any
) -> None:
    agenda_item, paper = paper_item
    assert selectors.get_primary_paper_for_item(agenda_item) == paper

    serialized = services.create_thread_note(org, meeting, agenda_item, member, {"content": "Zur Vorlage"})
    assert serialized["source"] == "paper_comment"
    comment = PaperComment.objects.get(id=serialized["id"])
    assert comment.paper == paper
    assert comment.organization == org
    assert selectors.visible_paper_comments(paper, member) == [comment]

    assert services.delete_thread_note(member, comment.id) is True
    assert not PaperComment.objects.filter(id=comment.id).exists()


# ---------------------------------------------------------------------------
# Anlagen und Datei-Anmerkungen
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_add_document_link_validates_title_and_paper_anchor(
    org: Any, body: OParlBody, meeting: OParlMeeting, paper_item: tuple[OParlAgendaItem, OParlPaper], member: Any
) -> None:
    agenda_item, paper = paper_item
    with pytest.raises(PreparationError):
        services.add_document_link(org, meeting, agenda_item, member, {"title": " ", "url": "https://x.example"})

    foreign_paper = OParlPaper.objects.create(external_id="https://ris.example.org/paper/99", body=body)
    doc = services.add_document_link(
        org,
        meeting,
        agenda_item,
        member,
        {"title": "Fremde Vorlage", "url": "https://x.example", "paper_id": str(foreign_paper.id)},
    )
    assert doc.paper is None  # nicht von diesem TOP beraten -> kein Anker
    assert doc.share_across_committees is False

    shared = services.add_document_link(
        org,
        meeting,
        agenda_item,
        member,
        {"title": "Gutachten", "url": "https://x.example/g", "paper_id": str(paper.id), "share_across_committees": "1"},
    )
    assert shared.paper == paper
    assert shared.share_across_committees is True

    docs = selectors.documents_with_annotation_counts(org, agenda_item)
    assert {d.id for d, _count in docs} == {doc.id, shared.id}
    assert all(count == 0 for _doc, count in docs)

    assert services.delete_document(member, doc.id) is True
    assert not AgendaSupplementaryDocument.objects.filter(id=doc.id).exists()
    assert services.delete_document(member, doc.id) is False


@pytest.mark.django_db
def test_file_annotations_are_org_bound_and_author_deletable(
    org: Any, body: OParlBody, member: Any, other_member: Any, make_member: Any
) -> None:
    ris_file = OParlFile.objects.create(external_id="https://ris.example.org/file/1", body=body, name="Anlage.pdf")

    with pytest.raises(Http404):
        selectors.resolve_file_anchor(org, "unknown", ris_file.id)
    anchor = selectors.resolve_file_anchor(org, "oparl", ris_file.id)
    assert anchor == {"oparl_file": ris_file}

    with pytest.raises(PreparationError):
        services.add_file_annotation(org, member, anchor, {"content": "", "page": "2"})
    serialized, count = services.add_file_annotation(org, member, anchor, {"content": "Seite 2 prüfen", "page": "x"})
    assert serialized["page"] == 1
    assert count == 1
    annotation = FileAnnotation.objects.get(id=serialized["id"])
    assert selectors.file_annotations(org, anchor) == [annotation]

    # Nur der Autor darf löschen (403); andere Organisationen sehen die Anmerkung nicht (False -> 404)
    with pytest.raises(PreparationError) as excinfo:
        services.delete_file_annotation(org, other_member, annotation.id)
    assert excinfo.value.status == 403

    from apps.common.tests.factories import OrganizationFactory

    organization_factory: Any = OrganizationFactory
    other_org = organization_factory(name="Andere Fraktion", slug="andere-fraktion")
    stranger = make_member(other_org, ["meetings.prepare"], email="fremd@example.org")
    assert services.delete_file_annotation(other_org, stranger, annotation.id) is False
    assert services.delete_file_annotation(org, member, annotation.id) is True


# ---------------------------------------------------------------------------
# Vorbereitungsseite und Zusammenfassung
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_load_preparation_data_and_prepare_config(
    org: Any, meeting: OParlMeeting, item: OParlAgendaItem, paper_item: tuple[OParlAgendaItem, OParlPaper], member: Any
) -> None:
    agenda_item, paper = paper_item
    services.save_position(org, meeting, item, member, {"position": "against"})
    services.save_speech_note(org, agenda_item, member, {"title": "Mein Beitrag"})
    preparation = services.ensure_preparation(org, meeting, member)
    services.save_meeting_notes(org, meeting, member, "Allgemeines")

    data = selectors.load_preparation_data(org, member, meeting)
    assert [entry.item for entry in data.prepared_items] == [item, agenda_item]
    assert data.prepared_items[0].position is not None
    assert data.prepared_items[1].primary_paper == paper
    assert data.stats == {"total_items": 2, "positioned": 1, "want_to_speak": 1, "with_notes": 0}
    assert data.consultations_by_paper[paper.id][0]["isCurrent"] is True
    assert "_sort" not in data.consultations_by_paper[paper.id][0]

    preparation.refresh_from_db()
    config = build_prepare_config(
        organization=org, meeting=meeting, preparation=preparation, data=data, current_user_name="Test"
    )
    assert config["orgSlug"] == org.slug
    assert config["orgNotes"] == "Allgemeines"
    assert [entry["name"] for entry in config["items"]] == ["Haushalt", "Spielplatz"]
    assert config["items"][0]["position"] == "against"
    assert config["items"][1]["paper"]["reference"] == "V/2026/1"
    assert config["items"][1]["speechTitle"] == "Mein Beitrag"
    assert config["urls"]["summary"].endswith(f"/meetings/{meeting.id}/summary/")


@pytest.mark.django_db
def test_group_positions_by_type_and_preparation_actions(
    org: Any, meeting: OParlMeeting, item: OParlAgendaItem, member: Any
) -> None:
    services.save_position(org, meeting, item, member, {"position": "defer"})
    positions_by_type, sections, has_positions = selectors.group_positions_by_type(
        selectors.positions_for_meeting(org, meeting)
    )
    assert has_positions is True
    assert len(positions_by_type["defer"]) == 1
    assert "open" not in {section["code"] for section in sections}

    services.apply_preparation_action(org, meeting, member, "unmark_prepared", "")
    preparation = selectors.get_preparation(org, meeting)
    assert preparation is not None
    assert preparation.is_prepared is False
    services.apply_preparation_action(org, meeting, member, "save_notes", "Notiz")
    preparation.refresh_from_db()
    assert decrypted(preparation, "notes") == "Notiz"
    assert preparation.is_prepared is True
