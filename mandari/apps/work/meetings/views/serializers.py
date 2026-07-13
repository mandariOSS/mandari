# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meeting preparation views for the Work module.

Org-weite Sitzungsvorbereitung mit 5 Sektionen pro TOP:
1. Position/Beschluss (org-weit)
2. Private Notizen (pro User)
3. Redebeitrag (pro User, teilbar)
4. Fraktionsdiskussion (org-weit)
5. Dokumente (org-weit)
"""

from django.utils import timezone


def serialize_paper_comment_as_note(comment, membership):
    """
    PaperComment im Format des TOP-Diskussions-Threads serialisieren.

    ARCHITEKTUR: PaperComment ist DER Thread für TOPs mit Vorlage.
    is_recommendation und is_decision heißen im UI (Etappe 2) einheitlich
    "Position der Fraktion" — beide Flags bleiben im Backend erhalten.
    """
    return {
        "id": str(comment.id),
        "content": comment.get_content_decrypted(),
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


def serialize_agenda_note(note, membership, origin_meeting=None):
    """AgendaItemNote für den Diskussions-Thread serialisieren."""
    origin = None
    if origin_meeting is not None:
        label = origin_meeting.get_display_name()
        if origin_meeting.start:
            label = f"{label}, {timezone.localtime(origin_meeting.start).strftime('%d.%m.%Y')}"
        origin = {"meeting_id": str(origin_meeting.id), "label": label}
    return {
        "id": str(note.id),
        "content": note.get_content_decrypted(),
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


def serialize_file_annotation(annotation, membership):
    """FileAnnotation für die Kommentarspur der PDF-Vorschau serialisieren."""
    return {
        "id": str(annotation.id),
        "page": annotation.page,
        "content": annotation.get_content_decrypted(),
        "author": annotation.author.user.get_display_name(),
        "is_own": annotation.author_id == membership.id,
        "created_at": annotation.created_at.isoformat(),
    }
