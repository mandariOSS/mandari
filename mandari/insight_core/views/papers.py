# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.conf import settings
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.generic import DetailView, ListView

from apps.common.mixins import HTMXMixin

from ..models import (
    OParlAgendaItem,
    OParlMeeting,
    OParlPaper,
    withdrawn_q,
)
from ..services import file_reconcile
from ._helpers import ActiveBodyRequiredMixin, get_active_body
from ._withdrawn import withdrawn_response

# =============================================================================
# Vorgänge (Papers)
# =============================================================================


class PaperListView(HTMXMixin, ActiveBodyRequiredMixin, ListView):
    """Liste aller Vorgänge."""

    model = OParlPaper
    template_name = "pages/papers/list.html"
    context_object_name = "papers"
    paginate_by = 25

    def get_template_names(self):
        # Für HTMX-Requests nur das Partial zurückgeben
        if self.is_htmx:
            return ["partials/paper_list_items.html"]
        return [self.template_name]

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlPaper.objects.none()

        qs = OParlPaper.objects.filter(body=body, deleted=False)

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
                OParlPaper.objects.filter(body=body, deleted=False)
                .exclude(paper_type__isnull=True)
                .values_list("paper_type", flat=True)
                .distinct()
                .order_by("paper_type")
            )

        from ..seo import get_page_seo

        _body = get_active_body(self.request)
        context["seo"] = get_page_seo(
            self.request,
            title="Vorgänge & Beschlüsse",
            description="Anträge, Vorlagen und Beschlüsse der Kommunalpolitik durchsuchen und nachvollziehen.",
            body=_body,
        ).to_dict()
        return context


class PaperDetailView(DetailView):
    """Detailseite eines Vorgangs."""

    model = OParlPaper
    template_name = "pages/papers/detail.html"
    context_object_name = "paper"

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.withdrawn_by_publisher:
            return withdrawn_response(request, self.object)
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paper = self.object

        # Alle Dateien – ohne zurückgenommene Anlagen aus Session und ohne Dokumente, die die Kommune
        # entfernt hat oder die dort nicht mehr abrufbar sind (Löschabgleich, #787)
        files = [f for f in paper.files.all() if not f.withdrawn_by_publisher and not file_reconcile.is_blocked(f)]
        context["files"] = files

        # Beratungsverlauf (Consultations mit Meeting-Info), Stand-Satz und Zeitstrahl
        from ..services.paper_status import paper_status, timeline

        consultations = self._get_consultations_with_meetings(paper)
        now = timezone.now()
        status = paper_status(consultations, now)
        context["paper_status"] = status
        context["consultations"] = timeline(consultations, status, now)
        # Gremium der Bezugsberatung als Angabe in der Dokumentansicht
        reference = status.upcoming or status.last
        context["viewer_committee"] = reference.get("organization_name") if reference else ""
        # Seite des Vorgangs im Ratsinformationssystem (OParl ``web``), nur als http(s)-Adresse
        web = (paper.raw_json or {}).get("web") if isinstance(paper.raw_json, dict) else None
        context["source_url"] = web if isinstance(web, str) and web.startswith(("https://", "http://")) else ""

        # Ortsbezüge (offizielle OParl-Locations + extrahierte) für Karte/Liste
        locations = paper.locations if isinstance(paper.locations, list) else []
        context["paper_locations"] = [
            loc
            for loc in locations
            if isinstance(loc, dict) and loc.get("lat") is not None and loc.get("lon") is not None
        ]
        # Amtliche Umringe von Bebauungsplänen mit Planseite und Quellenangabe (#598)
        from ..services.plan_boundaries import paper_map_data, paper_plan_context

        plan_areas = paper_plan_context(paper)
        context["plan_areas"] = plan_areas
        context["paper_map_data"] = paper_map_data(context["paper_locations"], plan_areas)

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
        # Von mandari Session zurückgenommene Beratungen, Sitzungen und TOPs (z. B. nicht-öffentlich
        # gestellt) erscheinen nicht – wie in der Session-OParl-API, die solche Verweise auslässt.
        consultations = list(paper.consultations.exclude(withdrawn_q()))
        if not consultations:
            return []

        # Sammle alle meeting_external_ids und agenda_item_external_ids
        meeting_ids = [c.meeting_external_id for c in consultations if c.meeting_external_id]
        agenda_item_ids = [c.agenda_item_external_id for c in consultations if c.agenda_item_external_id]

        # Batch-Lookup für Meetings
        meetings_by_id = {}
        if meeting_ids:
            meetings = (
                OParlMeeting.objects.filter(external_id__in=meeting_ids)
                .exclude(withdrawn_q())
                .prefetch_related("organizations")
            )
            meetings_by_id = {m.external_id: m for m in meetings}

        # Batch-Lookup für AgendaItems
        agenda_items_by_id = {}
        if agenda_item_ids:
            agenda_items = (
                OParlAgendaItem.objects.filter(external_id__in=agenda_item_ids)
                .exclude(withdrawn_q())
                .exclude(withdrawn_q("meeting"))
            )
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
                    "organization_count": _organization_count(meeting) if meeting else None,
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


def _organization_count(meeting) -> int | None:
    """Anzahl der Gremien hinter ``OParlMeeting.get_display_name`` (Namen mit Komma verbunden).

    Aus den vorgeladenen Gremien und ohne weitere Abfrage; ``None``, wenn sie so nicht feststeht. Der
    Stand-Satz beugt danach auch Gremiennamen mit Komma („im Ausschuss für Planung, Bau und Umwelt“).
    """
    named = [org for org in list(meeting.organizations.all())[:2] if org.name]
    if named:
        return len(named)
    urls = meeting.raw_json.get("organization") if isinstance(meeting.raw_json, dict) else None
    return 1 if isinstance(urls, list) and len(urls) == 1 else None


NO_TEXT_MESSAGE = "Zu diesem Vorgang liegen keine auswertbaren Dokumenttexte vor."
RETRY_MESSAGE = "Die Zusammenfassung konnte gerade nicht erstellt werden. Bitte versuche es später erneut."


def _summary_response(request, context, status=200):
    """Teilansicht der Zusammenfassung; HTMX tauscht nur 2xx ein, deshalb dort immer 200."""
    response = render(request, "partials/paper_summary.html", context)
    if status != 200 and not request.htmx:
        response.status_code = status
    return response


@require_http_methods(["GET", "POST"])
def paper_summary(request, pk):
    """
    HTMX-Endpunkt für die KI-Zusammenfassung eines Vorgangs.

    GET liefert die gespeicherte Zusammenfassung oder das Angebot, eine zu erstellen – ohne
    KI-Aufruf. Erstellt wird nur per POST (Klick, CSRF-geschützt) und in den Grenzen aus
    ``summary_guard``: eine Erstellung je Vorgang gleichzeitig, je IP-Adresse wenige pro Stunde
    und Tag, insgesamt ein Tagesbudget.
    """
    from ..services import summary_guard

    paper = get_object_or_404(OParlPaper, pk=pk)
    if paper.withdrawn_by_publisher:
        return withdrawn_response(request, paper)

    if paper.summary:
        return _summary_response(request, {"paper": paper, "summary": paper.summary})
    if summary_guard.has_no_text(paper.pk):
        return _summary_response(request, {"paper": paper, "error": NO_TEXT_MESSAGE, "retry": False})
    if request.method != "POST":
        return _summary_response(request, {"paper": paper, "offer": True})

    if not summary_guard.acquire(paper.pk):
        return _summary_response(
            request,
            {
                "paper": paper,
                "error": "Die Zusammenfassung wird gerade erstellt. Bitte lade die Seite in einer Minute neu.",
            },
            status=409,
        )
    try:
        if summary_guard.budget_exceeded(request):
            return _summary_response(
                request,
                {
                    "paper": paper,
                    "error": "Gerade werden sehr viele Zusammenfassungen erstellt. Bitte versuche es später erneut.",
                },
                status=429,
            )
        return _generate_summary(request, paper)
    finally:
        summary_guard.release(paper.pk)


def _generate_summary(request, paper):
    from insight_ai.services.summarizer import (
        APINotConfiguredError,
        NoTextContentError,
        SummaryError,
        SummaryService,
    )

    from ..services import summary_guard

    retry = True
    try:
        # Dokumente nachladen nur mit Höchstwartezeit auf die Drossel je Host (Web-Anfrage)
        pace_max_wait = float(getattr(settings, "FILE_PROXY_PACE_MAX_WAIT_SECONDS", 5))
        summary = SummaryService(pace_max_wait=pace_max_wait).generate_summary(paper)
        return _summary_response(request, {"paper": paper, "summary": summary})
    except NoTextContentError:
        summary_guard.remember_no_text(paper.pk)
        error, retry = NO_TEXT_MESSAGE, False
    except APINotConfiguredError:
        error = "Die KI-Zusammenfassung ist derzeit nicht verfügbar."
    except SummaryError:
        error = RETRY_MESSAGE
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Unerwarteter Fehler bei der Zusammenfassung von Vorgang %s", paper.pk)
        error = RETRY_MESSAGE
    # Nie Ausnahmetexte ausgeben: Sie können Details des KI-Dienstes oder der Quelle enthalten
    return _summary_response(request, {"paper": paper, "error": error, "retry": retry})
