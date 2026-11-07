# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.generic import DetailView, ListView

from apps.common.mixins import HTMXMixin
from hub.ris import selectors as ris_selectors
from hub.ris import vorlagennummer
from hub.ris.original import seite_im_ris

from ..models import OParlPaper, VorgangUrheber
from ..services import file_reconcile, fraktionskuerzel
from ..services.vorgang_dokumente import dokumente_anhaengen
from ..services.vorgang_urheber import (
    FraktionsAuswahl,
    bezeichnungen_fuer,
    fraktionen_der_kommune,
    urheber_je_vorgang,
)
from ._helpers import ActiveBodyRequiredMixin, BegrenzteSeitenMixin, FilterInAdresseMixin, get_active_body
from ._withdrawn import withdrawn_response

# =============================================================================
# Vorgänge (Papers)
# =============================================================================


class PaperListView(HTMXMixin, FilterInAdresseMixin, ActiveBodyRequiredMixin, BegrenzteSeitenMixin, ListView):
    """Liste aller Vorgänge."""

    model = OParlPaper
    template_name = "pages/papers/list.html"
    context_object_name = "papers"
    paginate_by = 25
    teil_vorlage = "partials/paper_list_items.html"

    def get(self, request, *args, **kwargs):
        ziel = self._einziger_nummerntreffer()
        if ziel:
            return HttpResponseRedirect(ziel)
        return super().get(request, *args, **kwargs)

    def _einziger_nummerntreffer(self) -> str:
        """
        Adresse des Vorgangs, wenn die Eingabetaste (``direkt=1`` aus dem Formular) eine Vorlagennummer mit genau einem
        Treffer abschickt, der genau passt (Issue #1102); sonst leer. HTMX-Antworten ersetzen nur die Liste und leiten
        nie weiter.
        """
        if self.is_htmx or self.request.GET.get("direkt") != "1" or self.request.GET.get("page", "1") not in ("", "1"):
            return ""
        if not vorlagennummer.sieht_wie_nummer_aus(self.request.GET.get("q", "")):
            return ""
        treffer = list(self.get_queryset()[:2])
        if len(treffer) != 1 or not vorlagennummer.genau(getattr(treffer[0], "nummer_rang", None)):
            return ""
        return reverse("insight_core:insight:paper_detail", kwargs={"pk": treffer[0].pk})

    def _fraktionen(self, body) -> list[FraktionsAuswahl]:
        """Fraktionen des Filters, einmal je Anfrage berechnet (Liste und Auswahl brauchen sie)."""
        if getattr(self, "_fraktionen_cache", None) is None:
            self._fraktionen_cache = fraktionen_der_kommune(body.pk)
        return self._fraktionen_cache

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlPaper.objects.none()

        qs = OParlPaper.objects.filter(body=body, deleted=False)

        # Typ-Filter
        paper_type = self.request.GET.get("type", "").strip()
        if paper_type:
            qs = qs.filter(paper_type=paper_type)

        # Antragstellende Fraktion (Issue #1138), mit früheren Namen derselben Fraktion (Issue #1258)
        fraktion = self.request.GET.get("fraktion", "").strip()
        if fraktion:
            bezeichnungen = bezeichnungen_fuer(self._fraktionen(body), fraktion)
            qs = qs.filter(
                pk__in=VorgangUrheber.objects.filter(body_id=body.pk, bezeichnung__in=bezeichnungen).values("paper_id")
            )

        # Suche in Name und Nummer, Vorlagennummern in jeder Schreibweise mit dem passenden Vorgang vorn (Issue #1102)
        return ris_selectors.search_papers_in(qs, self.request.GET.get("q", ""))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        # Leerzustand bei einer Vorlagennummer: „Die Vorlage … liegt uns noch nicht vor.“ (nur ohne weiteren Filter)
        q = self.request.GET.get("q", "").strip()
        if (
            vorlagennummer.sieht_wie_nummer_aus(q)
            and not self.request.GET.get("type", "").strip()
            and not self.request.GET.get("fraktion", "").strip()
        ):
            context["nummer_gesucht"] = q

        if body:
            # Verfügbare Typen für Filter
            context["paper_types"] = (
                OParlPaper.objects.filter(body=body, deleted=False)
                .exclude(paper_type__isnull=True)
                .values_list("paper_type", flat=True)
                .distinct()
                .order_by("paper_type")
            )
            # Fraktionen der laufenden Wahlperiode, die in der Kommune Vorgänge gestellt haben (Filter, Issues #1138,
            # #1258)
            context["paper_fraktionen"] = self._fraktionen(body)

        from ..seo import get_list_seo

        # Dieselbe Kommune wie oben, ohne zweite Abfrage (Performance-Budget der Vorgangsliste); kanonisch ist die
        # Adresse je Kommune (Issue #939)
        context["seo"] = get_list_seo(
            self.request,
            "paper_list",
            title="Vorgänge & Beschlüsse",
            description=(
                f"Anträge, Vorlagen und Beschlüsse aus {body.get_display_name()} durchsuchen und nachvollziehen."
                if body
                else "Anträge, Vorlagen und Beschlüsse der Kommunalpolitik durchsuchen und nachvollziehen."
            ),
            body=body,
        ).to_dict()
        # Stand je Vorgang aus dem Beratungsverlauf (Spalte „Stand“ auf breiten Bildschirmen, Issue #841)
        from ..services.search_presentation import statuses_for_papers

        papers = list(context["papers"])
        staende = statuses_for_papers(str(p.pk) for p in papers)
        for paper in papers:
            paper.stand = staende.get(str(paper.pk))
        # Vorlage und Anlagen je Vorgang in einer Abfrage für die Seite (Issue #1103)
        dokumente_anhaengen(papers)
        # Titel ohne Absender und Fraktionskürzel (Issue #1272), eine Abfrage für die Seite
        fraktionskuerzel.anhaengen(papers)
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
        # entfernt hat oder die dort nicht mehr abrufbar sind (Löschabgleich, #787). Ohne Volltext geladen: Die
        # Dokumentzeile zeigt nur seinen Anfang, je Datei und Seite begrenzt (``with_text_start``)
        files = [
            f
            for f in paper.files.defer("text_content", "raw_json")
            if not f.withdrawn_by_publisher and not file_reconcile.is_blocked(f)
        ]
        context["files"] = ris_selectors.with_text_start(files)
        # Antragstellende Fraktionen (Issue #1138); Titel ohne Absender und Kürzel im Kopf (Issue #1272)
        context["urheber"] = urheber_je_vorgang([paper.pk]).get(paper.pk, [])
        context["absender"] = fraktionskuerzel.absender(paper.name, paper.paper_type, context["urheber"])

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
        # Seite des Vorgangs im Ratsinformationssystem (OParl ``web`` bzw. Detailseite der Scraper-Quelle, #1259)
        context["source_url"] = seite_im_ris(paper)

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

        # Zusammenfassung älter als der Beratungsstand (Issue #1257): Hinweis darüber, nicht in der Description
        from ..services import summary_store

        veraltet = summary_store.veraltet(paper, now=now)
        context["zusammenfassung_veraltet"] = veraltet

        # SEO-Kontext
        from ..seo import get_paper_seo

        context["seo"] = get_paper_seo(
            paper, self.request, stand=status, mit_zusammenfassung=not veraltet, original=context["source_url"]
        ).to_dict()

        return context


NO_TEXT_MESSAGE = "Zu diesem Vorgang liegen keine auswertbaren Dokumenttexte vor."
TEXT_PENDING_MESSAGE = "Die Texte der Dokumente werden gerade erkannt. Bitte versuche es in einigen Minuten erneut."
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
    und Tag, insgesamt ein Tagesbudget. Eine vorhandene Zusammenfassung ersetzt ein POST nur, wenn sie
    älter als der Beratungsstand ist (``summary_store.veraltet``, Issue #1257).
    """
    from ..services import summary_guard, summary_store

    paper = get_object_or_404(OParlPaper, pk=pk)
    if paper.withdrawn_by_publisher:
        return withdrawn_response(request, paper)

    if paper.summary:
        veraltet = summary_store.veraltet(paper)
        if not (veraltet and request.method == "POST"):
            return _summary_response(request, {"paper": paper, "summary": paper.summary, "veraltet": veraltet})
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


@require_http_methods(["POST"])
def paper_summary_bewertung(request, pk):
    """
    Daumen hoch/runter zur KI-Zusammenfassung (Issue #1221, ``services.ki_bewertung``): POST mit ``wert``
    (``hoch``/``runter``), ``fassung`` und optional ``zeichen`` einer früheren Bewertung desselben Browsers.
    Antwort JSON ``{"hilfreich": …, "zeichen": …}``; 409, wenn die Zusammenfassung inzwischen eine andere ist.
    """
    from django.http import JsonResponse

    from ..services import ki_bewertung

    paper = get_object_or_404(OParlPaper, pk=pk)
    if paper.withdrawn_by_publisher or not paper.summary:
        return JsonResponse({"fehler": "keine_zusammenfassung"}, status=404)
    wert = request.POST.get("wert")
    if wert not in ("hoch", "runter"):
        return JsonResponse({"fehler": "wert"}, status=400)
    try:
        bewertung, zeichen = ki_bewertung.abgeben(
            request,
            paper,
            hilfreich=wert == "hoch",
            bewertete_fassung=str(request.POST.get("fassung", ""))[:32],
            zeichen=str(request.POST.get("zeichen", ""))[:500],
        )
    except ki_bewertung.FassungVeraltetError:
        return JsonResponse({"fehler": "fassung"}, status=409)
    except ki_bewertung.ZuVieleBewertungenError:
        return JsonResponse({"fehler": "grenze"}, status=429)
    return JsonResponse({"hilfreich": bewertung.hilfreich, "zeichen": zeichen})


def _generate_summary(request, paper):
    from insight_ai.services.summarizer import (
        APINotConfiguredError,
        BudgetExhaustedError,
        NoTextContentError,
        SummaryError,
        SummaryService,
        TextPendingError,
    )

    from ..services import summary_guard

    retry = True
    try:
        # Liest nur gespeicherten Text; Dokumente lädt und erkennt die Zusammenfassung nicht (Issue #919)
        summary = SummaryService().generate_summary(paper)
        return _summary_response(request, {"paper": paper, "summary": summary})
    except TextPendingError:
        # Kein „kein Text“ merken: Die Erkennung steht aus (ggf. mit Vorrang eingeplant), der Text kommt noch
        error = TEXT_PENDING_MESSAGE
    except NoTextContentError:
        summary_guard.remember_no_text(paper.pk)
        error, retry = NO_TEXT_MESSAGE, False
    except APINotConfiguredError:
        error = "Die KI-Zusammenfassung ist derzeit nicht verfügbar."
    except BudgetExhaustedError:
        # Monatsbudget der Claude API ausgeschöpft: bis Monatsende kein Einzelaufruf, erneuter Versuch zwecklos
        error, retry = "Die KI-Zusammenfassung ist derzeit nicht verfügbar.", False
    except SummaryError:
        error = RETRY_MESSAGE
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Unerwarteter Fehler bei der Zusammenfassung von Vorgang %s", paper.pk)
        error = RETRY_MESSAGE
    # Nie Ausnahmetexte ausgeben: Sie können Details des KI-Dienstes oder der Quelle enthalten
    return _summary_response(request, {"paper": paper, "error": error, "retry": retry})
