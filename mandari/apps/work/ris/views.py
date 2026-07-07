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


class RISBodiesMixin:
    """
    Multi-Kommune-Unterstützung für RIS-Views.

    Eine Organisation kann mit mehreren OParl-Bodies verknüpft sein
    (Organization.bodies M2M + primärer FK Organization.body).
    """

    def get_bodies(self):
        """Alle verknüpften Kommunen als QuerySet (für body__in-Filter)."""
        return self.organization.get_all_bodies()

    def setup_body_context(self, context):
        """
        Setzt bodies/body/no_body_linked in den Context.

        Returns:
            QuerySet der Bodies oder None, wenn keine Kommune verknüpft ist.
        """
        bodies = self.get_bodies()
        if not bodies.exists():
            context["no_body_linked"] = True
            return None
        context["bodies"] = bodies
        context["has_multiple_bodies"] = bodies.count() > 1
        # Primäre Kommune für Anzeige (Subtitle, Karte etc.)
        context["body"] = self.organization.get_primary_body()
        return bodies


class RISOverviewView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS overview page with statistics."""

    template_name = "work/ris/overview.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_overview"

        # Get the linked OParl bodies (multi-Kommune-fähig)
        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

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
        papers_total = OParlPaper.objects.filter(body__in=bodies).count()
        papers_this_year = OParlPaper.objects.filter(body__in=bodies, date__year=today.year).count()

        # Meetings (Sitzungen)
        meetings_total = OParlMeeting.objects.filter(body__in=bodies).count()
        meetings_upcoming = OParlMeeting.objects.filter(
            body__in=bodies, start__gt=timezone.now(), cancelled=False
        ).count()

        # Organizations (Gremien)
        organizations_total = OParlOrganization.objects.filter(body__in=bodies).count()
        organizations_active = (
            OParlOrganization.objects.filter(body__in=bodies)
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .count()
        )

        # Persons (Personen)
        persons_total = OParlPerson.objects.filter(body__in=bodies).count()

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
        context["recent_papers"] = OParlPaper.objects.filter(body__in=bodies).order_by("-date", "-oparl_created")[:5]

        # Upcoming meetings
        context["upcoming_meetings"] = (
            OParlMeeting.objects.filter(body__in=bodies, start__gt=timezone.now(), cancelled=False)
            .prefetch_related("organizations")
            .order_by("start")[:5]
        )

        return context


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


class RISMeetingsView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS meetings list."""

    template_name = "work/ris/meetings.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_meetings"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        from insight_core.models import OParlMeeting, OParlOrganization

        # Base queryset
        meetings = OParlMeeting.objects.filter(body__in=bodies).prefetch_related("organizations")

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
        context["organizations"] = OParlOrganization.objects.filter(body__in=bodies).order_by("name")

        # Filter by year
        year = self.request.GET.get("year")
        if year:
            try:
                meetings = meetings.filter(start__year=int(year))
                context["selected_year"] = int(year)
            except ValueError:
                pass

        context["years"] = OParlMeeting.objects.filter(body__in=bodies, start__isnull=False).dates(
            "start", "year", order="DESC"
        )

        # Pagination
        paginator = Paginator(meetings, 25)
        page = self.request.GET.get("page", 1)
        context["meetings"] = paginator.get_page(page)
        context["paginator"] = paginator

        return context


class RISMeetingDetailView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS meeting detail view."""

    template_name = "work/ris/meeting_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_meetings"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        from insight_core.models import OParlMeeting

        meeting = get_object_or_404(OParlMeeting, id=kwargs.get("meeting_id"), body__in=bodies)

        context["meeting"] = meeting

        # Get agenda items with related papers
        # Natural sort: 1, 2, 10 instead of 1, 10, 2
        import re

        agenda_items = sorted(
            meeting.agenda_items.all(),
            key=lambda x: [
                (0, int(p)) if p.isdigit() else (1, p.lower()) for p in re.split(r"(\d+)", x.number or "999") if p
            ],
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


class RISOrganizationsView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS organizations list."""

    template_name = "work/ris/organizations.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_organizations"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

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
        organizations = OParlOrganization.objects.filter(body__in=bodies).annotate(
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
        all_orgs = OParlOrganization.objects.filter(body__in=bodies).annotate(
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


class RISOrganizationDetailView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS organization detail view."""

    template_name = "work/ris/organization_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_organizations"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        from insight_core.models import OParlOrganization

        org = get_object_or_404(OParlOrganization, id=kwargs.get("org_id"), body__in=bodies)

        context["org"] = org

        today = timezone.now().date()
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

        # Active members (no end_date or end_date >= today)
        all_memberships = org.memberships.select_related("person")
        context["active_members"] = all_memberships.filter(Q(end_date__isnull=True) | Q(end_date__gte=today)).order_by(
            "person__family_name", "person__name"
        )
        # Past members (end_date < today)
        context["past_members"] = all_memberships.filter(end_date__lt=today).order_by("-end_date")

        # Upcoming meetings (today + future)
        context["upcoming_meetings"] = org.meetings.filter(start__gte=today_start, cancelled=False).order_by("start")
        # Past meetings
        context["past_meetings"] = org.meetings.filter(start__lt=today_start).order_by("-start")[:30]

        # Active tab from query parameter
        context["active_tab"] = self.request.GET.get("tab", "members")

        return context


class RISPersonsView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS persons list."""

    template_name = "work/ris/persons.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_persons"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        from insight_core.models import OParlMembership, OParlOrganization, OParlPerson

        # Base queryset
        persons = OParlPerson.objects.filter(body__in=bodies)

        # Council role annotation (like Insight portal)
        today = timezone.now().date()
        rat_orgs = OParlOrganization.objects.filter(body__in=bodies, name="Rat")
        if rat_orgs.exists():
            council_role_sq = Subquery(
                OParlMembership.objects.filter(
                    person=OuterRef("pk"),
                    organization__in=rat_orgs,
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


class RISPersonDetailView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS person detail view."""

    template_name = "work/ris/person_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_persons"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        from insight_core.models import OParlPerson

        person = get_object_or_404(OParlPerson, id=kwargs.get("person_id"), body__in=bodies)

        context["person"] = person

        today = timezone.now().date()

        # Split memberships into active and past
        all_memberships = person.memberships.select_related("organization")
        context["active_memberships"] = all_memberships.filter(
            Q(end_date__isnull=True) | Q(end_date__gte=today)
        ).order_by("organization__name")
        context["past_memberships"] = all_memberships.filter(end_date__lt=today).order_by("-end_date")

        # Active tab
        context["active_tab"] = self.request.GET.get("tab", "memberships")

        return context


class RISMapView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS map view showing geolocalized papers."""

    template_name = "work/ris/map.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_overview"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        # Kartenzentrum: primäre Kommune
        body = context["body"]

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


class RISMapDataView(RISBodiesMixin, WorkViewMixin, View):
    """API endpoint for map data (GeoJSON)."""

    permission_required = "ris.view"

    def get(self, request, *args, **kwargs):
        bodies = self.get_bodies()
        if not bodies.exists():
            return JsonResponse({"type": "FeatureCollection", "features": []})

        from insight_core.models import OParlPaper

        # Get papers with locations
        papers = OParlPaper.objects.filter(body__in=bodies, locations__isnull=False).exclude(locations=[])[:500]

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


class RISFilesView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS files/documents list."""

    template_name = "work/ris/files.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_files"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        from insight_core.models import OParlFile
        from insight_core.views import _annotate_files_with_context

        # Base queryset
        files = OParlFile.objects.filter(body__in=bodies).select_related("paper").order_by("-file_date", "-created_at")

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


class RISSearchView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """
    RIS search across all entities.

    Nutzt Elasticsearch (inkl. OCR-Volltexte der Dokumente) mit Filtern für
    Zeitraum, Gremium und Vorlagen-Art. Bei Organisationen mit mehreren
    Kommunen wird über alle body_ids gesucht (terms-Query) — optional per
    Kommunen-Dropdown auf eine Kommune eingeschränkt. Fällt auf Django-ORM
    zurück, wenn Elasticsearch nicht erreichbar ist.
    """

    template_name = "work/ris/search.html"
    permission_required = "ris.view"

    PAGE_SIZE = 25

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_search"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        all_bodies = list(bodies)
        all_body_ids = [str(b.id) for b in all_bodies]

        # Kommunen-Filter (nur relevant bei mehreren Kommunen)
        body_filter = self.request.GET.get("kommune", "").strip()
        if body_filter and body_filter in all_body_ids:
            search_body_ids = [body_filter]
        else:
            body_filter = ""
            search_body_ids = all_body_ids
        context["body_filter"] = body_filter

        # Filter-Optionen (immer anzeigen, auch ohne Query)
        from insight_core.models import OParlOrganization, OParlPaper

        context["committees"] = (
            OParlOrganization.objects.filter(body__in=bodies)
            .exclude(name="")
            .order_by("name")
            .values_list("name", flat=True)
        )
        context["paper_types"] = (
            OParlPaper.objects.filter(body__in=bodies)
            .exclude(paper_type__isnull=True)
            .exclude(paper_type="")
            .values_list("paper_type", flat=True)
            .distinct()
            .order_by("paper_type")
        )

        query = self.request.GET.get("q", "").strip()
        date_from = self.request.GET.get("von", "").strip()
        date_to = self.request.GET.get("bis", "").strip()
        committee = self.request.GET.get("gremium", "").strip()
        paper_type = self.request.GET.get("art", "").strip()
        result_type = self.request.GET.get("typ", "").strip()
        try:
            page = max(1, int(self.request.GET.get("seite", "1")))
        except ValueError:
            page = 1

        context.update(
            {
                "search_query": query,
                "date_from": date_from,
                "date_to": date_to,
                "committee_filter": committee,
                "paper_type_filter": paper_type,
                "result_type_filter": result_type,
            }
        )

        has_filters = any([date_from, date_to, committee, paper_type, body_filter])
        if not query and not has_filters:
            return context

        index_names = None
        if result_type in ("papers", "meetings", "persons", "organizations", "files"):
            index_names = [result_type]
        elif committee or paper_type:
            # Diese Filter wirken nur auf Dokument-/Sitzungs-Indexe
            index_names = ["papers", "meetings", "files"] if not paper_type else ["papers"]

        try:
            from insight_core.services.search_service import ElasticsearchService

            service = ElasticsearchService()
            result = service.search_all(
                query=query,
                body_ids=search_body_ids,
                page=page,
                page_size=self.PAGE_SIZE,
                index_names=index_names,
                date_from=date_from or None,
                date_to=date_to or None,
                organization_name=committee or None,
                paper_type=paper_type or None,
            )
            # Django-Templates erlauben keinen Zugriff auf _-Attribute
            for doc in result["results"]:
                doc["formatted"] = doc.get("_formatted", {})
            context["es_results"] = result["results"]
            context["total_results"] = result["total"]
            context["page"] = result["page"]
            context["pages"] = result["pages"]
            context["search_backend"] = "elasticsearch"
            return context
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning(f"RIS-Suche: Elasticsearch nicht verfügbar, ORM-Fallback: {e}")

        # ---- Fallback: Django ORM (ohne OCR-Volltext, einfache Filter) ----
        from insight_core.models import OParlMeeting, OParlPerson

        papers = OParlPaper.objects.filter(body_id__in=search_body_ids)
        meetings = OParlMeeting.objects.filter(body_id__in=search_body_ids)
        if query:
            papers = papers.filter(Q(name__icontains=query) | Q(reference__icontains=query))
            meetings = meetings.filter(Q(name__icontains=query) | Q(location_name__icontains=query))
        if date_from:
            papers = papers.filter(date__gte=date_from)
            meetings = meetings.filter(start__gte=date_from)
        if date_to:
            papers = papers.filter(date__lte=date_to)
            meetings = meetings.filter(start__lte=date_to)
        if paper_type:
            papers = papers.filter(paper_type=paper_type)
        if committee:
            meetings = meetings.filter(organizations__name=committee)

        papers = papers.order_by("-date")[:10]
        meetings = meetings.prefetch_related("organizations").order_by("-start")[:10]

        organizations = (
            OParlOrganization.objects.filter(body_id__in=search_body_ids)
            .filter(Q(name__icontains=query) | Q(short_name__icontains=query))
            .order_by("name")[:10]
            if query
            else OParlOrganization.objects.none()
        )
        persons = (
            OParlPerson.objects.filter(body_id__in=search_body_ids)
            .filter(Q(name__icontains=query) | Q(family_name__icontains=query) | Q(given_name__icontains=query))
            .order_by("family_name")[:10]
            if query
            else OParlPerson.objects.none()
        )

        context["results"] = {
            "papers": papers,
            "meetings": meetings,
            "organizations": organizations,
            "persons": persons,
        }
        context["total_results"] = len(papers) + len(meetings) + organizations.count() + persons.count()
        context["search_backend"] = "orm"

        return context
