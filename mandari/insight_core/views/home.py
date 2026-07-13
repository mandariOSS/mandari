"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

import json

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
                "files": OParlFile.objects.count(),
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
                "files": OParlFile.objects.filter(paper__body=body).count(),
                "public_questions": PublicQuestion.objects.filter(body=body, status="published").count(),
            }

            # Nächste Sitzungen (5 für einheitliche Listen)
            context["upcoming_meetings"] = (
                OParlMeeting.objects.filter(body=body, start__gte=timezone.now(), cancelled=False)
                .prefetch_related("organizations")
                .order_by("start")[:5]
            )

            # Neueste Vorgänge
            context["recent_papers"] = OParlPaper.objects.filter(body=body).order_by("-date", "-oparl_created")[:5]

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
