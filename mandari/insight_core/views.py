"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

import json
from datetime import timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView, ListView, DetailView
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.db.models import Q, Count
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST, require_GET

from django.shortcuts import get_object_or_404

from .models import (
    OParlBody,
    OParlOrganization,
    OParlPerson,
    OParlMeeting,
    OParlAgendaItem,
    OParlPaper,
    TileCache,
)
from .ranking import sort_organizations_by_ranking


# =============================================================================
# Helper Functions
# =============================================================================

def get_active_body(request):
    """Holt die aktive Kommune aus der Session oder setzt einen Standard."""
    body_id = request.session.get("active_body_id")
    if body_id and body_id != "all":
        try:
            return OParlBody.objects.get(id=body_id)
        except OParlBody.DoesNotExist:
            pass
    # Fallback: Erste Kommune als Standard
    default_body = OParlBody.objects.first()
    if default_body:
        request.session["active_body_id"] = str(default_body.id)
        return default_body
    return None


def is_all_bodies_mode(request):
    """Prüft ob der 'Alle Kommunen' Modus aktiv ist."""
    body_id = request.session.get("active_body_id")
    return body_id is None or body_id == "all"


# =============================================================================
# Landingpage (Marketing)
# =============================================================================

class HomeView(TemplateView):
    """Marketing-Landingpage mit Produkt-Übersicht."""
    template_name = "pages/landing.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Globale Statistiken für die Landingpage
        context["stats"] = {
            "bodies": OParlBody.objects.count(),
            "organizations": OParlOrganization.objects.count(),
            "persons": OParlPerson.objects.count(),
            "papers": OParlPaper.objects.count(),
        }

        # Verfügbare Kommunen für die Landingpage
        context["available_bodies"] = OParlBody.objects.all().order_by("name")[:6]

        return context


# =============================================================================
# Portal Homepage (RIS)
# =============================================================================

class PortalHomeView(TemplateView):
    """Portal-Startseite mit Kommune-Auswahl und Statistiken."""
    template_name = "pages/portal/home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        all_bodies_mode = is_all_bodies_mode(self.request)

        context["all_bodies_mode"] = all_bodies_mode

        if all_bodies_mode:
            # Alle Kommunen Übersicht
            context["stats"] = {
                "bodies": OParlBody.objects.count(),
                "organizations": OParlOrganization.objects.count(),
                "persons": OParlPerson.objects.count(),
                "meetings": OParlMeeting.objects.count(),
                "papers": OParlPaper.objects.count(),
            }
            context["upcoming_meetings"] = None
            context["recent_papers"] = None

        elif body:
            # Statistiken für die aktive Kommune
            context["stats"] = {
                "organizations": OParlOrganization.objects.filter(body=body).count(),
                "persons": OParlPerson.objects.filter(body=body).count(),
                "meetings": OParlMeeting.objects.filter(body=body).count(),
                "papers": OParlPaper.objects.filter(body=body).count(),
            }

            # Nächste Sitzungen
            context["upcoming_meetings"] = OParlMeeting.objects.filter(
                body=body,
                start__gte=timezone.now(),
                cancelled=False
            ).prefetch_related("organizations").order_by("start")[:5]

            # Neueste Vorgänge
            context["recent_papers"] = OParlPaper.objects.filter(
                body=body
            ).order_by("-date", "-oparl_created")[:5]

        return context


def set_body(request, body_id):
    """Setzt die aktive Kommune und leitet zur Portal-Homepage weiter."""
    from django.utils.http import url_has_allowed_host_and_scheme

    try:
        body = OParlBody.objects.get(id=body_id)
        request.session["active_body_id"] = str(body.id)
        # Explicitly mark session as modified and save to ensure persistence
        request.session.modified = True
        request.session.save()
    except OParlBody.DoesNotExist:
        pass

    # SECURITY: Use Django's built-in URL validation to prevent Open Redirect
    default_redirect = "/insight/"
    referer = request.META.get("HTTP_REFERER", "")

    # For HTMX requests, use HX-Redirect header for reliable navigation
    is_htmx = request.headers.get("HX-Request") == "true"

    if referer and url_has_allowed_host_and_scheme(
        referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        redirect_url = referer
    else:
        redirect_url = default_redirect

    if is_htmx:
        response = HttpResponse(status=200)
        response["HX-Redirect"] = redirect_url
        return response

    return redirect(redirect_url)


def clear_body(request):
    """Setzt auf 'Alle Kommunen' Modus und leitet zur Portal-Homepage weiter."""
    request.session["active_body_id"] = "all"
    # Explicitly mark session as modified and save to ensure persistence
    request.session.modified = True
    request.session.save()

    # For HTMX requests, use HX-Redirect header
    is_htmx = request.headers.get("HX-Request") == "true"
    redirect_url = "/insight/"

    if is_htmx:
        response = HttpResponse(status=200)
        response["HX-Redirect"] = redirect_url
        return response

    return redirect(redirect_url)


# =============================================================================
# Gremien (Organizations)
# =============================================================================

class OrganizationListView(TemplateView):
    """Liste aller Gremien mit Aktiv/Vergangen-Tabs."""
    template_name = "pages/organizations/list.html"

    def get_template_names(self):
        # Für HTMX-Requests nur das Partial zurückgeben
        if self.request.headers.get("HX-Request"):
            return ["partials/organization_list_items.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        tab = self.request.GET.get("tab", "active")
        q = self.request.GET.get("q", "").strip()

        if body:
            today = timezone.now().date()
            base_qs = OParlOrganization.objects.filter(body=body)

            # Suche
            if q:
                base_qs = base_qs.filter(
                    Q(name__icontains=q) | Q(short_name__icontains=q)
                )

            if tab == "active":
                orgs = base_qs.filter(
                    Q(end_date__isnull=True) | Q(end_date__gte=today)
                )
            else:
                orgs = base_qs.filter(end_date__lt=today)

            context["organizations"] = sort_organizations_by_ranking(orgs)

            # Counts ohne Suchfilter
            all_orgs = OParlOrganization.objects.filter(body=body)
            context["active_count"] = all_orgs.filter(
                Q(end_date__isnull=True) | Q(end_date__gte=today)
            ).count()
            context["past_count"] = all_orgs.filter(end_date__lt=today).count()

        context["tab"] = tab
        return context


class OrganizationDetailView(DetailView):
    """Detailseite eines Gremiums."""
    model = OParlOrganization
    template_name = "pages/organizations/detail.html"
    context_object_name = "organization"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.object

        # Mitglieder
        context["members"] = org.memberships.select_related("person").filter(
            Q(end_date__isnull=True) | Q(end_date__gte=timezone.now().date())
        ).order_by("person__family_name")

        return context


class OrganizationListPartial(ListView):
    """HTMX Partial für Gremien-Liste."""
    model = OParlOrganization
    template_name = "partials/organization_list.html"
    context_object_name = "organizations"
    paginate_by = 20

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlOrganization.objects.none()

        tab = self.request.GET.get("tab", "active")
        today = timezone.now().date()
        base_qs = OParlOrganization.objects.filter(body=body)

        if tab == "active":
            qs = base_qs.filter(
                Q(end_date__isnull=True) | Q(end_date__gte=today)
            )
        else:
            qs = base_qs.filter(end_date__lt=today)
        return sort_organizations_by_ranking(qs)


# =============================================================================
# Personen
# =============================================================================

class PersonListView(ListView):
    """Liste aller Personen."""
    model = OParlPerson
    template_name = "pages/persons/list.html"
    context_object_name = "persons"
    paginate_by = 50

    def get_template_names(self):
        # Für HTMX-Requests nur das Partial zurückgeben
        if self.request.headers.get("HX-Request"):
            return ["partials/person_list_items.html"]
        return [self.template_name]

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlPerson.objects.none()

        qs = OParlPerson.objects.filter(body=body)

        # Suche
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(family_name__icontains=q) |
                Q(given_name__icontains=q)
            )

        return qs.order_by("family_name", "given_name")


class PersonDetailView(DetailView):
    """Detailseite einer Person."""
    model = OParlPerson
    template_name = "pages/persons/detail.html"
    context_object_name = "person"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        person = self.object

        # Mitgliedschaften
        context["memberships"] = person.memberships.select_related(
            "organization"
        ).order_by("-start_date")

        return context


class PersonListPartial(ListView):
    """HTMX Partial für Personen-Liste."""
    model = OParlPerson
    template_name = "partials/person_list.html"
    context_object_name = "persons"
    paginate_by = 20


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
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(reference__icontains=q)
            )

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
            context["paper_types"] = OParlPaper.objects.filter(
                body=body
            ).exclude(
                paper_type__isnull=True
            ).values_list(
                "paper_type", flat=True
            ).distinct().order_by("paper_type")

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
        context["files_with_text"] = [
            f for f in files
            if f.text_content and f.text_content.strip()
        ]

        # Beratungsverlauf (Consultations mit Meeting-Info)
        context["consultations"] = self._get_consultations_with_meetings(paper)

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
            meetings = OParlMeeting.objects.filter(
                external_id__in=meeting_ids
            ).prefetch_related('organizations')
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

            result.append({
                'consultation': consultation,
                'meeting': meeting,
                'agenda_item': agenda_item,
                'date': meeting.start if meeting else None,
                'organization_name': meeting.get_display_name() if meeting else None,
                'agenda_number': agenda_item.number if agenda_item else None,
                'result': agenda_item.result if agenda_item else None,
                'public': agenda_item.public if agenda_item else True,
                'role': consultation.role,
                'authoritative': consultation.authoritative,
            })

        # Sortiere nach Datum (älteste zuerst = chronologischer Verlauf)
        result.sort(key=lambda x: x['date'] or timezone.now(), reverse=False)

        return result


class PaperListPartial(ListView):
    """HTMX Partial für Vorgänge-Liste."""
    model = OParlPaper
    template_name = "partials/paper_list.html"
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
        return render(request, "partials/paper_summary.html", {
            "paper": paper,
            "summary": paper.summary,
        })

    # Generate new summary
    try:
        from insight_ai.services.summarizer import (
            SummaryService,
            NoTextContentError,
            APINotConfiguredError,
            SummaryError,
        )

        service = SummaryService()
        summary = service.generate_summary(paper)

        return render(request, "partials/paper_summary.html", {
            "paper": paper,
            "summary": summary,
        })

    except NoTextContentError as e:
        return render(request, "partials/paper_summary.html", {
            "paper": paper,
            "error": str(e),
        })

    except APINotConfiguredError as e:
        return render(request, "partials/paper_summary.html", {
            "paper": paper,
            "error": str(e),
        })

    except SummaryError as e:
        return render(request, "partials/paper_summary.html", {
            "paper": paper,
            "error": str(e),
        })

    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.exception(f"Unexpected error in paper_summary: {e}")
        return render(request, "partials/paper_summary.html", {
            "paper": paper,
            "error": f"Unerwarteter Fehler: {str(e)}",
        })


# =============================================================================
# Termine (Meetings)
# =============================================================================

class MeetingListView(ListView):
    """Liste aller Sitzungen."""
    model = OParlMeeting
    template_name = "pages/meetings/list.html"
    context_object_name = "meetings"
    paginate_by = 25

    def get_template_names(self):
        # Für HTMX-Requests nur das Partial zurückgeben
        if self.request.headers.get("HX-Request"):
            return ["partials/meeting_list_items.html"]
        return [self.template_name]

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlMeeting.objects.none()

        qs = OParlMeeting.objects.filter(body=body).prefetch_related("organizations")

        # Suche
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(location_name__icontains=q)
            )

        # Zeitraum-Filter
        period = self.request.GET.get("period", "upcoming")
        now = timezone.now()

        if period == "upcoming":
            qs = qs.filter(start__gte=now, cancelled=False)
            return qs.order_by("start")
        elif period == "past":
            qs = qs.filter(start__lt=now)
        # "all" zeigt alles

        return qs.order_by("-start")


class MeetingCalendarView(TemplateView):
    """Kalenderansicht der Sitzungen."""
    template_name = "pages/meetings/calendar.html"


class MeetingDetailView(DetailView):
    """Detailseite einer Sitzung."""
    model = OParlMeeting
    template_name = "pages/meetings/detail.html"
    context_object_name = "meeting"

    def get_queryset(self):
        return OParlMeeting.objects.prefetch_related("organizations")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        meeting = self.object

        # Tagesordnungspunkte
        context["agenda_items"] = meeting.agenda_items.all().order_by("order", "number")

        # Location Koordinaten für Karte
        if meeting.location_name and meeting.body:
            from .models import LocationMapping
            coords = LocationMapping.get_coordinates_for_location(
                meeting.body,
                meeting.location_name
            )
            context["location_coordinates"] = coords

        return context


class MeetingListPartial(ListView):
    """HTMX Partial für Sitzungen-Liste."""
    model = OParlMeeting
    template_name = "partials/meeting_list.html"
    context_object_name = "meetings"
    paginate_by = 20


@require_GET
def calendar_events(request):
    """JSON-Endpoint für Kalender-Events (FullCalendar/Alpine.js)."""
    body = get_active_body(request)
    if not body:
        return JsonResponse([], safe=False)

    # Zeitraum aus Request (FullCalendar sendet start/end)
    start_str = request.GET.get("start")
    end_str = request.GET.get("end")

    qs = OParlMeeting.objects.filter(body=body, cancelled=False).prefetch_related("organizations")

    if start_str:
        from datetime import datetime
        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            qs = qs.filter(start__gte=start)
        except ValueError:
            pass

    if end_str:
        from datetime import datetime
        try:
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            qs = qs.filter(start__lte=end)
        except ValueError:
            pass

    events = []
    for meeting in qs:
        full_title = meeting.get_display_name()
        # Truncate long titles for calendar display
        title = full_title[:37] + "..." if len(full_title) > 40 else full_title

        events.append({
            "id": str(meeting.id),
            "title": title,
            "start": meeting.start.isoformat() if meeting.start else None,
            "end": meeting.end.isoformat() if meeting.end else None,
            "url": f"/insight/termine/{meeting.id}/",
            "extendedProps": {
                "location": meeting.location_name,
                "fullTitle": full_title,
            }
        })

    return JsonResponse(events, safe=False)


# =============================================================================
# Suche
# =============================================================================

class SearchView(TemplateView):
    """Suchseite mit erweiterter Filterung."""
    template_name = "pages/search.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["query"] = self.request.GET.get("q", "")
        context["search_type"] = self.request.GET.get("type", "all")
        context["available_types"] = [
            ("all", "Alle"),
            ("paper", "Vorgänge"),
            ("meeting", "Sitzungen"),
            ("person", "Personen"),
            ("organization", "Gremien"),
            ("file", "Dokumente"),
        ]
        return context


@require_GET
def search_results(request):
    """
    HTMX Endpoint für Suchergebnisse.

    Nutzt Meilisearch für Volltextsuche.
    """
    query = request.GET.get("q", "").strip()
    search_type = request.GET.get("type", "all")
    page = int(request.GET.get("page", 1))
    body = get_active_body(request)

    if not query or len(query) < 2:
        return render(request, "partials/search_results.html", {
            "results": [],
            "query": query,
        })

    # Meilisearch verwenden
    try:
        from .services.search_service import get_search_service, format_search_result, INDEX_MEETINGS, INDEX_PAPERS, INDEX_PERSONS, INDEX_ORGANIZATIONS, INDEX_FILES

        search_service = get_search_service()

        # Body-ID für Filter
        body_id = str(body.id) if body else None

        # Index-Auswahl basierend auf Typ
        index_map = {
            "all": None,  # Alle Indexe
            "paper": [INDEX_PAPERS],
            "meeting": [INDEX_MEETINGS],
            "person": [INDEX_PERSONS],
            "organization": [INDEX_ORGANIZATIONS],
            "file": [INDEX_FILES],
        }
        index_names = index_map.get(search_type)

        # Suche ausführen
        search_result = search_service.search_all(
            query=query,
            body_id=body_id,
            page=page,
            page_size=20,
            index_names=index_names,
        )

        # Ergebnisse formatieren
        results = [format_search_result(hit) for hit in search_result["results"]]

        return render(request, "partials/search_results.html", {
            "results": results,
            "query": query,
            "total": search_result["total"],
            "page": search_result["page"],
            "pages": search_result["pages"],
            "search_type": search_type,
        })

    except Exception as e:
        # Fallback auf Django-Suche bei Fehler
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Meilisearch-Suche fehlgeschlagen, Fallback auf Django: {e}")

        results = []

        if body:
            # Vorgänge
            papers = OParlPaper.objects.filter(
                body=body
            ).filter(
                Q(name__icontains=query) | Q(reference__icontains=query)
            )[:10]
            for paper in papers:
                results.append({
                    "type": "paper",
                    "title": paper.name or paper.reference,
                    "subtitle": paper.paper_type,
                    "url": f"/vorgaenge/{paper.id}/",
                })

            # Personen
            persons = OParlPerson.objects.filter(
                body=body
            ).filter(
                Q(name__icontains=query) |
                Q(family_name__icontains=query) |
                Q(given_name__icontains=query)
            )[:10]
            for person in persons:
                results.append({
                    "type": "person",
                    "title": person.display_name,
                    "subtitle": "Person",
                    "url": f"/personen/{person.id}/",
                })

            # Gremien
            orgs = OParlOrganization.objects.filter(
                body=body
            ).filter(
                Q(name__icontains=query) | Q(short_name__icontains=query)
            )[:10]
            for org in orgs:
                results.append({
                    "type": "organization",
                    "title": org.name,
                    "subtitle": org.organization_type,
                    "url": f"/gremien/{org.id}/",
                })

            # Sitzungen
            meetings = OParlMeeting.objects.filter(
                body=body
            ).filter(
                Q(name__icontains=query) | Q(location_name__icontains=query)
            )[:10]
            for meeting in meetings:
                results.append({
                    "type": "meeting",
                    "title": meeting.name or "Sitzung",
                    "subtitle": meeting.start.strftime("%d.%m.%Y") if meeting.start else None,
                    "url": f"/termine/{meeting.id}/",
                })

        return render(request, "partials/search_results.html", {
            "results": results,
            "query": query,
            "total": len(results),
        })


# =============================================================================
# Karte
# =============================================================================

class MapView(TemplateView):
    """Kartenansicht mit Vorgängen der letzten 4 Wochen."""
    template_name = "pages/map.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)

        if body:
            context["active_body"] = body

            # Geodaten für initiale Kartenansicht
            if body.latitude and body.longitude:
                context["map_center"] = {
                    "lat": float(body.latitude),
                    "lng": float(body.longitude),
                }

            # Bounding Box für Zoom
            if body.bbox_north and body.bbox_south and body.bbox_east and body.bbox_west:
                context["map_bounds"] = {
                    "north": float(body.bbox_north),
                    "south": float(body.bbox_south),
                    "east": float(body.bbox_east),
                    "west": float(body.bbox_west),
                }

        return context


@require_GET
def map_markers(request):
    """GeoJSON-Endpoint für Karten-Marker."""
    body = get_active_body(request)
    if not body:
        return JsonResponse({"type": "FeatureCollection", "features": []})

    # Vorgänge der letzten 4 Wochen mit Geo-Daten
    four_weeks_ago = timezone.now() - timedelta(weeks=4)

    papers = OParlPaper.objects.filter(
        body=body,
        date__gte=four_weeks_ago,
        locations__isnull=False
    )

    features = []
    for paper in papers:
        if paper.locations and isinstance(paper.locations, list):
            for loc in paper.locations:
                if "lat" in loc and "lon" in loc:
                    features.append({
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [loc["lon"], loc["lat"]]
                        },
                        "properties": {
                            "id": str(paper.id),
                            "title": paper.name,
                            "reference": paper.reference,
                            "url": f"/vorgaenge/{paper.id}/",
                            "location_name": loc.get("name", ""),
                        }
                    })

    return JsonResponse({
        "type": "FeatureCollection",
        "features": features
    })


# =============================================================================
# Tile Proxy (DSGVO-konform)
# =============================================================================

import httpx
from django.views.decorators.cache import cache_page

@require_GET
def tile_proxy(request, z, x, y):
    """
    Proxy für OpenStreetMap Raster-Tiles (für Leaflet).

    1. Prüft zuerst den lokalen Tile-Cache (Datenbank)
    2. Falls nicht im Cache, lädt von OSM und speichert im Cache
    3. Liefert das Tile aus

    Dies ist 100% DSGVO-konform, da alle Tiles serverseitig geladen werden.
    OSM Tile Usage Policy: https://operations.osmfoundation.org/policies/tiles/
    """
    # 1. Prüfe den lokalen Cache
    tile_data, content_type = TileCache.get_tile(z, x, y)

    if tile_data:
        # Tile aus Cache liefern (super schnell!)
        # SECURITY NOTE: CORS "*" is intentional for public map tiles.
        # Map tiles must be accessible from any origin for proper rendering.
        # This endpoint only serves static, public image data with no auth.
        return HttpResponse(
            tile_data,
            content_type=content_type,
            headers={
                "Cache-Control": "public, max-age=604800",  # 7 Tage Browser-Cache
                "Access-Control-Allow-Origin": "*",  # nosec: intentional for public tiles
                "X-Tile-Source": "cache",
            }
        )

    # 2. Nicht im Cache - von OSM laden
    subdomain = ['a', 'b', 'c'][x % 3]
    tile_url = f"https://{subdomain}.tile.openstreetmap.org/{z}/{x}/{y}.png"

    try:
        with httpx.Client(timeout=10.0, headers={
            "User-Agent": "Mandari/1.0 (https://mandari.dev; contact@mandari.dev)"
        }) as client:
            response = client.get(tile_url)

            if response.status_code == 200:
                # Im Cache speichern für zukünftige Requests
                TileCache.store_tile(z, x, y, response.content, "image/png", "openstreetmap")

                # SECURITY NOTE: CORS "*" is intentional for public map tiles.
                return HttpResponse(
                    response.content,
                    content_type="image/png",
                    headers={
                        "Cache-Control": "public, max-age=604800",  # 7 Tage Browser-Cache
                        "Access-Control-Allow-Origin": "*",  # nosec: intentional for public tiles
                        "X-Tile-Source": "osm",
                    }
                )
            else:
                from django.http import HttpResponseNotFound
                return HttpResponseNotFound()
    except Exception as e:
        from django.http import HttpResponseServerError
        return HttpResponseServerError(f"Tile proxy error: {str(e)}")


@require_GET
@cache_page(60 * 60 * 24)  # Cache für 24 Stunden
def style_proxy(request):
    """
    Proxy für VersaTiles Style JSON.

    Lädt die Style-Konfiguration und ersetzt die Tile-URLs
    mit lokalen Proxy-URLs.
    """
    style_url = "https://tiles.versatiles.org/assets/styles/colorful/style.json"

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(style_url)

            if response.status_code == 200:
                import json
                style = response.json()

                # Ersetze externe Tile-URLs mit lokalem Proxy
                if "sources" in style:
                    for source_name, source in style["sources"].items():
                        if "tiles" in source:
                            # Ersetze VersaTiles URL mit lokalem Proxy
                            source["tiles"] = [
                                request.build_absolute_uri("/insight/tiles/{z}/{x}/{y}")
                            ]
                        if "url" in source:
                            # Für TileJSON URLs
                            del source["url"]
                            source["tiles"] = [
                                request.build_absolute_uri("/insight/tiles/{z}/{x}/{y}")
                            ]

                # Ersetze Sprite und Glyphs URLs
                base_url = request.build_absolute_uri("/static/vendor/maplibre/")
                if "sprite" in style:
                    style["sprite"] = request.build_absolute_uri("/insight/map-assets/sprite")
                if "glyphs" in style:
                    style["glyphs"] = request.build_absolute_uri("/insight/map-assets/glyphs/{fontstack}/{range}.pbf")

                return JsonResponse(style, safe=False)
            else:
                return JsonResponse({"error": "Style not found"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_GET
@cache_page(60 * 60 * 24)
def map_sprite(request, filename="sprite"):
    """Proxy für Map Sprites."""
    ext = request.GET.get("ext", "json")
    if filename.endswith(".png"):
        ext = "png"
        filename = filename[:-4]
    elif filename.endswith(".json"):
        ext = "json"
        filename = filename[:-5]

    sprite_url = f"https://tiles.versatiles.org/assets/styles/colorful/{filename}.{ext}"

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(sprite_url)
            if response.status_code == 200:
                from django.http import HttpResponse
                content_type = "application/json" if ext == "json" else "image/png"
                return HttpResponse(response.content, content_type=content_type)
    except:
        pass

    from django.http import HttpResponseNotFound
    return HttpResponseNotFound()


@require_GET
@cache_page(60 * 60 * 24)
def map_glyphs(request, fontstack, range_):
    """Proxy für Map Glyphs (Fonts)."""
    glyphs_url = f"https://tiles.versatiles.org/assets/fonts/{fontstack}/{range_}.pbf"

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(glyphs_url)
            if response.status_code == 200:
                from django.http import HttpResponse
                return HttpResponse(
                    response.content,
                    content_type="application/x-protobuf"
                )
    except:
        pass

    from django.http import HttpResponseNotFound
    return HttpResponseNotFound()


# =============================================================================
# Chat (KI-Assistent)
# =============================================================================

class ChatView(TemplateView):
    """KI-Chat-Interface."""
    template_name = "pages/chat.html"


@require_POST
def chat_message(request):
    """
    API-Endpoint für Chat-Nachrichten.

    Nutzt Groq API via insight_ai.
    """
    import json

    try:
        data = json.loads(request.body)
        message = data.get("message", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if not message:
        return JsonResponse({"error": "Message is required"}, status=400)

    # Prüfe DSGVO-Consent
    if not request.session.get("chat_consent"):
        return JsonResponse({
            "error": "consent_required",
            "message": "Bitte stimmen Sie der Datenverarbeitung zu."
        }, status=403)

    # TODO: Groq API Integration via insight_ai
    # Für jetzt: Placeholder-Antwort
    return JsonResponse({
        "response": "Der KI-Assistent ist noch nicht konfiguriert. "
                   "Bitte konfigurieren Sie den GROQ_API_KEY in der .env Datei.",
        "sources": []
    })


# =============================================================================
# Rechtliche Seiten
# =============================================================================

class ImpressumView(TemplateView):
    """Impressum."""
    template_name = "pages/legal/impressum.html"


class DatenschutzView(TemplateView):
    """Datenschutzerklärung."""
    template_name = "pages/legal/datenschutz.html"


class AGBView(TemplateView):
    """Allgemeine Geschäftsbedingungen."""
    template_name = "pages/legal/agb.html"


# =============================================================================
# Über Mandari Seiten
# =============================================================================

class ProduktView(TemplateView):
    """Produktübersicht - Mandari Work & RIS."""
    template_name = "pages/about/produkt.html"


class LoesungenView(TemplateView):
    """Lösungen für Fraktionen, Verwaltungen, Bürger."""
    template_name = "pages/about/loesungen.html"


class SicherheitView(TemplateView):
    """Sicherheit & Datenschutz."""
    template_name = "pages/about/sicherheit.html"


class OpenSourceView(TemplateView):
    """Open Source Philosophie."""
    template_name = "pages/about/open-source.html"


class KontaktView(TemplateView):
    """Kontaktseite mit Formular."""
    template_name = "pages/about/kontakt.html"

    # Target email for contact form notifications
    CONTACT_EMAIL = "hello@mandari.de"

    def get_client_ip(self, request):
        """Extract client IP address from request."""
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR")

    def post(self, request, *args, **kwargs):
        """Verarbeitet das Kontaktformular."""
        from django.contrib import messages
        from apps.common.email import send_template_email
        from .models import ContactRequest
        import logging

        logger = logging.getLogger(__name__)

        # Honeypot Check
        if request.POST.get("website"):
            # Bot detected - silently fail
            logger.warning(f"Honeypot triggered from IP {self.get_client_ip(request)}")
            messages.error(request, "Ihre Nachricht konnte nicht gesendet werden.")
            return self.get(request, *args, **kwargs)

        name = request.POST.get("name", "").strip()
        organization = request.POST.get("organization", "").strip()
        email = request.POST.get("email", "").strip()
        subject = request.POST.get("subject", "").strip()
        message_text = request.POST.get("message", "").strip()
        privacy = request.POST.get("privacy")

        # Validierung
        if not all([name, email, subject, message_text, privacy]):
            messages.error(request, "Bitte füllen Sie alle Pflichtfelder aus.")
            return self.get(request, *args, **kwargs)

        # E-Mail-Validierung
        from django.core.validators import validate_email
        from django.core.exceptions import ValidationError
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Bitte geben Sie eine gültige E-Mail-Adresse ein.")
            return self.get(request, *args, **kwargs)

        # ContactRequest erstellen und speichern
        try:
            contact = ContactRequest.objects.create(
                name=name,
                email=email,
                organization_name=organization,
                subject=subject,
                message=message_text,
                ip_address=self.get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
            )
            logger.info(f"ContactRequest created: {contact.id}")
        except Exception as e:
            logger.error(f"Failed to create ContactRequest: {e}")
            messages.error(request, "Es ist ein Fehler aufgetreten. Bitte versuchen Sie es später erneut.")
            return self.get(request, *args, **kwargs)

        # E-Mail-Kontext vorbereiten
        email_context = {
            "contact": contact,
            "admin_url": request.build_absolute_uri(f"/admin/insight_core/contactrequest/{contact.id}/change/"),
        }

        # 1. Benachrichtigung an hello@mandari.de senden
        try:
            subject_map = {
                "demo": "Demo-Anfrage",
                "preise": "Preisanfrage",
                "support": "Support-Anfrage",
                "datenschutz": "Datenschutz-Anfrage",
                "sonstiges": "Kontaktanfrage",
            }
            email_subject = f"[Mandari] {subject_map.get(subject, 'Kontaktanfrage')} von {name}"

            notification_sent = send_template_email(
                subject=email_subject,
                template_name="emails/contact/notification",
                context=email_context,
                to=[self.CONTACT_EMAIL],
                reply_to=[email],
                fail_silently=True,
            )
            if notification_sent:
                contact.notification_sent = True
                logger.info(f"Notification email sent for ContactRequest {contact.id}")
            else:
                logger.warning(f"Failed to send notification email for ContactRequest {contact.id}")
        except Exception as e:
            logger.error(f"Error sending notification email: {e}")

        # 2. Bestätigung an den Absender senden
        try:
            confirmation_sent = send_template_email(
                subject="Ihre Anfrage bei Mandari - Bestätigung",
                template_name="emails/contact/confirmation",
                context=email_context,
                to=[email],
                fail_silently=True,
            )
            if confirmation_sent:
                contact.confirmation_sent = True
                logger.info(f"Confirmation email sent for ContactRequest {contact.id}")
            else:
                logger.warning(f"Failed to send confirmation email for ContactRequest {contact.id}")
        except Exception as e:
            logger.error(f"Error sending confirmation email: {e}")

        # E-Mail-Status speichern
        contact.save(update_fields=["notification_sent", "confirmation_sent"])

        # Erfolgsmeldung
        messages.success(
            request,
            "Vielen Dank für Ihre Nachricht! Wir haben Ihnen eine Bestätigung per E-Mail gesendet und werden uns schnellstmöglich bei Ihnen melden."
        )
        return self.get(request, *args, **kwargs)


# =============================================================================
# Neue Seiten (Preise, Mitmachen, Team, FAQ, Releases, Partner, Blog)
# =============================================================================

class PreiseView(TemplateView):
    """Preise - Transparente Preisübersicht."""
    template_name = "pages/about/preise.html"


class MitmachenView(TemplateView):
    """Mitmachen - Projekt unterstützen."""
    template_name = "pages/about/mitmachen.html"


class TeamView(TemplateView):
    """Team - Die Menschen hinter Mandari."""
    template_name = "pages/about/team.html"


class FAQView(TemplateView):
    """FAQ - Häufig gestellte Fragen."""
    template_name = "pages/about/faq.html"


class ReleasesView(TemplateView):
    """Releases - Versionshistorie."""
    template_name = "pages/about/releases.html"


class PartnerView(TemplateView):
    """Partner - Gemeinsam für Transparenz."""
    template_name = "pages/about/partner.html"


class BlogView(TemplateView):
    """Blog - Neuigkeiten und Einblicke (Platzhalter)."""
    template_name = "pages/about/blog.html"


class UeberUnsView(TemplateView):
    """Mission & Werte - Warum Mandari existiert."""
    template_name = "pages/about/ueber-uns.html"


class KommunenView(TemplateView):
    """Kommunen - Übersicht aller angebundenen Städte."""
    template_name = "pages/about/kommunen.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["bodies"] = OParlBody.objects.all().order_by("name")
        context["stats"] = {
            "bodies": OParlBody.objects.count(),
            "organizations": OParlOrganization.objects.count(),
            "persons": OParlPerson.objects.count(),
            "meetings": OParlMeeting.objects.count(),
            "papers": OParlPaper.objects.count(),
        }
        return context


class RoadmapView(TemplateView):
    """Roadmap - Meilensteine und Zukunftsplanung."""
    template_name = "pages/about/roadmap.html"


class PresseView(TemplateView):
    """Presse & Medien - Ressourcen für Journalisten."""
    template_name = "pages/about/presse.html"


class DanksagungenView(TemplateView):
    """Danksagungen & Abhängigkeiten - Transparenz über verwendete Projekte."""
    template_name = "pages/about/danksagungen.html"


# =============================================================================
# Öffentliche Fraktionsprotokolle
# =============================================================================

class PublicProtocolListView(TemplateView):
    """
    Öffentliches Dashboard für genehmigte Fraktionsprotokolle.

    Zeigt nur Protokolle die:
    - Status 'approved' haben
    - Zu einer Organisation gehören die öffentliche Protokolle erlaubt

    Keine Authentifizierung erforderlich.
    """
    template_name = "pages/public/protocols/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get body from URL slug
        body_slug = kwargs.get("body_slug")
        body = get_object_or_404(OParlBody, slug=body_slug) if body_slug else get_active_body(self.request)

        if not body:
            context["protocols"] = []
            context["body"] = None
            return context

        context["body"] = body

        # Find organizations linked to this body that allow public protocols
        from apps.tenants.models import Organization

        organizations = Organization.objects.filter(
            body=body,
            is_active=True,
        )

        # Get approved protocols from these organizations
        from apps.work.faction.models import FactionMeeting

        protocols = FactionMeeting.objects.filter(
            organization__in=organizations,
            protocol_status="approved",
            status="completed",
        ).select_related(
            "organization"
        ).order_by("-start")

        # Filter and search
        search = self.request.GET.get("q", "").strip()
        if search:
            protocols = protocols.filter(
                Q(title__icontains=search) |
                Q(description__icontains=search)
            )

        # Year filter
        year = self.request.GET.get("year")
        if year:
            protocols = protocols.filter(start__year=year)

        # Pagination
        paginator = Paginator(protocols, 20)
        page = self.request.GET.get("page", 1)
        context["protocols"] = paginator.get_page(page)

        # Available years for filter
        years = FactionMeeting.objects.filter(
            organization__in=organizations,
            protocol_status="approved",
        ).dates("start", "year", order="DESC")
        context["available_years"] = [d.year for d in years]

        context["search_query"] = search
        context["selected_year"] = year

        return context


class PublicProtocolDetailView(TemplateView):
    """
    Öffentliche Detailansicht eines genehmigten Fraktionsprotokolls.

    Zeigt nur öffentliche TOPs (visibility='public').
    Keine Authentifizierung erforderlich.
    """
    template_name = "pages/public/protocols/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        from apps.work.faction.models import FactionMeeting, FactionAgendaItem

        # Get the meeting
        meeting_id = kwargs.get("meeting_id")
        meeting = get_object_or_404(
            FactionMeeting,
            id=meeting_id,
            protocol_status="approved",  # Only approved protocols
            status="completed",
        )

        context["meeting"] = meeting
        context["organization"] = meeting.organization
        context["body"] = meeting.organization.body if meeting.organization else None

        # Only show public agenda items
        agenda_items = meeting.agenda_items.filter(
            visibility="public",
            proposal_status="active",  # Only accepted items
        ).order_by("order", "number")

        context["agenda_items"] = agenda_items

        # Get public protocol entries (only for public items)
        public_item_ids = agenda_items.values_list("id", flat=True)
        protocol_entries = meeting.protocol_entries.filter(
            Q(agenda_item__isnull=True) | Q(agenda_item_id__in=public_item_ids)
        ).select_related(
            "agenda_item", "speaker__user"
        ).order_by("order", "created_at")

        context["protocol_entries"] = protocol_entries

        # Previous/Next navigation
        context["previous_meeting"] = FactionMeeting.objects.filter(
            organization=meeting.organization,
            protocol_status="approved",
            start__lt=meeting.start,
        ).order_by("-start").first()

        context["next_meeting"] = FactionMeeting.objects.filter(
            organization=meeting.organization,
            protocol_status="approved",
            start__gt=meeting.start,
        ).order_by("start").first()

        return context
