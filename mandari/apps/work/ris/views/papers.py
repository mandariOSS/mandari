# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from ._mixins import RISBodiesMixin


class RISPapersView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS papers list with search and filtering."""

    template_name = "work/ris/papers.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_papers"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        from insight_core.models import OParlPaper

        # Base queryset
        papers = OParlPaper.objects.filter(body__in=bodies)

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            papers = papers.filter(Q(name__icontains=search) | Q(reference__icontains=search))
            context["search_query"] = search

        # Filter by paper type
        paper_type = self.request.GET.get("type")
        if paper_type:
            papers = papers.filter(paper_type=paper_type)
            context["selected_type"] = paper_type

        # Filter by year
        year = self.request.GET.get("year")
        if year:
            try:
                papers = papers.filter(date__year=int(year))
                context["selected_year"] = int(year)
            except ValueError:
                pass

        # Get available filters
        context["paper_types"] = (
            OParlPaper.objects.filter(body__in=bodies)
            .values_list("paper_type", flat=True)
            .distinct()
            .order_by("paper_type")
        )

        context["years"] = OParlPaper.objects.filter(body__in=bodies, date__isnull=False).dates(
            "date", "year", order="DESC"
        )

        # Order and paginate
        papers = papers.order_by("-date", "-oparl_created")

        paginator = Paginator(papers, 25)
        page = self.request.GET.get("page", 1)
        context["papers"] = paginator.get_page(page)
        context["paginator"] = paginator

        return context


class RISPaperDetailView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS paper detail view."""

    template_name = "work/ris/paper_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_papers"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        from insight_core.models import OParlPaper

        paper = get_object_or_404(OParlPaper, id=kwargs.get("paper_id"), body__in=bodies)

        context["paper"] = paper

        # Get files - first try database relationship
        db_files = paper.files.all()

        if db_files.exists():
            context["files"] = db_files
            context["files_from_raw_json"] = False
        else:
            # Fallback: Extract files from raw_json if database relationship is empty
            raw_files = []
            raw_json = paper.raw_json or {}

            # Main file
            main_file = raw_json.get("mainFile")
            if main_file and isinstance(main_file, dict):
                raw_files.append(
                    {
                        "name": main_file.get("name", main_file.get("fileName", "Hauptdokument")),
                        "file_name": main_file.get("fileName", ""),
                        "mime_type": main_file.get("mimeType", ""),
                        "access_url": main_file.get("accessUrl", ""),
                        "download_url": main_file.get("downloadUrl", ""),
                        "is_main": True,
                    }
                )

            # Auxiliary files
            aux_files = raw_json.get("auxiliaryFile", [])
            if isinstance(aux_files, list):
                for af in aux_files:
                    if isinstance(af, dict):
                        raw_files.append(
                            {
                                "name": af.get("name", af.get("fileName", "Dokument")),
                                "file_name": af.get("fileName", ""),
                                "mime_type": af.get("mimeType", ""),
                                "access_url": af.get("accessUrl", ""),
                                "download_url": af.get("downloadUrl", ""),
                                "is_main": False,
                            }
                        )

            context["files"] = raw_files
            context["files_from_raw_json"] = True

        # Get consultations enriched with meeting/agenda item data
        context["consultations"] = self._get_enriched_consultations(paper)

        return context

    def _get_enriched_consultations(self, paper):
        """
        Load consultations with resolved meeting and agenda item references.

        OParl structure:
        - Paper contains consultation objects
        - Consultation references Meeting and AgendaItem via external_id strings
        - We resolve these to show the full consultation history
        """
        from insight_core.models import OParlAgendaItem, OParlMeeting

        consultations = paper.consultations.all()
        if not consultations:
            return []

        # Collect all meeting and agenda item external IDs
        meeting_ids = [c.meeting_external_id for c in consultations if c.meeting_external_id]
        agenda_item_ids = [c.agenda_item_external_id for c in consultations if c.agenda_item_external_id]

        # Batch lookup for meetings
        meetings_by_id = {}
        if meeting_ids:
            meetings = OParlMeeting.objects.filter(external_id__in=meeting_ids).prefetch_related("organizations")
            meetings_by_id = {m.external_id: m for m in meetings}

        # Batch lookup for agenda items
        agenda_items_by_id = {}
        if agenda_item_ids:
            agenda_items = OParlAgendaItem.objects.filter(external_id__in=agenda_item_ids)
            agenda_items_by_id = {a.external_id: a for a in agenda_items}

        # Build enriched consultation list
        result = []
        for consultation in consultations:
            meeting = meetings_by_id.get(consultation.meeting_external_id)
            agenda_item = agenda_items_by_id.get(consultation.agenda_item_external_id)

            # Get organization name from meeting's organizations
            org_name = None
            if meeting:
                orgs = meeting.organizations.all()
                if orgs:
                    org_name = orgs[0].name or orgs[0].short_name

            result.append(
                {
                    "consultation": consultation,
                    "meeting": meeting,
                    "agenda_item": agenda_item,
                    "date": meeting.start if meeting else None,
                    "organization_name": org_name,
                    "meeting_name": meeting.name if meeting else None,
                    "agenda_number": agenda_item.number if agenda_item else None,
                    "result": getattr(agenda_item, "result", None) if agenda_item else None,
                    "public": getattr(agenda_item, "public", True) if agenda_item else True,
                    "role": consultation.role,
                    "authoritative": consultation.authoritative,
                }
            )

        # Sort by date (oldest first = chronological order)
        result.sort(key=lambda x: x["date"] or timezone.now(), reverse=False)

        return result
