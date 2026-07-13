# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meeting preparation views for the Work module.

Thematisch aufgeteiltes Paket; alle Namen werden hier re-exportiert,
damit bestehende Imports (``from apps.work.meetings import views``)
unverändert funktionieren.
"""

from ._helpers import (
    get_primary_paper_for_item,
    is_pdf_file,
    natural_sort_key,
    prefetch_papers_for_agenda_items,
)
from .api_agenda import (
    AgendaNotesAPIView,
    AgendaPositionAPIView,
    PrivateNoteAPIView,
    SpeechLinkableDocumentsAPIView,
    SpeechNoteAPIView,
)
from .api_documents import (
    FileAnnotationAPIView,
    SupplementaryDocumentAPIView,
)
from .list import (
    MeetingCalendarEventsView,
    MeetingCalendarView,
    MeetingDetailView,
    MeetingListView,
)
from .paper_comments import (
    PaperCommentAPIView,
)
from .prepare import (
    MeetingPrepareView,
)
from .serializers import (
    serialize_agenda_note,
    serialize_file_annotation,
    serialize_paper_comment_as_note,
)
from .summary import (
    PreparationSummaryView,
)
from .teleprompter import (
    TeleprompterView,
)

__all__ = [
    "AgendaNotesAPIView",
    "AgendaPositionAPIView",
    "FileAnnotationAPIView",
    "MeetingCalendarEventsView",
    "MeetingCalendarView",
    "MeetingDetailView",
    "MeetingListView",
    "MeetingPrepareView",
    "PaperCommentAPIView",
    "PreparationSummaryView",
    "PrivateNoteAPIView",
    "SpeechLinkableDocumentsAPIView",
    "SpeechNoteAPIView",
    "SupplementaryDocumentAPIView",
    "TeleprompterView",
    "get_primary_paper_for_item",
    "is_pdf_file",
    "natural_sort_key",
    "prefetch_papers_for_agenda_items",
    "serialize_agenda_note",
    "serialize_file_annotation",
    "serialize_paper_comment_as_note",
]
