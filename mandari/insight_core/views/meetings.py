# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from django.views.generic import DetailView, ListView, TemplateView

from ..models import (
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlOrganization,
)
from ._helpers import ActiveBodyRequiredMixin, get_active_body

# =============================================================================
# Termine (Meetings)
# =============================================================================


class MeetingListView(ActiveBodyRequiredMixin, ListView):
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from ..seo import get_page_seo

        context["seo"] = get_page_seo(
            self.request,
            title="Sitzungen & Termine",
            description="Aktuelle und vergangene Sitzungen der kommunalen Gremien mit Tagesordnungen und Dokumenten.",
            body=get_active_body(self.request),
        ).to_dict()
        return context

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlMeeting.objects.none()

        qs = OParlMeeting.objects.filter(body=body, deleted=False).prefetch_related("organizations")

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


MONTH_NAMES = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]


def _select_body_from_query(request):
    """?kommune=<uuid> wählt die Kommune (Deep-Link, z. B. aus dem Session-RIS)."""
    body_id = request.GET.get("kommune")
    if not body_id:
        return
    try:
        body = OParlBody.objects.get(id=body_id, deleted=False)
    except (OParlBody.DoesNotExist, ValueError, ValidationError):
        return
    request.session["active_body_id"] = str(body.id)
    request.session.modified = True


def _organizations_with_meetings(body):
    """Gremien der Kommune, die Sitzungen haben (für Abo-Auswahl und Jahresplan)."""
    from django.db.models import Count

    return (
        OParlOrganization.objects.filter(body=body, deleted=False)
        .annotate(meeting_count=Count("meetings", filter=Q(meetings__deleted=False)))
        .filter(meeting_count__gt=0)
        .order_by("name")
    )


class MeetingCalendarView(ActiveBodyRequiredMixin, TemplateView):
    """Kalenderansicht der Sitzungen."""

    template_name = "pages/meetings/calendar.html"

    def dispatch(self, request, *args, **kwargs):
        _select_body_from_query(request)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from ..seo import get_page_seo

        body = get_active_body(self.request)
        context["feed_organizations"] = _organizations_with_meetings(body)[:200] if body else []
        context["seo"] = get_page_seo(
            self.request,
            title="Sitzungskalender",
            description="Alle Sitzungen der kommunalen Gremien im Kalender: Monats- und Wochenansicht mit Details.",
            body=body,
        ).to_dict()
        return context


class MeetingYearPlanView(ActiveBodyRequiredMixin, TemplateView):
    """
    Öffentlicher Sitzungsplan (Issue #82): Jahresübersicht Gremium × Monat.

    Zeigt alle öffentlichen Sitzungen eines Jahres, wie sie das RIS bzw. das
    Session-Modul veröffentlicht — druckbar und je Gremium abonnierbar.
    """

    template_name = "pages/meetings/year_plan.html"

    def dispatch(self, request, *args, **kwargs):
        _select_body_from_query(request)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        today = timezone.localdate()
        try:
            year = int(self.request.GET.get("year", today.year))
            if not 2000 <= year <= 2100:
                raise ValueError
        except (TypeError, ValueError):
            year = today.year

        meetings = (
            OParlMeeting.objects.filter(body=body, deleted=False, start__year=year)
            .prefetch_related("organizations")
            .order_by("start")
        )
        rows_by_org: dict = {}
        for meeting in meetings:
            month_index = timezone.localtime(meeting.start).month - 1
            orgs = [org for org in meeting.organizations.all() if not org.deleted] or [None]
            for org in orgs:
                key = org.id if org else None
                row = rows_by_org.setdefault(
                    key, {"organization": org, "months": [[] for _ in range(12)], "count": 0, "cancelled": 0}
                )
                row["months"][month_index].append(meeting)
                row["count"] += 1
                if meeting.cancelled:
                    row["cancelled"] += 1
        rows = sorted(
            rows_by_org.values(),
            key=lambda r: (
                r["organization"] is None,
                (r["organization"].get_display_name() if r["organization"] else ""),
            ),
        )

        context.update(
            {
                "year": year,
                "rows": rows,
                "month_names": MONTH_NAMES,
                "total_meetings": meetings.count(),
                "cancelled_meetings": meetings.filter(cancelled=True).count(),
            }
        )
        from ..seo import get_page_seo

        context["seo"] = get_page_seo(
            self.request,
            title=f"Sitzungsplan {year}",
            description=f"Jahresübersicht aller öffentlichen Sitzungen {year} nach Gremium – druckbar und als Kalender abonnierbar.",
            body=body,
            keywords=["Sitzungsplan", "Sitzungskalender", "Gremien", "Termine"],
        ).to_dict()
        return context


@require_GET
def calendar_feed(request):
    """
    Abonnierbarer ICS-Feed der öffentlichen Sitzungen (Issue #82):
    ganze Kommune oder ein Gremium (?gremium=<uuid>), 3 Monate zurück, 13 Monate voraus.
    Kein Login — die Daten sind ohnehin öffentlich.
    """
    from datetime import timedelta

    from apps.common.ical import build_ics_feed

    _select_body_from_query(request)
    body = get_active_body(request)
    if not body:
        raise Http404("Keine Kommune gewählt")

    organization = None
    org_id = request.GET.get("gremium")
    if org_id:
        try:
            organization = OParlOrganization.objects.get(id=org_id, body=body, deleted=False)
        except (OParlOrganization.DoesNotExist, ValueError, ValidationError):
            raise Http404("Gremium nicht gefunden")

    now = timezone.now()
    qs = OParlMeeting.objects.filter(
        body=body, deleted=False, start__gte=now - timedelta(days=90), start__lte=now + timedelta(days=400)
    ).order_by("start")
    if organization is not None:
        qs = qs.filter(organizations=organization)

    site_url = getattr(settings, "SITE_URL", "https://mandari.de").rstrip("/")
    events = [
        {
            "uid": f"meeting-{meeting.id}@mandari.de",
            "summary": meeting.get_display_name() + (" (abgesagt)" if meeting.cancelled else ""),
            "start": meeting.start,
            "end": meeting.end,
            "location": meeting.location_name or "",
            "description": f"{site_url}/insight/termine/{meeting.id}/",
            "status": "CANCELLED" if meeting.cancelled else "CONFIRMED",
        }
        for meeting in qs
        if meeting.start
    ]
    name = f"{body.get_display_name()} – {organization.get_display_name() if organization else 'Sitzungen'}"
    response = HttpResponse(build_ics_feed(events, name=name), content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = 'inline; filename="sitzungen.ics"'
    response["Cache-Control"] = "public, max-age=3600"
    return response


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
        # Abstimmungsergebnisse aus dem Quell-RIS (Issue #41): Summen + namentliche Stimmen
        for item in agenda_items:
            raw = item.raw_json or {}
            item.vote_info = raw.get("mandari:vote") if isinstance(raw.get("mandari:vote"), dict) else None
            roll_call = raw.get("mandari:rollCall")
            item.roll_call = roll_call if isinstance(roll_call, list) and roll_call else None
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

        # SEO-Kontext (verwaiste Meetings ohne Body dürfen die Seite nicht crashen)
        try:
            from ..seo import get_meeting_seo

            context["seo"] = get_meeting_seo(meeting, self.request).to_dict()
        except Exception:
            import logging

            logging.getLogger(__name__).warning("SEO-Kontext für Meeting %s fehlgeschlagen", meeting.pk, exc_info=True)
            context["seo"] = {}

        return context


class MeetingListPartial(ListView):
    """HTMX Partial für Sitzungen-Liste."""

    model = OParlMeeting
    template_name = "partials/meeting_list_items.html"
    context_object_name = "meetings"
    paginate_by = 20

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlMeeting.objects.none()
        return (
            OParlMeeting.objects.filter(body=body, deleted=False).prefetch_related("organizations").order_by("-start")
        )


@require_GET
def calendar_events(request):
    """JSON-Endpoint für Kalender-Events (FullCalendar/Alpine.js)."""
    body = get_active_body(request)
    if not body:
        return JsonResponse([], safe=False)

    # Zeitraum aus Request (FullCalendar sendet start/end)
    start_str = request.GET.get("start")
    end_str = request.GET.get("end")

    qs = OParlMeeting.objects.filter(body=body, cancelled=False, deleted=False).prefetch_related("organizations")

    # Optional: nur ein Gremium (Issue #82)
    org_id = request.GET.get("gremium")
    if org_id:
        try:
            qs = qs.filter(organizations__id=org_id)
        except (ValueError, ValidationError):
            pass

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
