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
from hub.ris import selectors as ris_selectors

from ..models import OParlPaper
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

        # Dieselbe Kommune wie oben, ohne zweite Abfrage (Performance-Budget der Vorgangsliste)
        context["seo"] = get_page_seo(
            self.request,
            title="Vorgänge & Beschlüsse",
            description="Anträge, Vorlagen und Beschlüsse der Kommunalpolitik durchsuchen und nachvollziehen.",
            body=body,
        ).to_dict()
        # Stand je Vorgang aus dem Beratungsverlauf (Spalte „Stand“ auf breiten Bildschirmen, Issue #841)
        from ..services.search_presentation import statuses_for_papers

        papers = list(context["papers"])
        staende = statuses_for_papers(str(p.pk) for p in papers)
        for paper in papers:
            paper.stand = staende.get(str(paper.pk))
        context["papers"] = papers
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

        # Beratungsverlauf (Consultations mit Meeting-Info), Stand-Satz und Zeitstrahl; der Verlauf kommt aus der
        # Lese-Fassade wie auf der Vorgangsseite von Work (Issue #853)
        from ..services.paper_status import paper_status, timeline

        consultations = ris_selectors.consultation_history(paper)
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

        context["seo"] = get_paper_seo(paper, self.request, stand=status).to_dict()

        return context


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
