# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.core.paginator import Paginator
from django.db.models import Count, Q
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
        papers_this_year = OParlPaper.objects.filter(
            body=body,
            date__year=today.year
        ).count()

        # Meetings (Sitzungen)
        meetings_total = OParlMeeting.objects.filter(body=body).count()
        meetings_upcoming = OParlMeeting.objects.filter(
            body=body,
            start__gt=timezone.now(),
            cancelled=False
        ).count()

        # Organizations (Gremien)
        organizations_total = OParlOrganization.objects.filter(body=body).count()
        organizations_active = OParlOrganization.objects.filter(
            body=body
        ).filter(
            Q(end_date__isnull=True) | Q(end_date__gte=today)
        ).count()

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
        context["recent_papers"] = OParlPaper.objects.filter(
            body=body
        ).order_by("-date", "-oparl_created")[:5]

        # Upcoming meetings
        context["upcoming_meetings"] = OParlMeeting.objects.filter(
            body=body,
            start__gt=timezone.now(),
            cancelled=False
        ).prefetch_related("organizations").order_by("start")[:5]

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
            papers = papers.filter(
                Q(name__icontains=search) |
                Q(reference__icontains=search)
            )
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
        context["paper_types"] = OParlPaper.objects.filter(
            body=body
        ).values_list("paper_type", flat=True).distinct().order_by("paper_type")

        context["years"] = OParlPaper.objects.filter(
            body=body,
            date__isnull=False
        ).dates("date", "year", order="DESC")

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

        paper = get_object_or_404(
            OParlPaper,
            id=kwargs.get("paper_id"),
            body=body
        )

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
                raw_files.append({
                    "name": main_file.get("name", main_file.get("fileName", "Hauptdokument")),
                    "file_name": main_file.get("fileName", ""),
                    "mime_type": main_file.get("mimeType", ""),
                    "access_url": main_file.get("accessUrl", ""),
                    "download_url": main_file.get("downloadUrl", ""),
                    "is_main": True,
                })

            # Auxiliary files
            aux_files = raw_json.get("auxiliaryFile", [])
            if isinstance(aux_files, list):
                for af in aux_files:
                    if isinstance(af, dict):
                        raw_files.append({
                            "name": af.get("name", af.get("fileName", "Dokument")),
                            "file_name": af.get("fileName", ""),
                            "mime_type": af.get("mimeType", ""),
                            "access_url": af.get("accessUrl", ""),
                            "download_url": af.get("downloadUrl", ""),
                            "is_main": False,
                        })

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
        from insight_core.models import OParlMeeting, OParlAgendaItem

        consultations = paper.consultations.all()
        if not consultations:
            return []

        # Collect all meeting and agenda item external IDs
        meeting_ids = [c.meeting_external_id for c in consultations if c.meeting_external_id]
        agenda_item_ids = [c.agenda_item_external_id for c in consultations if c.agenda_item_external_id]

        # Batch lookup for meetings
        meetings_by_id = {}
        if meeting_ids:
            meetings = OParlMeeting.objects.filter(
                external_id__in=meeting_ids
            ).prefetch_related('organizations')
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

            result.append({
                'consultation': consultation,
                'meeting': meeting,
                'agenda_item': agenda_item,
                'date': meeting.start if meeting else None,
                'organization_name': org_name,
                'meeting_name': meeting.name if meeting else None,
                'agenda_number': agenda_item.number if agenda_item else None,
                'result': getattr(agenda_item, 'result', None) if agenda_item else None,
                'public': getattr(agenda_item, 'public', True) if agenda_item else True,
                'role': consultation.role,
                'authoritative': consultation.authoritative,
            })

        # Sort by date (oldest first = chronological order)
        result.sort(key=lambda x: x['date'] or timezone.now(), reverse=False)

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
        meetings = OParlMeeting.objects.filter(body=body).prefetch_related(
            "organizations"
        )

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
        context["organizations"] = OParlOrganization.objects.filter(
            body=body
        ).order_by("name")

        # Filter by year
        year = self.request.GET.get("year")
        if year:
            try:
                meetings = meetings.filter(start__year=int(year))
                context["selected_year"] = int(year)
            except ValueError:
                pass

        context["years"] = OParlMeeting.objects.filter(
            body=body,
            start__isnull=False
        ).dates("start", "year", order="DESC")

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

        meeting = get_object_or_404(
            OParlMeeting,
            id=kwargs.get("meeting_id"),
            body=body
        )

        context["meeting"] = meeting
        context["body"] = body

        # Get agenda items with related papers
        agenda_items = meeting.agenda_items.order_by("order", "number")

        # Enrich with papers
        items_with_papers = []
        for item in agenda_items:
            papers = item.get_papers()
            items_with_papers.append({
                "item": item,
                "papers": papers,
            })

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

        from insight_core.models import OParlOrganization

        # Base queryset
        organizations = OParlOrganization.objects.filter(body=body)

        # Filter by type
        org_type = self.request.GET.get("type")
        if org_type:
            organizations = organizations.filter(organization_type=org_type)
            context["selected_type"] = org_type

        # Filter active/all
        show_inactive = self.request.GET.get("inactive") == "1"
        if not show_inactive:
            today = timezone.now().date()
            organizations = organizations.filter(
                Q(end_date__isnull=True) | Q(end_date__gte=today)
            )
        context["show_inactive"] = show_inactive

        # Get available types
        context["org_types"] = OParlOrganization.objects.filter(
            body=body
        ).values_list("organization_type", flat=True).distinct().order_by("organization_type")

        # Annotate with member count
        organizations = organizations.annotate(
            member_count=Count("memberships")
        ).order_by("name")

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

        org = get_object_or_404(
            OParlOrganization,
            id=kwargs.get("org_id"),
            body=body
        )

        context["org"] = org
        context["body"] = body

        # Get members
        context["memberships"] = org.memberships.select_related(
            "person"
        ).order_by("-start_date")

        # Get recent meetings
        context["recent_meetings"] = org.meetings.order_by("-start")[:10]

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

        from insight_core.models import OParlPerson

        # Base queryset
        persons = OParlPerson.objects.filter(body=body)

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            persons = persons.filter(
                Q(name__icontains=search) |
                Q(family_name__icontains=search) |
                Q(given_name__icontains=search)
            )
            context["search_query"] = search

        # Order
        persons = persons.annotate(
            membership_count=Count("memberships")
        ).order_by("family_name", "given_name")

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

        person = get_object_or_404(
            OParlPerson,
            id=kwargs.get("person_id"),
            body=body
        )

        context["person"] = person
        context["body"] = body

        # Get memberships
        context["memberships"] = person.memberships.select_related(
            "organization"
        ).order_by("-start_date")

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
            } if body.bbox_north else None
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
        papers = OParlPaper.objects.filter(
            body=body,
            locations__isnull=False
        ).exclude(locations=[])[:500]

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
                    features.append({
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [float(lng), float(lat)]
                        },
                        "properties": {
                            "id": str(paper.id),
                            "title": paper.name or "Vorgang",
                            "reference": paper.reference,
                            "date": paper.date.isoformat() if paper.date else None,
                            "location_name": location.get("name", ""),
                        }
                    })

        return JsonResponse({
            "type": "FeatureCollection",
            "features": features
        })


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
        papers = OParlPaper.objects.filter(
            body=body
        ).filter(
            Q(name__icontains=query) |
            Q(reference__icontains=query)
        ).order_by("-date")[:10]

        # Search meetings
        meetings = OParlMeeting.objects.filter(
            body=body
        ).filter(
            Q(name__icontains=query) |
            Q(location_name__icontains=query)
        ).prefetch_related("organizations").order_by("-start")[:10]

        # Search organizations
        organizations = OParlOrganization.objects.filter(
            body=body
        ).filter(
            Q(name__icontains=query) |
            Q(short_name__icontains=query)
        ).order_by("name")[:10]

        # Search persons
        persons = OParlPerson.objects.filter(
            body=body
        ).filter(
            Q(name__icontains=query) |
            Q(family_name__icontains=query) |
            Q(given_name__icontains=query)
        ).order_by("family_name")[:10]

        context["results"] = {
            "papers": papers,
            "meetings": meetings,
            "organizations": organizations,
            "persons": persons,
        }

        context["total_results"] = (
            papers.count() +
            meetings.count() +
            organizations.count() +
            persons.count()
        )

        return context
