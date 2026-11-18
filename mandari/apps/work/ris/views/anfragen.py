# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anfrage beantworten (Issue #1488): Seite mit dem Formular und dem Antwortentwurf aus den öffentlichen Ratsdaten.

GET zeigt das Formular, POST erstellt den Entwurf; per HTMX kommt nur der Entwurf in den Bereich unter dem Formular,
ohne JavaScript die ganze Seite mit ausgefülltem Formular. Logik und Grenzen: ``apps.work.ris.anfragen``.
"""

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.work.rahmen import neues_design

from .. import anfragen, fragen
from ._mixins import RISBodiesMixin


class RISAnfrageView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """Antwortentwurf zu einer Bürger- oder Presseanfrage; die Seite gibt es nur im neuen Rahmen."""

    template_name = anfragen.SEITE
    permission_required = "ris.view"

    def _kontext(self, **kwargs: Any) -> dict[str, Any]:
        ansicht: Any = self  # Mixins ohne Typangaben (WorkViewMixin, RISBodiesMixin)
        context: dict[str, Any] = ansicht.get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["bodies_der_anfrage"] = ansicht.setup_body_context(context)
        context["verfuegbar"] = fragen.verfuegbar(ansicht.organization)
        context["arten"] = [(schluessel, art.label) for schluessel, art in anfragen.ARTEN.items()]
        context["art"] = anfragen.STANDARD_ART
        context["max_anfrage"] = anfragen.MAX_ANFRAGE
        context["max_anrede"] = anfragen.MAX_ANREDE
        context["standard_anrede"] = anfragen.STANDARD_ANREDE
        context["anfrage_text"] = ""
        context["anrede"] = ""
        context["gewaehlte_kommune"] = ""
        return context

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        if not neues_design(ansicht.organization):
            return redirect("work:ris_search", org_slug=ansicht.organization.slug)
        return self.response_class(
            request=request, template=[anfragen.SEITE], context=self._kontext(**kwargs), using=self.template_engine
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ansicht: Any = self
        if not neues_design(ansicht.organization):
            return redirect("work:ris_search", org_slug=ansicht.organization.slug)
        context = self._kontext(**kwargs)
        anfrage, art, anrede, kommune_id = anfragen.eingabe(request.POST)
        bodies = context["bodies_der_anfrage"]
        body = fragen.kommune(bodies, kommune_id) if bodies is not None else None
        context["entwurf"] = anfragen.entwerfen(
            ansicht.organization, ansicht.membership, anfrage, body, art=art, anrede=anrede
        )
        context["anfrage_text"] = context["entwurf"].anfrage
        context["art"] = art
        context["anrede"] = anrede
        context["gewaehlte_kommune"] = str(body.pk) if body is not None else ""
        return self.response_class(
            request=request,
            template=[anfragen.entwurf_vorlage(request.headers)],
            context=context,
            using=self.template_engine,
        )
