"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from django.views.generic import DetailView, ListView, TemplateView

from ..models import (
    OParlBody,
    OParlConsultation,
    OParlMeeting,
)
from ._helpers import get_active_body

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
            qs = qs.filter(Q(name__icontains=q) | Q(location_name__icontains=q))

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

        # Tagesordnungspunkte mit batch-loaded Papers (vermeidet N+1 Queries)
        agenda_items = list(meeting.agenda_items.all())
        # Natural sort: 1, 2, 10 instead of 1, 10, 2
        import re

        agenda_items.sort(
            key=lambda x: [
                (0, int(p)) if p.isdigit() else (1, p.lower()) for p in re.split(r"(\d+)", x.number or "999") if p
            ]
        )
        if agenda_items:
            ext_ids = [item.external_id for item in agenda_items]
            # Alle Consultations + Papers in 1 Query laden
            consultations = OParlConsultation.objects.filter(agenda_item_external_id__in=ext_ids).select_related(
                "paper"
            )
            # Papers pro AgendaItem zuordnen
            papers_by_agenda = {}
            for c in consultations:
                if c.paper:
                    papers_by_agenda.setdefault(c.agenda_item_external_id, []).append(c.paper)
            # An jedes AgendaItem anhängen
            for item in agenda_items:
                item._prefetched_papers = papers_by_agenda.get(item.external_id, [])
        context["agenda_items"] = agenda_items

        # Location Koordinaten für Karte (body kann fehlen bei verwaisten Meetings)
        try:
            meeting_body = meeting.body
        except OParlBody.DoesNotExist:
            meeting_body = None
        if meeting.location_name and meeting_body:
            from ..models import LocationMapping

            coords = LocationMapping.get_coordinates_for_location(meeting_body, meeting.location_name)
            context["location_coordinates"] = coords

        # SEO-Kontext
        try:
            from ..seo import get_meeting_seo

            context["seo"] = get_meeting_seo(meeting, self.request).to_dict()
        except (OParlBody.DoesNotExist, Exception):
            context["seo"] = {}

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

        events.append(
            {
                "id": str(meeting.id),
                "title": title,
                "start": meeting.start.isoformat() if meeting.start else None,
                "end": meeting.end.isoformat() if meeting.end else None,
                "url": f"/insight/termine/{meeting.id}/",
                "extendedProps": {
                    "location": meeting.location_name,
                    "fullTitle": full_title,
                },
            }
        )

    return JsonResponse(events, safe=False)
