# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from typing import Any

from django.http import Http404
from django.shortcuts import render
from django.urls import reverse

from apps.work.rahmen import neues_design

from .. import neu, selectors

#: Seite für Sitzungen und Vorgänge, die mandari Session aus der Veröffentlichung zurückgenommen hat (Issue #853)
ZURUECKGENOMMEN_VORLAGE = "work/ris/zurueckgenommen.html"


class RISBodiesMixin:
    """
    Multi-Kommune-Unterstützung für RIS-Views.

    Eine Organisation kann mit mehreren OParl-Bodies verknüpft sein
    (Organization.bodies M2M + primärer FK Organization.body).

    Im neuen Erscheinungsbild (Schalter je Organisation, Issues #852/#853) nimmt eine View mit ``neue_vorlage``
    diese Vorlage aus ``work/ris/neu/``; ``neu`` sagt der View, ob sie den Kontext dafür ergänzen soll.
    ``?ansicht=bisher`` öffnet die bisherige Vorlage (Rückweg wie ``apps.work.rahmen.NeueVorlageMixin``, Issue #951).
    """

    #: Vorlage im neuen Erscheinungsbild; leer = dieselbe Vorlage wie bisher
    neue_vorlage = ""

    @property
    def neu(self):
        return bool(self.neue_vorlage) and not neu.bisher_angefragt(self.request) and neues_design(self.organization)

    def get_template_names(self):
        if self.neu:
            return [self.neue_vorlage]
        return super().get_template_names()

    def zurueckgenommen(self, context: dict[str, Any], objekt: Any, liste: str) -> dict[str, Any]:
        """
        Detailseite eines Objekts, das es in den Kommunen nicht (mehr) gibt: Hat mandari Session es zurückgenommen
        (``objekt``), zeigt die Seite wie das Bürgerportal nur einen neutralen Hinweis ohne Titel und Inhalt
        (``render_to_response`` antwortet mit 410) und den Weg zur Liste (Adressname ``liste``), sonst 404.
        """
        if objekt is None:
            raise Http404
        context["zurueckgenommen_am"] = objekt.deleted_at
        context["zurueckgenommen"] = True
        context["zurueckgenommen_liste"] = reverse(liste, kwargs={"org_slug": self.organization.slug})
        return context

    def render_to_response(self, context: dict[str, Any], **response_kwargs: Any) -> Any:
        if context.get("zurueckgenommen"):
            response = render(self.request, ZURUECKGENOMMEN_VORLAGE, context, status=410)
            response["Cache-Control"] = "no-store"
            response["X-Robots-Tag"] = "noindex"
            return response
        return super().render_to_response(context, **response_kwargs)

    def get_bodies(self):
        """Alle verknüpften Kommunen als QuerySet (für body__in-Filter)."""
        return selectors.bodies_for_organization(self.organization)

    def setup_body_context(self, context):
        """
        Setzt bodies/body/no_body_linked in den Context.

        Returns:
            QuerySet der Bodies oder None, wenn keine Kommune verknüpft ist.
        """
        bodies = self.get_bodies()
        if not bodies.exists():
            context["no_body_linked"] = True
            return None
        context["bodies"] = bodies
        context["has_multiple_bodies"] = bodies.count() > 1
        # Primäre Kommune für Anzeige (Subtitle, Karte etc.)
        context["body"] = selectors.primary_body(self.organization)
        return bodies
