# SPDX-License-Identifier: AGPL-3.0-or-later
"""
JSON-Darstellungen der Sitzungsvorbereitung (Issue #160, Service-Layer).

Reine Funktionen ohne Datenbankzugriff: Sie bilden Modelle auf die Strukturen ab, die
die API-Antworten, die Echtzeit-Broadcasts und die Alpine-Komponente ``preparationApp``
(``frontend/alpine/prepare-meeting.ts``) erwarten. Werden von Views und Services
gemeinsam genutzt, damit Antwort und Broadcast identisch bleiben.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from django.urls import reverse
from django.utils import timezone

if TYPE_CHECKING:
    from apps.tenants.models import Membership, Organization
    from insight_core.models import OParlFile, OParlMeeting

    from .models import (
        AgendaItemNote,
        AgendaItemPosition,
        AgendaSpeechNote,
        AgendaSupplementaryDocument,
        FileAnnotation,
        MeetingPreparation,
        PaperComment,
    )
    from .selectors import PreparationData, PreparedItem

TRUE_VALUES = (True, "true", "1", "on")


def decrypted(obj: object, field: str) -> str:
    """Entschlüsselten Wert eines ``EncryptedTextField`` lesen (``get_<feld>_decrypted``)."""
    return str(getattr(obj, f"get_{field}_decrypted")())


def set_encrypted(obj: object, field: str, value: str) -> None:
    """Wert eines ``EncryptedTextField`` verschlüsselt setzen (``set_<feld>_encrypted``)."""
    getattr(obj, f"set_{field}_encrypted")(value)


def is_pdf_file(mime_type: str | None, *names: str | None) -> bool:
    """PDF-Erkennung wie im RIS-Bereich: MIME-Typ, ersatzweise Dateiendung."""
    if (mime_type or "").split(";")[0].strip().lower() == "application/pdf":
        return True
    return any(str(n or "").lower().endswith(".pdf") for n in names)


# ---------------------------------------------------------------------------
# Diskussions-Thread
# ---------------------------------------------------------------------------


def serialize_paper_comment_as_note(comment: PaperComment, membership: Membership) -> dict[str, Any]:
    """
    PaperComment im Format des TOP-Diskussions-Threads serialisieren.

    ARCHITEKTUR: PaperComment ist DER Thread für TOPs mit Vorlage.
    is_recommendation und is_decision heißen im UI (Etappe 2) einheitlich
    "Position der Fraktion" — beide Flags bleiben im Backend erhalten.
    """
    return {
        "id": str(comment.id),
        "content": decrypted(comment, "content"),
        "is_decision": comment.is_recommendation,
        "is_recommendation": comment.is_recommendation,
        "is_pinned": False,
        "author": comment.author.user.get_display_name(),
        "organization": comment.organization.name,
        "is_own": comment.author == membership,
        "is_own_org": comment.organization_id == membership.organization_id,
        "created_at": comment.created_at.isoformat(),
        "visibility": comment.visibility,
        "visibility_display": comment.get_visibility_display(),
        "origin": None,
        "source": "paper_comment",
    }


def serialize_paper_comment(comment: PaperComment, membership: Membership) -> dict[str, Any]:
    """PaperComment für die Vorlagen-Kommentar-API (gremienübergreifend)."""
    return {
        "id": str(comment.id),
        "content": decrypted(comment, "content"),
        "visibility": comment.visibility,
        "visibility_display": comment.get_visibility_display(),
        "is_recommendation": comment.is_recommendation,
        "author": comment.author.user.get_display_name(),
        "organization": comment.organization.name,
        "is_own": comment.author == membership,
        "is_own_org": comment.organization == membership.organization,
        "created_at": comment.created_at.isoformat(),
    }


def serialize_agenda_note(
    note: AgendaItemNote, membership: Membership, origin_meeting: OParlMeeting | None = None
) -> dict[str, Any]:
    """AgendaItemNote für den Diskussions-Thread serialisieren."""
    origin = None
    if origin_meeting is not None:
        label = str(cast(Any, origin_meeting).get_display_name())
        if origin_meeting.start:
            label = f"{label}, {timezone.localtime(origin_meeting.start).strftime('%d.%m.%Y')}"
        origin = {"meeting_id": str(origin_meeting.id), "label": label}
    return {
        "id": str(note.id),
        "content": decrypted(note, "content"),
        "is_decision": note.is_decision,
        "is_recommendation": note.is_decision,
        "is_pinned": note.is_pinned,
        "author": note.author.user.get_display_name(),
        "is_own": note.author == membership,
        "created_at": note.created_at.isoformat(),
        "visibility": note.visibility,
        "visibility_display": note.get_visibility_display(),
        "origin": origin,
        "source": "agenda_note",
    }


# ---------------------------------------------------------------------------
# Position, Redebeitrag, Anmerkungen
# ---------------------------------------------------------------------------


def serialize_position(position: AgendaItemPosition | None) -> dict[str, Any]:
    """Org-weite Position eines TOPs (leere Standardwerte, wenn noch keine existiert)."""
    if position is None:
        return {
            "position": "open",
            "position_display": "Noch offen",
            "is_final": False,
            "reasoning": "",
            "outcome": "",
            "outcome_display": "",
        }
    return {
        "position": position.position,
        "position_display": position.get_position_display(),
        "is_final": position.is_final,
        "reasoning": decrypted(position, "reasoning"),
        "outcome": position.outcome,
        "outcome_display": position.get_outcome_display() if position.outcome else "",
    }


def serialize_speech_note(note: AgendaSpeechNote | None, membership: Membership) -> dict[str, Any]:
    """Redebeitrag inkl. aufgelöstem linked_document-Inhalt (read-only, can_access-geprüft)."""
    if note is None:
        return {
            "content": "",
            "title": "",
            "estimated_duration": 0,
            "is_shared": False,
            "linked_document": None,
            "content_readonly": False,
        }
    linked: dict[str, Any] | None = None
    content = decrypted(note, "content")
    readonly = False
    doc = note.linked_document
    if doc is not None:
        if doc.can_access(membership):
            linked = {"id": str(doc.id), "title": doc.title}
            content = decrypted(doc, "content")
            readonly = True
        else:
            # Verknüpfung existiert, aber kein Zugriff (z.B. entzogene Freigabe)
            linked = {"id": str(doc.id), "title": None, "access": False}
            content = ""
            readonly = True
    return {
        "content": content,
        "title": note.title,
        "estimated_duration": note.estimated_duration,
        "is_shared": note.is_shared,
        "linked_document": linked,
        "content_readonly": readonly,
    }


def serialize_shared_speech(note: AgendaSpeechNote) -> dict[str, Any]:
    """Geteilter Redebeitrag eines anderen Mitglieds (Autor + Inhalt)."""
    return {"author": note.author.user.get_display_name(), "content": decrypted(note, "content")}


def serialize_file_annotation(annotation: FileAnnotation, membership: Membership) -> dict[str, Any]:
    """FileAnnotation für die Kommentarspur der PDF-Vorschau serialisieren."""
    return {
        "id": str(annotation.id),
        "page": annotation.page,
        "content": decrypted(annotation, "content"),
        "author": annotation.author.user.get_display_name(),
        "is_own": annotation.author_id == membership.id,
        "created_at": annotation.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Ergänzende Dokumente
# ---------------------------------------------------------------------------


def preview_info(doc: AgendaSupplementaryDocument) -> dict[str, Any]:
    """
    Vorschau-Informationen einer Anlage (Muster RIS-Inline-Vorschau).

    PDF-Uploads werden über ihre Media-URL im iframe angezeigt (Anmerkungs-Anker
    "doc"), OParl-Referenzen über den file_proxy (Anmerkungs-Anker "oparl").
    """
    if doc.document_type == "file" and doc.file and is_pdf_file(doc.mime_type, doc.filename, doc.title):
        return {
            "is_pdf": True,
            "preview_kind": "doc",
            "preview_id": str(doc.id),
            "preview_url": doc.display_url,
        }
    if doc.document_type == "oparl" and doc.oparl_file_id:
        f = doc.oparl_file
        if f is not None and is_pdf_file(f.mime_type, f.file_name, f.name):
            return {
                "is_pdf": True,
                "preview_kind": "oparl",
                "preview_id": str(f.id),
                "preview_url": reverse("insight_core:insight:file_proxy", args=[f.id]),
            }
    return {"is_pdf": False, "preview_kind": None, "preview_id": None, "preview_url": ""}


def serialize_new_document(doc: AgendaSupplementaryDocument) -> dict[str, Any]:
    """Antwort nach dem Anlegen einer Anlage (Link oder Upload)."""
    return {
        "id": str(doc.id),
        "title": doc.title,
        "url": doc.display_url,
        "document_type": doc.document_type,
        "paper_id": str(doc.paper_id) if doc.paper_id else None,
        "share_across_committees": doc.share_across_committees,
        "annotations": 0,
        **preview_info(doc),
    }


def serialize_document(doc: AgendaSupplementaryDocument, agenda_item_id: Any, annotations: int) -> dict[str, Any]:
    """Anlage in der Dokumentenliste eines TOPs (inkl. Anmerkungs-Zähler und Herkunft)."""
    return {
        "id": str(doc.id),
        "title": doc.title,
        "url": doc.display_url,
        "document_type": doc.document_type,
        "description": doc.description,
        "added_by": doc.added_by.user.get_display_name(),
        "created_at": doc.created_at.isoformat(),
        "paper_id": str(doc.paper_id) if doc.paper_id else None,
        "share_across_committees": doc.share_across_committees,
        "is_from_other_item": doc.agenda_item_id != agenda_item_id,
        "annotations": annotations,
        **preview_info(doc),
    }


# ---------------------------------------------------------------------------
# Sitzungsliste / Kalender
# ---------------------------------------------------------------------------


def serialize_calendar_event(meeting: OParlMeeting, org_slug: str) -> dict[str, Any]:
    """Sitzung als FullCalendar-Ereignis."""
    org_name = ""
    try:
        orgs = meeting.organizations.all()
        if orgs:
            org_name = orgs[0].name or ""
    except Exception:  # noqa: BLE001 — fehlerhafte Gremienzuordnung darf den Kalender nicht brechen
        pass
    return {
        "id": str(meeting.id),
        "title": meeting.name or org_name or "Sitzung",
        "start": meeting.start.isoformat() if meeting.start else None,
        "end": meeting.end.isoformat() if meeting.end else None,
        "url": f"/work/{org_slug}/meetings/{meeting.id}/",
        "extendedProps": {"committee": org_name, "cancelled": meeting.cancelled},
        "color": "#ef4444" if meeting.cancelled else None,
    }


# ---------------------------------------------------------------------------
# Vorbereitungsseite: Konfiguration der Alpine-Komponente ``preparationApp``
# ---------------------------------------------------------------------------


def _serialize_ris_file(f: OParlFile, annotation_counts: dict[Any, int]) -> dict[str, Any]:
    return {
        "id": str(f.id),
        "name": f.name or f.file_name or "Dokument",
        "url": f.access_url or f.download_url,
        "previewUrl": reverse("insight_core:insight:file_proxy", args=[f.id]),
        "mimeType": f.mime_type or "",
        "isPdf": is_pdf_file(f.mime_type, f.file_name, f.name),
        "size": f.size_human,
        "pageCount": f.page_count or 0,
        "annotations": annotation_counts.get(f.id, 0),
    }


def serialize_prepared_item(entry: PreparedItem, index: int, data: PreparationData) -> dict[str, Any]:
    """Ein TOP mit allen Sektionen für ``preparationApp`` (Feldnamen sind Vertrag mit dem Frontend)."""
    item = entry.item
    position = entry.position
    speech = entry.own_speech
    paper = entry.primary_paper
    return {
        "id": str(item.id),
        "number": item.number or str(index + 1),
        "name": item.name or "Ohne Titel",
        # Position (org-weit)
        "position": position.position if position else "open",
        "isFinal": position.is_final if position else False,
        "reasoning": decrypted(position, "reasoning") if position else "",
        "outcome": position.outcome if position else "",
        "setBy": position.set_by.user.get_display_name() if position and position.set_by else None,
        # Positionen derselben Org aus anderen Gremien zur selben Vorlage
        "crossPositions": data.cross_positions.get(item.id, []),
        # Private Notiz (pro User)
        "privateNote": decrypted(entry.private_note, "content") if entry.private_note else "",
        # Redebeitrag (pro User)
        "hasSpeechNote": bool(speech),
        "speechTitle": speech.title if speech else "",
        "speechContent": decrypted(speech, "content") if speech else "",
        "speechDuration": speech.estimated_duration if speech else 0,
        "speechShared": speech.is_shared if speech else False,
        "speechLinkedDocument": {"id": str(speech.linked_document.id), "title": speech.linked_document.title}
        if speech and speech.linked_document is not None
        else None,
        "sharedSpeeches": [serialize_shared_speech(s) for s in entry.shared_speeches],
        # Vorlage
        "paper": {
            "id": str(paper.id),
            "name": paper.name or "Ohne Titel",
            "reference": paper.reference or "",
            "paperType": paper.paper_type or "",
            "consultationCount": getattr(paper, "_prefetched_consultation_count", 0),
            "consultations": data.consultations_by_paper.get(paper.id, []),
        }
        if paper
        else None,
        "hasFiles": entry.has_files,
        "files": [
            _serialize_ris_file(f, data.file_annotation_counts)
            for p in entry.papers
            for f in p.files.all()
            if f.access_url or f.download_url
        ],
        # Dokumente (TOP-Anhänge + geteilte Vorlagen-Anhänge)
        "documents": [
            {
                "id": str(d.id),
                "title": d.title,
                "url": d.display_url,
                "type": d.document_type,
                "addedBy": d.added_by.user.get_display_name(),
                "paperId": str(d.paper_id) if d.paper_id else None,
                "sharedAcrossCommittees": d.share_across_committees,
            }
            for d in entry.documents
        ],
        # Alias für Altbestand im Template (documentLinks)
        "documentLinks": [
            {"id": str(d.id), "title": d.title, "url": d.display_url}
            for d in entry.documents
            if d.document_type == "link"
        ],
        "notesCount": len(entry.notes),
    }


def build_prepare_config(
    *,
    organization: Organization,
    meeting: OParlMeeting,
    preparation: MeetingPreparation,
    data: PreparationData,
    current_user_name: str,
) -> dict[str, Any]:
    """Ein JSON-Objekt für den Client (``json_script`` im Template) — keine String-Interpolation in JS."""
    from .models import AgendaItemPosition

    return {
        "orgSlug": organization.slug,
        "meetingId": str(meeting.id),
        "currentUser": current_user_name,
        "positionLabels": {code: str(label) for code, label in AgendaItemPosition.POSITION_CHOICES},
        "orgNotes": decrypted(preparation, "notes") or "",
        "items": [serialize_prepared_item(entry, idx, data) for idx, entry in enumerate(data.prepared_items)],
        "urls": {
            "summary": reverse(
                "work:meeting_summary", kwargs={"org_slug": organization.slug, "meeting_id": meeting.id}
            ),
        },
    }
