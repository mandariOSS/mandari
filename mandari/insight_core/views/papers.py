# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET
from django.views.generic import DetailView, ListView

from ..models import (
    OParlAgendaItem,
    OParlMeeting,
    OParlPaper,
)
from ._helpers import get_active_body

# =============================================================================
# Vorgänge (Papers)
# =============================================================================


class PaperListView(ListView):
    """Liste aller Vorgänge."""

    model = OParlPaper
    template_name = "pages/papers/list.html"
    context_object_name = "papers"
    paginate_by = 25

    def get_template_names(self):
        # Für HTMX-Requests nur das Partial zurückgeben
        if self.request.headers.get("HX-Request"):
            return ["partials/paper_list_items.html"]
        return [self.template_name]

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlPaper.objects.none()

        qs = OParlPaper.objects.filter(body=body)

        # Suche
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(reference__icontains=q))

        # Typ-Filter
        paper_type = self.request.GET.get("type", "").strip()
        if paper_type:
            qs = qs.filter(paper_type=paper_type)

        return qs.order_by("-date", "-oparl_created")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)

        if body:
            # Verfügbare Typen für Filter
            context["paper_types"] = (
                OParlPaper.objects.filter(body=body)
                .exclude(paper_type__isnull=True)
                .values_list("paper_type", flat=True)
                .distinct()
                .order_by("paper_type")
            )

        return context


class PaperDetailView(DetailView):
    """Detailseite eines Vorgangs."""

    model = OParlPaper
    template_name = "pages/papers/detail.html"
    context_object_name = "paper"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paper = self.object

        # Alle Dateien
        files = paper.files.all()
        context["files"] = files

        # Dateien mit extrahiertem Text für Rohtext-Tab
        context["files_with_text"] = [f for f in files if f.text_content and f.text_content.strip()]

        # Beratungsverlauf (Consultations mit Meeting-Info)
        consultations = self._get_consultations_with_meetings(paper)
        context["consultations"] = consultations

        # Kontext-Summary für Dokumente-Tab (nächste zukünftige Beratung, Fallback neueste)
        if consultations:
            now = timezone.now()
            with_meeting = [item for item in consultations if item.get("meeting") and item.get("date")]
            if with_meeting:
                future = [item for item in with_meeting if item["date"] >= now]
                if future:
                    # Nächste zukünftige (früheste)
                    best = min(future, key=lambda x: x["date"])
                else:
                    # Neueste vergangene
                    best = max(with_meeting, key=lambda x: x["date"])
                context["file_context_summary"] = best

        # SEO-Kontext
        from ..seo import get_paper_seo

        context["seo"] = get_paper_seo(paper, self.request).to_dict()

        return context

    def _get_consultations_with_meetings(self, paper):
        """
        Lädt Consultations mit aufgelösten Meeting- und AgendaItem-Referenzen.

        OParl-Struktur:
        - Paper enthält eingebettete Consultation-Objekte
        - Consultation referenziert Meeting und AgendaItem als URL-Strings
        - Wir lösen diese Referenzen auf, um den Beratungsverlauf anzuzeigen
        """
        consultations = paper.consultations.all()
        if not consultations:
            return []

        # Sammle alle meeting_external_ids und agenda_item_external_ids
        meeting_ids = [c.meeting_external_id for c in consultations if c.meeting_external_id]
        agenda_item_ids = [c.agenda_item_external_id for c in consultations if c.agenda_item_external_id]

        # Batch-Lookup für Meetings
        meetings_by_id = {}
        if meeting_ids:
            meetings = OParlMeeting.objects.filter(external_id__in=meeting_ids).prefetch_related("organizations")
            meetings_by_id = {m.external_id: m for m in meetings}

        # Batch-Lookup für AgendaItems
        agenda_items_by_id = {}
        if agenda_item_ids:
            agenda_items = OParlAgendaItem.objects.filter(external_id__in=agenda_item_ids)
            agenda_items_by_id = {a.external_id: a for a in agenda_items}

        # Baue angereicherte Consultation-Liste
        result = []
        for consultation in consultations:
            meeting = meetings_by_id.get(consultation.meeting_external_id)
            agenda_item = agenda_items_by_id.get(consultation.agenda_item_external_id)

            result.append(
                {
                    "consultation": consultation,
                    "meeting": meeting,
                    "agenda_item": agenda_item,
                    "date": meeting.start if meeting else None,
                    "organization_name": meeting.get_display_name() if meeting else None,
                    "agenda_number": agenda_item.number if agenda_item else None,
                    "result": agenda_item.result if agenda_item else None,
                    "public": agenda_item.public if agenda_item else True,
                    "role": consultation.role,
                    "authoritative": consultation.authoritative,
                }
            )

        # Sortiere nach Datum (älteste zuerst = chronologischer Verlauf)
        result.sort(key=lambda x: x["date"] or timezone.now(), reverse=False)

        return result


class PaperListPartial(ListView):
    """HTMX Partial für Vorgänge-Liste."""

    model = OParlPaper
    template_name = "partials/paper_list_items.html"
    context_object_name = "papers"
    paginate_by = 20


@require_GET
def paper_summary(request, pk):
    """
    HTMX Endpoint für KI-Zusammenfassung eines Vorgangs.

    Nutzt gecachte Zusammenfassung oder generiert neue via Nebius AI.
    """
    paper = get_object_or_404(OParlPaper, pk=pk)

    # Return cached summary if available
    if paper.summary:
        return render(
            request,
            "partials/paper_summary.html",
            {
                "paper": paper,
                "summary": paper.summary,
            },
        )

    # Generate new summary
    try:
        from insight_ai.services.summarizer import (
            APINotConfiguredError,
            NoTextContentError,
            SummaryError,
            SummaryService,
        )

        service = SummaryService()
        summary = service.generate_summary(paper)

        return render(
            request,
            "partials/paper_summary.html",
            {
                "paper": paper,
                "summary": summary,
            },
        )

    except NoTextContentError as e:
        return render(
            request,
            "partials/paper_summary.html",
            {
                "paper": paper,
                "error": str(e),
            },
        )

    except APINotConfiguredError as e:
        return render(
            request,
            "partials/paper_summary.html",
            {
                "paper": paper,
                "error": str(e),
            },
        )

    except SummaryError as e:
        return render(
            request,
            "partials/paper_summary.html",
            {
                "paper": paper,
                "error": str(e),
            },
        )

    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.exception(f"Unexpected error in paper_summary: {e}")
        return render(
            request,
            "partials/paper_summary.html",
            {
                "paper": paper,
                "error": f"Unerwarteter Fehler: {str(e)}",
            },
        )
