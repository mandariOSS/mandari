# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

import json
import re

from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from ..models import (
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    PublicQuestion,
)
from ._helpers import get_active_body, is_all_bodies_mode

# =============================================================================
# Portal Homepage (RIS)
# =============================================================================

# Bundesland aus den ersten beiden Stellen des Amtlichen Gemeindeschlüssels (AGS)
AGS_BUNDESLAND = {
    "01": "Schleswig-Holstein",
    "02": "Hamburg",
    "03": "Niedersachsen",
    "04": "Bremen",
    "05": "Nordrhein-Westfalen",
    "06": "Hessen",
    "07": "Rheinland-Pfalz",
    "08": "Baden-Württemberg",
    "09": "Bayern",
    "10": "Saarland",
    "11": "Berlin",
    "12": "Brandenburg",
    "13": "Mecklenburg-Vorpommern",
    "14": "Sachsen",
    "15": "Sachsen-Anhalt",
    "16": "Thüringen",
}


#: Ableitung des Körperschafts-Typs aus dem Namen, wenn die OParl-Quelle
#: keine ``classification`` liefert (Reihenfolge = Priorität).
_KIND_PATTERNS = (
    (re.compile(r"^(bezirksregierung|regionalrat|regionalverband|landschaftsverband)", re.I), "Regionalrat"),
    (re.compile(r"^(landkreis|kreis)(\s|$)", re.I), "Landkreis"),
    (re.compile(r"^(bundesstadt|landeshauptstadt|freie und hansestadt|hansestadt)", re.I), "Kreisfreie Stadt"),
    (re.compile(r"kreisfrei", re.I), "Kreisfreie Stadt"),
    (re.compile(r"^(samtgemeinde|verbandsgemeinde|amt)(\s|$)", re.I), "Gemeindeverband"),
    (re.compile(r"^(gemeinde|markt|flecken)(\s|$)", re.I), "Gemeinde"),
    (re.compile(r"^stadt(\s|$)", re.I), "Stadt"),
)


def get_kind_label_for_body(body):
    """Anzeige-Typ einer Körperschaft: OParl-``classification`` oder Ableitung
    aus dem Namen; letzter Fallback „Kommune"."""
    if body.classification:
        return body.classification
    name = body.name or ""
    for pattern, label in _KIND_PATTERNS:
        if pattern.search(name):
            return label
    return "Kommune"


def get_bundesland_for_body(body):
    """Leitet das Bundesland aus dem AGS der Kommune ab (oder None)."""
    if body.ags and len(body.ags) >= 2:
        return AGS_BUNDESLAND.get(body.ags[:2])
    return None


def _bodies_with_stats():
    """Alle Kommunen inkl. Kennzahlen (Vorgänge/Gremien/Sitzungen) und Region.

    Drei gruppierte Count-Queries statt Multi-Annotate (vermeidet Join-Explosion).
    """
    bodies = list(OParlBody.objects.filter(deleted=False).order_by("name"))

    def counts_by_body(model):
        qs = model.objects.filter(deleted=False).values("body").annotate(n=Count("id"))
        return {row["body"]: row["n"] for row in qs}

    paper_counts = counts_by_body(OParlPaper)
    org_counts = counts_by_body(OParlOrganization)
    meeting_counts = counts_by_body(OParlMeeting)

    for body in bodies:
        body.stat_papers = paper_counts.get(body.id, 0)
        body.stat_organizations = org_counts.get(body.id, 0)
        body.stat_meetings = meeting_counts.get(body.id, 0)
        body.bundesland = get_bundesland_for_body(body)
        body.kind_label = get_kind_label_for_body(body)
    return bodies


class PortalHomeView(TemplateView):
    """Portal-Startseite mit Kommune-Auswahl und Statistiken."""

    template_name = "pages/portal/home.html"
    select_template_name = "pages/portal/select_body.html"

    def get(self, request, *args, **kwargs):
        # Self-Hosting-Fall: Existiert genau eine Kommune, wird sie automatisch
        # gewählt — kein Auswahlzwang beim ersten Aufruf.
        if is_all_bodies_mode(request):
            bodies = OParlBody.objects.filter(deleted=False)
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
            # Kommune-Auswahl: alle Kommunen mit echten Kennzahlen
            bodies = _bodies_with_stats()
            context["select_bodies"] = bodies
            context["stats"] = {
                "bodies": len(bodies),
                "organizations": OParlOrganization.objects.filter(deleted=False).count(),
                "persons": OParlPerson.objects.filter(deleted=False).count(),
                "meetings": OParlMeeting.objects.filter(deleted=False).count(),
                "papers": OParlPaper.objects.filter(deleted=False).count(),
                "files": OParlFile.objects.filter(deleted=False).count(),
            }
            context["upcoming_meetings"] = None
            context["recent_papers"] = None

        elif body:
            # Statistiken für die aktive Kommune
            context["stats"] = {
                "organizations": OParlOrganization.objects.filter(body=body, deleted=False).count(),
                "persons": OParlPerson.objects.filter(body=body, deleted=False).count(),
                "meetings": OParlMeeting.objects.filter(body=body, deleted=False).count(),
                "papers": OParlPaper.objects.filter(body=body, deleted=False).count(),
                "files": OParlFile.objects.filter(paper__body=body, deleted=False).count(),
                "public_questions": PublicQuestion.objects.filter(body=body, status="published").count(),
            }

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
