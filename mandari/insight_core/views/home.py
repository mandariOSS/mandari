# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

import json
from datetime import timedelta

from django.db.models import OuterRef, Q, Subquery
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from ..models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    withdrawn_q,
)
from ..services import indexierung, kommunenverzeichnis
from ..services.paper_status import ENTSCHEIDUNG, KEINE_ENTSCHEIDUNG, ist_beschluss
from ._helpers import get_active_body, is_all_bodies_mode

# =============================================================================
# Portal Homepage (RIS)
# =============================================================================

#: Dritte Liste der Übersicht: so viele Ergebnisse, aus Sitzungen höchstens so viele Tage zurück
BESCHLUESSE_ANZAHL = 4
BESCHLUESSE_TAGE = 180


def zuletzt_beschlossen(body: OParlBody, anzahl: int = BESCHLUESSE_ANZAHL) -> list[OParlAgendaItem]:
    """
    Jüngste Beschlüsse öffentlicher Tagesordnungspunkte vergangener Sitzungen (Issue #841).

    Nur Punkte mit einem Ergebnis, das eine Entscheidung ist – „vertagt“, „zur Kenntnis genommen“ und ähnliche
    (``paper_status.ist_beschluss``) bleiben außen vor –, aus nicht abgesagten, nicht zurückgenommenen Sitzungen der
    letzten Monate. Zwei Abfragen: die Punkte (Gremium als Unterabfrage über die indizierte Zuordnung Sitzung–Gremium)
    und danach die Beratungen dieser Punkte (``agenda_item_external_id__in``, wie die Sitzungsseite) für den Vorgang.
    Jeder Punkt trägt ``vorgang`` (beratener Vorgang oder ``None``) und ``gremium``.
    """
    jetzt = timezone.now()
    gremien = OParlOrganization.objects.filter(meetings=OuterRef("meeting_id")).order_by("name")
    # Kenntnisnahmen, Antworten und Vertagungen ohne Entscheidungswort schon in der Abfrage aussortieren: Auch nach
    # vielen Kenntnisnahmen erscheinen so bis zu ``anzahl`` Beschlüsse (Feinprüfung danach mit ist_beschluss)
    keine = Q()
    for wort in (*KEINE_ENTSCHEIDUNG, "vertagt", "zurückgestellt", "abgesetzt", "verschoben"):
        keine |= Q(result__icontains=wort)
    entscheidung = Q()
    for wort in ENTSCHEIDUNG:
        entscheidung |= Q(result__icontains=wort)
    ohne_entscheidung = keine & ~entscheidung
    kandidaten = (
        OParlAgendaItem.objects.filter(
            meeting__body=body,
            meeting__deleted=False,
            meeting__cancelled=False,
            meeting__start__lt=jetzt,
            meeting__start__gte=jetzt - timedelta(days=BESCHLUESSE_TAGE),
            public=True,
            deleted=False,
        )
        .exclude(result__isnull=True)
        .exclude(result="")
        .exclude(ohne_entscheidung)
        .exclude(withdrawn_q())
        .exclude(withdrawn_q("meeting"))
        .annotate(gremium=Subquery(gremien.values("name")[:1]))
        .select_related("meeting")
        .order_by("-meeting__start", "order", "number")
    )
    # Etwas mehr laden, als gezeigt wird: seltene Schreibweisen fallen erst bei der Feinprüfung heraus
    punkte = [punkt for punkt in kandidaten[: anzahl * 8] if ist_beschluss(punkt.result)][:anzahl]
    vorgaenge: dict[str, OParlPaper] = {}
    if punkte:
        beratungen = (
            OParlConsultation.objects.filter(
                agenda_item_external_id__in=[p.external_id for p in punkte], paper__isnull=False, paper__deleted=False
            )
            .exclude(withdrawn_q())
            .exclude(withdrawn_q("paper"))
            .select_related("paper")
            .order_by("pk")
        )
        for beratung in beratungen:
            vorgaenge.setdefault(str(beratung.agenda_item_external_id), beratung.paper)
    for punkt in punkte:
        punkt.vorgang = vorgaenge.get(punkt.external_id)
    return punkte


class PortalHomeView(TemplateView):
    """Portal-Startseite: Kommunenauswahl bzw. Übersicht der Kommune mit Sitzungen, Vorgängen und Beschlüssen."""

    template_name = "pages/portal/home.html"
    select_template_name = "pages/portal/select_body.html"

    def get(self, request, *args, **kwargs):
        # Self-Hosting-Fall: Existiert genau eine Kommune, wird sie automatisch
        # gewählt — kein Auswahlzwang beim ersten Aufruf.
        if is_all_bodies_mode(request):
            bodies = OParlBody.objects.listed()
            if bodies.count() == 1:
                only_body = bodies.first()
                request.session["active_body_id"] = str(only_body.id)
                request.session.modified = True
                return redirect("insight_core:insight:portal_home")
        return super().get(request, *args, **kwargs)

    def get_template_names(self):
        if is_all_bodies_mode(self.request):
            return [self.select_template_name]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Wichtig: all_bodies_mode VOR get_active_body prüfen — der Fallback in
        # get_active_body würde sonst beim Erstbesuch still die erste Kommune
        # in die Session schreiben und die Auswahlseite überspringen.
        all_bodies_mode = is_all_bodies_mode(self.request)
        body = None if all_bodies_mode else get_active_body(self.request)

        context["all_bodies_mode"] = all_bodies_mode

        if all_bodies_mode:
            # Kommunenauswahl (Issue #783): keine Liste aller Kommunen, sondern Suche, Nähe und Stöbern über das
            # Kommunenverzeichnis (services/kommunenverzeichnis.py); hier nur, ob es überhaupt Kommunen gibt
            context["hat_kommunen"] = OParlBody.objects.listed().exists()
            if context["hat_kommunen"]:
                # Erste Stufe des Stöberns (Länder) gleich im Markup: Sie hält ihren Platz, statt nach dem Laden
                # alles darunter zu verschieben (CLS), und spart die erste Anfrage
                context["stoebern_start"] = kommunenverzeichnis.stoebern()
                # Direkte Links auf die Stadtseiten (Issue #914): ohne JavaScript und ohne Sitzung erreichbar,
                # so finden auch Suchmaschinen die Kommunen; höchstens zwölf, keine lange Liste
                context["stadtseiten"] = indexierung.stadtseiten()
            context["upcoming_meetings"] = None
            context["recent_papers"] = None

        elif body:
            # Nächste Sitzungen (5 für einheitliche Listen)
            context["upcoming_meetings"] = (
                OParlMeeting.objects.filter(body=body, start__gte=timezone.now(), cancelled=False, deleted=False)
                .prefetch_related("organizations")
                .order_by("start")[:5]
            )

            # Neueste Vorgänge
            context["recent_papers"] = OParlPaper.objects.filter(body=body, deleted=False).order_by(
                "-date", "-oparl_created"
            )[:5]

            # Zuletzt beschlossen: dritte Spalte auf breiten Bildschirmen (Issue #841)
            context["recent_decisions"] = zuletzt_beschlossen(body)

            # Stadtteile für Nachbarschafts-Schnellwahl
            import os

            data_path = os.path.join(os.path.dirname(__file__), "data", "stadtteile.json")
            if os.path.exists(data_path):
                with open(data_path, encoding="utf-8") as f:
                    all_districts = json.load(f)
                slug = body.slug or ""
                context["home_districts"] = all_districts.get(slug, [])

        # SEO-Kontext
        from ..seo import get_portal_home_seo

        context["seo"] = get_portal_home_seo(self.request, body if not all_bodies_mode else None).to_dict()

        return context


def set_body(request, body_id):
    """Setzt die aktive Kommune und leitet zur Portal-Homepage weiter."""
    from django.utils.http import url_has_allowed_host_and_scheme

    from .. import portal as portal_context

    aktuell = portal_context.get_portal(request)
    if aktuell is not None and not aktuell.can_leave:
        # Eigener Host einer Körperschaft (Issue #317): Die Kommune ist fest
        return redirect("insight_core:insight:portal_home")
    try:
        body = OParlBody.objects.get(id=body_id)
        if aktuell is not None and aktuell.body.pk != body.pk:
            # Andere Kommune gewählt: Einstieg der Körperschaft verlassen
            portal_context.leave(request)
        request.session["active_body_id"] = str(body.id)
        # Explicitly mark session as modified and save to ensure persistence
        request.session.modified = True
        request.session.save()
    except OParlBody.DoesNotExist:
        pass

    # SECURITY: Use Django's built-in URL validation to prevent Open Redirect
    default_redirect = "/insight/"
    referer = request.META.get("HTTP_REFERER", "")
    # Brotkrumen einer Seite aus einer anderen Kommune (Issue #783): erst diese Kommune wählen, dann auf ihre
    # Übersicht bzw. Liste. Nur relative Pfade des Bürgerportals.
    weiter = request.GET.get("weiter", "")

    if (
        weiter.startswith("/insight/")
        and not weiter.startswith("/insight/kommune/")
        and url_has_allowed_host_and_scheme(
            weiter,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        redirect_url = weiter
    elif referer and url_has_allowed_host_and_scheme(
        referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        redirect_url = referer
    else:
        redirect_url = default_redirect

    # For HTMX requests, use HX-Redirect header for reliable navigation
    if request.htmx:
        response = HttpResponse(status=200)
        response["HX-Redirect"] = redirect_url
        return response

    return redirect(redirect_url)


def clear_body(request):
    """Setzt auf 'Alle Kommunen' Modus und leitet zur Portal-Homepage weiter."""
    from .. import portal as portal_context

    aktuell = portal_context.get_portal(request)
    if aktuell is not None and not aktuell.can_leave:
        # Eigener Host einer Körperschaft (Issue #317): keine gemeinsame Auswahl
        return redirect("insight_core:insight:portal_home")
    # Verlässt auch den Einstieg einer Körperschaft (/insight/k/<slug>/)
    portal_context.leave(request)
    request.session["active_body_id"] = "all"
    # Explicitly mark session as modified and save to ensure persistence
    request.session.modified = True
    request.session.save()

    # For HTMX requests, use HX-Redirect header
    redirect_url = "/insight/"

    if request.htmx:
        response = HttpResponse(status=200)
        response["HX-Redirect"] = redirect_url
        return response

    return redirect(redirect_url)
