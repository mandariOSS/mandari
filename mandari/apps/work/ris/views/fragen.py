# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fragen an die Ratsdaten (Issue #853): Antwort aus den öffentlichen Ratsdaten mit Quellen und Links in Work.

Das Fragefeld steht auf der Recherche-Suche im neuen Rahmen; die Antwort kommt per HTMX in den Bereich über den
Treffern, ohne JavaScript als eigene Seite. Logik und Grenzen: ``apps.work.ris.fragen``.
"""

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import fragen
from ._mixins import RISBodiesMixin


class RISFrageView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """POST: eine Frage beantworten. GET führt zur Suche zurück (etwa nach Neuladen der Antwortseite)."""

    template_name = "work/ris/frage.html"
    permission_required = "ris.view"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self  # Mixins ohne Typangaben (WorkViewMixin, RISBodiesMixin)
        return redirect("work:ris_search", org_slug=ansicht.organization.slug)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        context = ansicht.get_context_data(**kwargs)
        context["active_nav"] = "ris"
        frage, kommune_id = fragen.eingabe(request.POST)
        bodies = ansicht.setup_body_context(context)
        body = fragen.kommune(bodies, kommune_id) if bodies is not None else None
        context["antwort"] = fragen.beantworten(ansicht.organization, ansicht.membership, frage, body)
        return self.response_class(
            request=request,
            template=[fragen.antwort_vorlage(request.headers)],
            context=context,
            using=self.template_engine,
        )
