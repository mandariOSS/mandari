# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schreibende Anwendungsfälle der Sitzungsvorbereitung (Issue #160, Service-Layer).

Views parsen die Anfrage (JSON oder Formular), prüfen Berechtigung und Org-Grenze
und rufen hier hinein. Die Funktionen kapseln Validierung, Verschlüsselung, den
abgeleiteten Vorbereitungsstatus (``record_activity``) und die Echtzeit-Broadcasts.
Pfade mit mehreren Schreibzugriffen laufen in ``transaction.atomic``; fachliche
Fehler werden als ``PreparationError`` (mit HTTP-Status) gemeldet.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction
from django.utils import timezone

from . import consumers, selectors
from .models import (
    AgendaItemNote,
    AgendaItemPosition,
    AgendaPrivateNote,
    AgendaSpeechNote,
    AgendaSupplementaryDocument,
    FileAnnotation,
    MeetingPreparation,
    PaperComment,
)
from .serializers import (
    TRUE_VALUES,
    serialize_agenda_note,
    serialize_file_annotation,
    serialize_paper_comment_as_note,
    serialize_position,
    set_encrypted,
)

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile

    from apps.tenants.models import Membership, Organization
    from insight_core.models import OParlAgendaItem, OParlMeeting, OParlPaper

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class PreparationError(Exception):
    """Fachlicher Fehler mit Meldung und HTTP-Status für die JSON-Antwort."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _broadcast(organization_id: Any, payload: dict[str, Any], **groups: Any) -> None:
    """Echtzeit-Ereignis senden (Polling bleibt Fallback; Fehler brechen den Request nie)."""
    cast(Any, consumers).broadcast_preparation_event(organization_id, payload, **groups)


def record_activity(organization: Organization, meeting: OParlMeeting, membership: Membership) -> MeetingPreparation:
    """Abgeleiteter Vorbereitungsstatus: Vorbereitung anlegen/markieren (idempotent)."""
    return cast(MeetingPreparation, cast(Any, MeetingPreparation).record_activity(organization, meeting, membership))


def _truthy(value: object) -> bool:
    return value in TRUE_VALUES


# ---------------------------------------------------------------------------
# Vorbereitung (Sitzungsebene)
# ---------------------------------------------------------------------------


def ensure_preparation(organization: Organization, meeting: OParlMeeting, membership: Membership) -> MeetingPreparation:
    """Org-weite Vorbereitung (eine pro Org+Sitzung) holen oder anlegen."""
    preparation = selectors.get_preparation(organization, meeting)
    if preparation is None:
        preparation = MeetingPreparation.objects.create(
            organization=organization, meeting=meeting, membership=membership
        )
    return preparation


@transaction.atomic
def save_meeting_notes(organization: Organization, meeting: OParlMeeting, membership: Membership, notes: str) -> None:
    """Org-weite Sitzungsnotizen speichern (JSON-Auto-Save; markiert die Vorbereitung als begonnen)."""
    preparation = record_activity(organization, meeting, membership)
    set_encrypted(preparation, "notes", notes or "")
    preparation.save(update_fields=["notes_encrypted", "updated_at"])


@transaction.atomic
def apply_preparation_action(
    organization: Organization, meeting: OParlMeeting, membership: Membership, action: str | None, notes: str
) -> None:
    """
    Formular-Aktionen der Vorbereitungsseite.

    - save_notes: org-weite Notizen speichern
    - mark_prepared / unmark_prepared: DEPRECATED — is_prepared wird inzwischen aus der
      inhaltlichen Arbeit abgeleitet (record_activity). Die Actions bleiben funktionsfähig,
      bis die UI den Button entfernt.
    """
    preparation = selectors.get_preparation(organization, meeting)
    if preparation is None:
        return
    if action == "mark_prepared":
        preparation.is_prepared = True
        preparation.prepared_at = timezone.now()
        preparation.prepared_by = membership
        preparation.save()
    elif action == "unmark_prepared":
        preparation.is_prepared = False
        preparation.prepared_at = None
        preparation.prepared_by = None
        preparation.save()
    elif action == "save_notes":
        set_encrypted(preparation, "notes", notes)
        preparation.save()
        if notes.strip():
            record_activity(organization, meeting, membership)


# ---------------------------------------------------------------------------
# Sektion 1: Position
# ---------------------------------------------------------------------------


@transaction.atomic
def save_position(
    organization: Organization,
    meeting: OParlMeeting,
    agenda_item: OParlAgendaItem,
    membership: Membership,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Org-weite Position partiell und idempotent speichern (Debounce-Auto-Save).

    Nur die übergebenen Felder position / is_final / reasoning / outcome werden geändert.
    Liefert das Antwort-Payload (ohne ``success``) und sendet den Echtzeit-Broadcast.
    """
    if "position" in payload and payload["position"] not in dict(AgendaItemPosition.POSITION_CHOICES):
        raise PreparationError("Ungültige Position")
    if "outcome" in payload and payload["outcome"] not in dict(AgendaItemPosition.OUTCOME_CHOICES):
        raise PreparationError("Ungültiges Ergebnis")

    position, _ = AgendaItemPosition.objects.get_or_create(organization=organization, agenda_item=agenda_item)
    if "position" in payload:
        position.position = payload["position"]
    if "is_final" in payload:
        position.is_final = _truthy(payload["is_final"])
    if "reasoning" in payload:
        set_encrypted(position, "reasoning", payload.get("reasoning") or "")
    if "outcome" in payload:
        position.outcome = payload["outcome"]
    position.set_by = membership
    position.save()

    # Abgeleiteter Vorbereitungsstatus: erster inhaltlicher Save
    if position.position != "open" or position.outcome or position.reasoning_encrypted:
        record_activity(organization, meeting, membership)

    result = {**serialize_position(position), "set_by": membership.user.get_display_name()}
    paper = selectors.get_primary_paper_for_item(agenda_item)
    _broadcast(
        organization.id,
        {"type": "position", "event": "updated", "agenda_item_id": str(agenda_item.id), "position": result},
        agenda_item_id=agenda_item.id,
        paper_id=paper.id if paper else None,
    )
    return result


# ---------------------------------------------------------------------------
# Sektion 2: Private Notiz
# ---------------------------------------------------------------------------


@transaction.atomic
def save_private_note(
    organization: Organization, agenda_item: OParlAgendaItem, membership: Membership, content: str
) -> AgendaPrivateNote:
    """Private Notiz des Mitglieds idempotent speichern."""
    note, _ = AgendaPrivateNote.objects.get_or_create(
        author=membership, agenda_item=agenda_item, defaults={"organization": organization}
    )
    set_encrypted(note, "content", content)
    note.save()
    if content.strip():
        record_activity(organization, agenda_item.meeting, membership)
    return note


# ---------------------------------------------------------------------------
# Sektion 3: Redebeitrag
# ---------------------------------------------------------------------------


@transaction.atomic
def save_speech_note(
    organization: Organization, agenda_item: OParlAgendaItem, membership: Membership, payload: Mapping[str, Any]
) -> AgendaSpeechNote:
    """
    Redebeitrag partiell speichern: title / content / estimated_duration / is_shared / linked_document.

    content enthält HTML (WYSIWYG); es wird nichts gestrippt — nur Ausgabe-Views sanitizen.
    Ein verknüpftes Dokument wird VOR dem Anlegen geprüft (403 ohne Seiteneffekt).
    """
    linked_document = None
    if "linked_document" in payload and payload.get("linked_document"):
        linked_document = selectors.find_linkable_document(organization, membership, payload["linked_document"])
        if linked_document is None:
            raise PreparationError("Kein Zugriff auf dieses Dokument", status=403)

    note, _ = AgendaSpeechNote.objects.get_or_create(
        author=membership, agenda_item=agenda_item, defaults={"organization": organization}
    )
    if "content" in payload:
        set_encrypted(note, "content", payload.get("content") or "")
    if "title" in payload:
        note.title = payload.get("title") or ""
    if "estimated_duration" in payload:
        with contextlib.suppress(TypeError, ValueError):
            note.estimated_duration = max(0, int(payload.get("estimated_duration") or 0))
    if "is_shared" in payload:
        note.is_shared = _truthy(payload.get("is_shared"))
    if "linked_document" in payload:
        note.linked_document = linked_document
    note.save()

    record_activity(organization, agenda_item.meeting, membership)
    return note


def delete_speech_note(membership: Membership, item_id: Any) -> None:
    """Eigenen Redebeitrag zu einem TOP löschen."""
    AgendaSpeechNote.objects.filter(author=membership, agenda_item_id=item_id).delete()


# ---------------------------------------------------------------------------
# Sektion 4: Diskussions-Thread
# ---------------------------------------------------------------------------


def _visibility(value: object, choices: list[tuple[str, str]]) -> str:
    visibility = str(value) if value is not None else "organization"
    return visibility if visibility in dict(choices) else "organization"


@transaction.atomic
def create_thread_note(
    organization: Organization,
    meeting: OParlMeeting,
    agenda_item: OParlAgendaItem,
    membership: Membership,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Beitrag im Diskussions-Thread eines TOPs anlegen.

    TOP MIT Vorlage: PaperComment (im gesamten Beratungsverlauf sichtbar);
    TOP OHNE Vorlage: org-lokale AgendaItemNote. Liefert den serialisierten Beitrag;
    nicht-private Beiträge werden gebroadcastet.
    """
    content = str(payload.get("content", "")).strip()
    if not content:
        raise PreparationError("Content required")
    is_decision = _truthy(payload.get("is_decision", False))
    visibility = _visibility(payload.get("visibility", "organization"), AgendaItemNote.VISIBILITY_CHOICES)

    paper = selectors.get_primary_paper_for_item(agenda_item)
    if paper:
        comment = PaperComment(
            paper=paper,
            organization=organization,
            author=membership,
            visibility=visibility,
            is_recommendation=is_decision,
        )
        set_encrypted(comment, "content", content)
        comment.save()
        serialized = serialize_paper_comment_as_note(comment, membership)
    else:
        note = AgendaItemNote(
            organization=organization,
            agenda_item=agenda_item,
            author=membership,
            visibility=visibility,
            is_decision=is_decision,
        )
        set_encrypted(note, "content", content)
        note.save()
        serialized = serialize_agenda_note(note, membership)

    record_activity(organization, meeting, membership)

    # Private Beiträge NICHT broadcasten — sie sind nur für den Autor sichtbar;
    # der Autor erhält das Objekt über die Antwort.
    if visibility != "private":
        _broadcast(
            organization.id,
            {"type": "comment", "event": "created", "agenda_item_id": str(agenda_item.id), "comment": serialized},
            agenda_item_id=agenda_item.id,
            paper_id=paper.id if paper else None,
        )
    return serialized


def delete_thread_note(membership: Membership, note_id: Any) -> bool:
    """Eigenen Thread-Beitrag löschen (Alt-Notiz ODER PaperComment); ``False``, wenn nichts gefunden."""
    note = AgendaItemNote.objects.filter(id=note_id, author=membership).first()
    if note is not None:
        agenda_item_id = note.agenda_item_id
        organization_id = note.organization_id
        note.delete()
        _broadcast(
            organization_id,
            {"type": "comment", "event": "deleted", "comment_id": str(note_id)},
            agenda_item_id=agenda_item_id,
        )
        return True
    return delete_paper_comment(membership, note_id)


def create_paper_comment(
    organization: Organization, paper: OParlPaper, membership: Membership, payload: Mapping[str, Any]
) -> PaperComment:
    """Kommentar zu einer Vorlage anlegen (gremienübergreifend); nicht-private werden gebroadcastet."""
    content = str(payload.get("content", "")).strip()
    if not content:
        raise PreparationError("Content required")
    visibility = _visibility(payload.get("visibility", "organization"), PaperComment.VISIBILITY_CHOICES)
    is_recommendation = _truthy(payload.get("is_recommendation", False))

    comment = PaperComment(
        paper=paper,
        organization=organization,
        author=membership,
        visibility=visibility,
        is_recommendation=is_recommendation,
    )
    set_encrypted(comment, "content", content)
    comment.save()

    if visibility != "private":
        _broadcast(
            organization.id,
            {"type": "comment", "event": "created", "comment": serialize_paper_comment_as_note(comment, membership)},
            paper_id=paper.id,
        )
    return comment


def delete_paper_comment(membership: Membership, comment_id: Any) -> bool:
    """Eigenen Vorlagen-Kommentar löschen; ``False``, wenn nicht gefunden."""
    comment = PaperComment.objects.filter(id=comment_id, author=membership).first()
    if comment is None:
        return False
    paper_id = comment.paper_id
    organization_id = comment.organization_id
    comment.delete()
    _broadcast(
        organization_id,
        {"type": "comment", "event": "deleted", "comment_id": str(comment_id)},
        paper_id=paper_id,
    )
    return True


# ---------------------------------------------------------------------------
# Sektion 5: Ergänzende Dokumente
# ---------------------------------------------------------------------------


def resolve_paper_anchor(
    agenda_item: OParlAgendaItem, paper_id: object, share_flag: object
) -> tuple[OParlPaper | None, bool]:
    """
    Vorlagen-Anker validieren: Die Vorlage muss tatsächlich von diesem TOP beraten werden
    (sonst könnte man beliebige Papers verknüpfen). Liefert (paper, share_across_committees).
    """
    if not paper_id:
        return None, False
    papers = {str(p.id): p for p in selectors.papers_of_item(agenda_item)}
    paper = papers.get(str(paper_id))
    if paper is None:
        return None, False
    return paper, _truthy(share_flag)


@transaction.atomic
def add_document_link(
    organization: Organization,
    meeting: OParlMeeting,
    agenda_item: OParlAgendaItem,
    membership: Membership,
    payload: Mapping[str, Any],
) -> AgendaSupplementaryDocument:
    """Link (oder OParl-Referenz) als ergänzendes Dokument anlegen."""
    title = str(payload.get("title", "")).strip()
    if not title:
        raise PreparationError("Titel erforderlich")
    paper, share_across = resolve_paper_anchor(
        agenda_item, payload.get("paper_id"), payload.get("share_across_committees", False)
    )
    doc = AgendaSupplementaryDocument.objects.create(
        organization=organization,
        added_by=membership,
        agenda_item=agenda_item,
        paper=paper,
        share_across_committees=share_across,
        document_type=payload.get("document_type", "link"),
        title=title,
        url=payload.get("url", ""),
        description=payload.get("description", ""),
    )
    record_activity(organization, meeting, membership)
    return doc


@transaction.atomic
def add_document_upload(
    organization: Organization,
    agenda_item: OParlAgendaItem,
    membership: Membership,
    uploaded_file: UploadedFile[Any] | None,
    *,
    title: str,
    paper_id: object,
    share_flag: object,
    description: str,
) -> AgendaSupplementaryDocument:
    """Datei-Upload (max. 50 MB) als ergänzendes Dokument anlegen."""
    if not uploaded_file:
        raise PreparationError("Keine Datei")
    if not title:
        title = uploaded_file.name or ""
    if (uploaded_file.size or 0) > MAX_UPLOAD_BYTES:
        raise PreparationError("Datei zu groß (max. 50 MB)")
    paper, share_across = resolve_paper_anchor(agenda_item, paper_id, share_flag)
    doc = AgendaSupplementaryDocument.objects.create(
        organization=organization,
        added_by=membership,
        agenda_item=agenda_item,
        paper=paper,
        share_across_committees=share_across,
        document_type="file",
        title=title,
        file=uploaded_file,
        filename=uploaded_file.name or "",
        mime_type=uploaded_file.content_type or "",
        file_size=uploaded_file.size or 0,
        description=description,
    )
    record_activity(organization, agenda_item.meeting, membership)
    return doc


@transaction.atomic
def delete_document(membership: Membership, doc_id: Any) -> bool:
    """Eigenes ergänzendes Dokument samt Datei löschen; ``False``, wenn nicht gefunden."""
    doc = AgendaSupplementaryDocument.objects.filter(id=doc_id, added_by=membership).first()
    if doc is None:
        return False
    if doc.file:
        doc.file.delete(save=False)
    doc.delete()
    return True


# ---------------------------------------------------------------------------
# Datei-Anmerkungen
# ---------------------------------------------------------------------------


def add_file_annotation(
    organization: Organization, membership: Membership, anchor: dict[str, Any], payload: Mapping[str, Any]
) -> tuple[dict[str, Any], int]:
    """Seitenbezogene Anmerkung an einer PDF anlegen; liefert (serialisierte Anmerkung, Gesamtzahl am Anker)."""
    content = str(payload.get("content") or "").strip()
    if not content:
        raise PreparationError("Inhalt erforderlich")
    try:
        page = max(1, int(payload.get("page") or 1))
    except (TypeError, ValueError):
        page = 1

    annotation = FileAnnotation(organization=organization, author=membership, page=page, **anchor)
    set_encrypted(annotation, "content", content)
    annotation.save()
    count = selectors.count_file_annotations(organization, anchor)
    return serialize_file_annotation(annotation, membership), count


def delete_file_annotation(organization: Organization, membership: Membership, annotation_id: Any) -> bool:
    """Anmerkung löschen — nur der Autor (403), fremde Organisationen sehen sie nicht (``False`` → 404)."""
    annotation = FileAnnotation.objects.filter(id=annotation_id, organization=organization).first()
    if annotation is None:
        return False
    if annotation.author_id != membership.id:
        raise PreparationError("Nur der Autor darf löschen", status=403)
    annotation.delete()
    return True
