# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin


class RISOverviewView(WorkViewMixin, TemplateView):
    """RIS overview page with statistics."""

    template_name = "work/ris/overview.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_overview"

        # Get the linked OParl body
        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        context["body"] = body

        # Import here to avoid circular imports
        from insight_core.models import (
            OParlMeeting,
            OParlOrganization,
            OParlPaper,
            OParlPerson,
        )

        # Statistics
        today = timezone.now().date()

        # Papers (Vorgänge)
        papers_total = OParlPaper.objects.filter(body=body).count()
        papers_this_year = OParlPaper.objects.filter(body=body, date__year=today.year).count()

        # Meetings (Sitzungen)
        meetings_total = OParlMeeting.objects.filter(body=body).count()
        meetings_upcoming = OParlMeeting.objects.filter(body=body, start__gt=timezone.now(), cancelled=False).count()

        # Organizations (Gremien)
        organizations_total = OParlOrganization.objects.filter(body=body).count()
        organizations_active = (
            OParlOrganization.objects.filter(body=body)
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .count()
        )

        # Persons (Personen)
        persons_total = OParlPerson.objects.filter(body=body).count()

        context["stats"] = {
            "papers_total": papers_total,
            "papers_this_year": papers_this_year,
            "meetings_total": meetings_total,
            "meetings_upcoming": meetings_upcoming,
            "organizations_total": organizations_total,
            "organizations_active": organizations_active,
            "persons_total": persons_total,
        }

        # Recent papers
        context["recent_papers"] = OParlPaper.objects.filter(body=body).order_by("-date", "-oparl_created")[:5]

        # Upcoming meetings
        context["upcoming_meetings"] = (
            OParlMeeting.objects.filter(body=body, start__gt=timezone.now(), cancelled=False)
            .prefetch_related("organizations")
            .order_by("start")[:5]
        )

        return context


class RISPapersView(WorkViewMixin, TemplateView):
    """RIS papers list with search and filtering."""

    template_name = "work/ris/papers.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_papers"

        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        context["body"] = body

        from insight_core.models import OParlPaper

        # Base queryset
        papers = OParlPaper.objects.filter(body=body)

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
            OParlPaper.objects.filter(body=body).values_list("paper_type", flat=True).distinct().order_by("paper_type")
        )

        context["years"] = OParlPaper.objects.filter(body=body, date__isnull=False).dates("date", "year", order="DESC")

        # Order and paginate
        papers = papers.order_by("-date", "-oparl_created")

        paginator = Paginator(papers, 25)
        page = self.request.GET.get("page", 1)
        context["papers"] = paginator.get_page(page)
        context["paginator"] = paginator

        return context


class RISPaperDetailView(WorkViewMixin, TemplateView):
    """RIS paper detail view."""

    template_name = "work/ris/paper_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_papers"

        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        from insight_core.models import OParlPaper

        paper = get_object_or_404(OParlPaper, id=kwargs.get("paper_id"), body=body)

        context["paper"] = paper
        context["body"] = body

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


class RISMeetingsView(WorkViewMixin, TemplateView):
    """RIS meetings list."""

    template_name = "work/ris/meetings.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_meetings"

        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        context["body"] = body

        from insight_core.models import OParlMeeting, OParlOrganization

        # Base queryset
        meetings = OParlMeeting.objects.filter(body=body).prefetch_related("organizations")

        # Filter: upcoming/past
        view_mode = self.request.GET.get("view", "upcoming")
        now = timezone.now()

        if view_mode == "upcoming":
            meetings = meetings.filter(start__gt=now, cancelled=False)
            meetings = meetings.order_by("start")
        elif view_mode == "past":
            meetings = meetings.filter(start__lte=now)
            meetings = meetings.order_by("-start")
        else:
            meetings = meetings.order_by("-start")

        context["view_mode"] = view_mode

        # Filter by organization
        org_id = self.request.GET.get("org")
        if org_id:
            meetings = meetings.filter(organizations__id=org_id)
            context["selected_org"] = org_id

        # Get available organizations for filter
        context["organizations"] = OParlOrganization.objects.filter(body=body).order_by("name")

        # Filter by year
        year = self.request.GET.get("year")
        if year:
            try:
                meetings = meetings.filter(start__year=int(year))
                context["selected_year"] = int(year)
            except ValueError:
                pass

        context["years"] = OParlMeeting.objects.filter(body=body, start__isnull=False).dates(
            "start", "year", order="DESC"
        )

        # Pagination
        paginator = Paginator(meetings, 25)
        page = self.request.GET.get("page", 1)
        context["meetings"] = paginator.get_page(page)
        context["paginator"] = paginator

        return context


class RISMeetingDetailView(WorkViewMixin, TemplateView):
    """RIS meeting detail view."""

    template_name = "work/ris/meeting_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_meetings"

        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        from insight_core.models import OParlMeeting

        meeting = get_object_or_404(OParlMeeting, id=kwargs.get("meeting_id"), body=body)

        context["meeting"] = meeting
        context["body"] = body

        # Get agenda items with related papers
        # Natural sort: 1, 2, 10 instead of 1, 10, 2
        import re
        agenda_items = sorted(
            meeting.agenda_items.all(),
            key=lambda x: [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in re.split(r"(\d+)", x.number or "999") if p]
        )

        # Enrich with papers
        items_with_papers = []
        for item in agenda_items:
            papers = item.get_papers()
            items_with_papers.append(
                {
                    "item": item,
                    "papers": papers,
                }
            )

        context["agenda_items"] = items_with_papers
        context["organizations"] = meeting.organizations.all()

        return context


class RISOrganizationsView(WorkViewMixin, TemplateView):
    """RIS organizations list."""

    template_name = "work/ris/organizations.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_organizations"

        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        context["body"] = body

        from insight_core.models import OParlMeeting, OParlOrganization
        from insight_core.ranking import sort_organizations_by_ranking

        now = timezone.now()
        today = now.date()
        tab = self.request.GET.get("tab", "active")
        q = self.request.GET.get("q", "").strip()

        # Meetings become "past" at midnight, not at meeting start time
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Subqueries for next/last meeting dates
        next_meeting_sq = Subquery(
            OParlMeeting.objects.filter(
                organizations=OuterRef("pk"),
                start__gte=today_start,
                cancelled=False,
            )
            .order_by("start")
            .values("start")[:1]
        )
        last_meeting_sq = Subquery(
            OParlMeeting.objects.filter(
                organizations=OuterRef("pk"),
                start__lt=today_start,
            )
            .order_by("-start")
            .values("start")[:1]
        )
        has_any_meeting = Exists(OParlMeeting.objects.filter(organizations=OuterRef("pk")))

        # Base queryset with meeting annotations
        organizations = OParlOrganization.objects.filter(body=body).annotate(
            next_meeting=next_meeting_sq,
            last_meeting=last_meeting_sq,
            has_meetings=has_any_meeting,
        )

        # Search
        if q:
            organizations = organizations.filter(Q(name__icontains=q) | Q(short_name__icontains=q))
            context["search_query"] = q

        # Tab filter: active vs all
        if tab == "active":
            organizations = organizations.filter(
                Q(end_date__isnull=True) | Q(end_date__gte=today),
                has_meetings=True,
            )
        context["tab"] = tab

        # Tab counts (without search filter)
        all_orgs = OParlOrganization.objects.filter(body=body).annotate(
            has_meetings=has_any_meeting,
        )
        context["active_count"] = all_orgs.filter(
            Q(end_date__isnull=True) | Q(end_date__gte=today),
            has_meetings=True,
        ).count()
        context["all_count"] = all_orgs.count()

        # Apply ranking (with activity check)
        organizations = sort_organizations_by_ranking(organizations, include_activity=True)

        # Pagination
        paginator = Paginator(organizations, 25)
        page = self.request.GET.get("page", 1)
        context["organizations"] = paginator.get_page(page)
        context["paginator"] = paginator

        return context


class RISOrganizationDetailView(WorkViewMixin, TemplateView):
    """RIS organization detail view."""

    template_name = "work/ris/organization_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_organizations"

        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        from insight_core.models import OParlOrganization

        org = get_object_or_404(OParlOrganization, id=kwargs.get("org_id"), body=body)

        context["org"] = org
        context["body"] = body

        today = timezone.now().date()
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

        # Active members (no end_date or end_date >= today)
        all_memberships = org.memberships.select_related("person")
        context["active_members"] = (
            all_memberships
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .order_by("person__family_name", "person__name")
        )
        # Past members (end_date < today)
        context["past_members"] = (
            all_memberships
            .filter(end_date__lt=today)
            .order_by("-end_date")
        )

        # Upcoming meetings (today + future)
        context["upcoming_meetings"] = (
            org.meetings
            .filter(start__gte=today_start, cancelled=False)
            .order_by("start")
        )
        # Past meetings
        context["past_meetings"] = (
            org.meetings
            .filter(start__lt=today_start)
            .order_by("-start")[:30]
        )

        # Active tab from query parameter
        context["active_tab"] = self.request.GET.get("tab", "members")

        return context


class RISPersonsView(WorkViewMixin, TemplateView):
    """RIS persons list."""

    template_name = "work/ris/persons.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_persons"

        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        context["body"] = body

        from insight_core.models import OParlMembership, OParlOrganization, OParlPerson

        # Base queryset
        persons = OParlPerson.objects.filter(body=body)

        # Council role annotation (like Insight portal)
        today = timezone.now().date()
        rat = OParlOrganization.objects.filter(body=body, name="Rat").first()
        if rat:
            council_role_sq = Subquery(
                OParlMembership.objects.filter(
                    person=OuterRef("pk"),
                    organization=rat,
                )
                .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
                .values("role")[:1]
            )
            persons = persons.annotate(council_role=council_role_sq)

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            persons = persons.filter(
                Q(name__icontains=search)
                | Q(family_name__icontains=search)
                | Q(given_name__icontains=search)
                | Q(email__icontains=search)
            )
            context["search_query"] = search

        # Order
        persons = persons.annotate(membership_count=Count("memberships")).order_by("family_name", "given_name")

        # Pagination
        paginator = Paginator(persons, 50)
        page = self.request.GET.get("page", 1)
        context["persons"] = paginator.get_page(page)
        context["paginator"] = paginator

        return context


class RISPersonDetailView(WorkViewMixin, TemplateView):
    """RIS person detail view."""

    template_name = "work/ris/person_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_persons"

        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        from insight_core.models import OParlPerson

        person = get_object_or_404(OParlPerson, id=kwargs.get("person_id"), body=body)

        context["person"] = person
        context["body"] = body

        today = timezone.now().date()

        # Split memberships into active and past
        all_memberships = person.memberships.select_related("organization")
        context["active_memberships"] = (
            all_memberships
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .order_by("organization__name")
        )
        context["past_memberships"] = (
            all_memberships
            .filter(end_date__lt=today)
            .order_by("-end_date")
        )

        # Active tab
        context["active_tab"] = self.request.GET.get("tab", "memberships")

        return context


class RISMapView(WorkViewMixin, TemplateView):
    """RIS map view showing geolocalized papers."""

    template_name = "work/ris/map.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_overview"

        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        context["body"] = body

        # Map center and bounds
        context["map_config"] = {
            "center_lat": float(body.latitude) if body.latitude else 51.5,
            "center_lng": float(body.longitude) if body.longitude else 7.5,
            "zoom": 13,
            "bbox": {
                "north": float(body.bbox_north) if body.bbox_north else None,
                "south": float(body.bbox_south) if body.bbox_south else None,
                "east": float(body.bbox_east) if body.bbox_east else None,
                "west": float(body.bbox_west) if body.bbox_west else None,
            }
            if body.bbox_north
            else None,
        }

        return context


class RISMapDataView(WorkViewMixin, View):
    """API endpoint for map data (GeoJSON)."""

    permission_required = "ris.view"

    def get(self, request, *args, **kwargs):
        body = self.organization.body
        if not body:
            return JsonResponse({"type": "FeatureCollection", "features": []})

        from insight_core.models import OParlPaper

        # Get papers with locations
        papers = OParlPaper.objects.filter(body=body, locations__isnull=False).exclude(locations=[])[:500]

        features = []
        for paper in papers:
            if not paper.locations:
                continue

            for location in paper.locations:
                if not isinstance(location, dict):
                    continue

                lat = location.get("lat") or location.get("latitude")
                lng = location.get("lng") or location.get("lon") or location.get("longitude")

                if lat and lng:
                    features.append(
                        {
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [float(lng), float(lat)]},
                            "properties": {
                                "id": str(paper.id),
                                "title": paper.name or "Vorgang",
                                "reference": paper.reference,
                                "date": paper.date.isoformat() if paper.date else None,
                                "location_name": location.get("name", ""),
                            },
                        }
                    )

        return JsonResponse({"type": "FeatureCollection", "features": features})


class RISFilesView(WorkViewMixin, TemplateView):
    """RIS files/documents list."""

    template_name = "work/ris/files.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_files"

        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        context["body"] = body

        from insight_core.models import OParlFile
        from insight_core.views import _annotate_files_with_context

        # Base queryset
        files = OParlFile.objects.filter(body=body).select_related("paper").order_by("-file_date", "-created_at")

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            files = files.filter(
                Q(name__icontains=search) | Q(file_name__icontains=search) | Q(paper__name__icontains=search)
            )
            context["search_query"] = search

        # Pagination
        paginator = Paginator(files, 30)
        page = self.request.GET.get("page", 1)
        page_obj = paginator.get_page(page)

        # Annotate with context (organization, meeting, agenda item)
        _annotate_files_with_context(page_obj.object_list)

        context["files"] = page_obj
        context["paginator"] = paginator
        context["total_count"] = paginator.count

        return context


class RISSearchView(WorkViewMixin, TemplateView):
    """RIS search across all entities."""

    template_name = "work/ris/search.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_search"

        body = self.organization.body
        if not body:
            context["no_body_linked"] = True
            return context

        context["body"] = body

        query = self.request.GET.get("q", "").strip()
        if not query:
            return context

        context["search_query"] = query

        from insight_core.models import (
            OParlMeeting,
            OParlOrganization,
            OParlPaper,
            OParlPerson,
        )

        # Search papers
        papers = (
            OParlPaper.objects.filter(body=body)
            .filter(Q(name__icontains=query) | Q(reference__icontains=query))
            .order_by("-date")[:10]
        )

        # Search meetings
        meetings = (
            OParlMeeting.objects.filter(body=body)
            .filter(Q(name__icontains=query) | Q(location_name__icontains=query))
            .prefetch_related("organizations")
            .order_by("-start")[:10]
        )

        # Search organizations
        organizations = (
            OParlOrganization.objects.filter(body=body)
            .filter(Q(name__icontains=query) | Q(short_name__icontains=query))
            .order_by("name")[:10]
        )

        # Search persons
        persons = (
            OParlPerson.objects.filter(body=body)
            .filter(Q(name__icontains=query) | Q(family_name__icontains=query) | Q(given_name__icontains=query))
            .order_by("family_name")[:10]
        )

        context["results"] = {
            "papers": papers,
            "meetings": meetings,
            "organizations": organizations,
            "persons": persons,
        }

        context["total_results"] = papers.count() + meetings.count() + organizations.count() + persons.count()

        return context
