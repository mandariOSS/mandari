# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlusskontrolle für Mandatsträger (Issue #37, Sichtbarkeit in Work).

Zeigt die öffentlichen Beschlüsse der Kommunen der Organisation mit ihrem Umsetzungsstand. Nicht-öffentliche TOPs und
Sitzungen erscheinen hier nie. Woher die Beschlüsse kommen (Session-Mandanten oder RIS-Bestand), bestimmt der Schalter
``RIS_DECISION_TRACKING`` (Issue #538, ``apps.work.ris.beschluesse``). Aus dem RIS-Bestand zeigen Adressen aus der Zeit
vor der Umstellung (Kennung eines Session-Beschlusses) weiter den bisherigen Beschluss.

Im neuen Erscheinungsbild (Issue #951) zeigt die Liste ``work/ris/neu/beschluesse.html``; jeder Beschluss hat dort eine
eigene Seite (``RISDecisionDetailView``) mit vollem Beschlusstext und Umsetzungsstand – dieselben Angaben wie in der
Liste, aus derselben Auswahl (öffentlich, entschieden, eigene Kommunen).
"""

from django.core.paginator import Paginator
from django.http import Http404
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.session.models import SessionAgendaItem

from .. import beschluesse, neu, selectors
from ._mixins import RISBodiesMixin

PAGE_SIZE = 50


class RISDecisionsView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """Öffentliche Beschlüsse mit Umsetzungsstand, Ampel und Filtern."""

    template_name = "work/ris/decisions.html"
    neue_vorlage = "work/ris/neu/beschluesse.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_decisions"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        auswahl = beschluesse.Auswahl.aus_anfrage(self.request.GET)
        today = timezone.localdate()
        if beschluesse.aus_bestand():
            qs, angaben = beschluesse.liste_aus_bestand(bodies, auswahl, today)
            anzeige = beschluesse.anzeige_bestand
        else:
            tenants = selectors.session_tenants(bodies)
            context["tenants"] = tenants
            if not tenants.exists():
                context["no_session_tenant"] = True
                return context
            qs = selectors.filter_decisions(
                selectors.decided_items(tenants),
                organization_id=auswahl.organization_id,
                year=auswahl.year,
                query=auswahl.query,
            )
            angaben = {
                "tracking_stats": selectors.tracking_stats(qs, today),
                "mehrere_herausgeber": len(tenants) > 1,
                "organizations": selectors.decision_organizations(tenants),
                "years": selectors.decision_years(tenants),
                "implementation_choices": SessionAgendaItem.IMPLEMENTATION_CHOICES,
            }
            qs = selectors.filter_implementation(qs, status=auswahl.status, overdue_only=auswahl.overdue, today=today)
            anzeige = beschluesse.anzeige_session

        paginator = Paginator(qs, PAGE_SIZE)
        page = paginator.get_page(auswahl.page)
        items = list(page.object_list)
        anzeige(items)
        context |= angaben | auswahl.kontext()
        context |= {"items": items, "page_obj": page, "paginator": paginator, "today": today}
        if self.neu:
            context |= neu.beschluesse(context)
        return context


class RISDecisionDetailView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """Ein Beschluss mit vollem Text und Umsetzungsstand (neue Seite, Issue #951); Rechte wie die Liste."""

    template_name = "work/ris/neu/beschluss.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_decisions"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        if beschluesse.aus_bestand():
            gefunden = beschluesse.beschluss_aus_bestand(bodies, kwargs["item_id"])
            if gefunden is not None:
                context |= gefunden
                return context
            # Sonst eine Adresse aus der Zeit vor der Umstellung: Kennung eines Session-Beschlusses (unten)

        tenants, item = selectors.decision_detail(bodies, kwargs["item_id"])
        context["tenants"] = tenants
        if tenants is None:
            if beschluesse.aus_bestand():
                raise Http404("Beschluss nicht gefunden")
            context["no_session_tenant"] = True
            return context
        if item is None:
            raise Http404("Beschluss nicht gefunden")
        beschluesse.anzeige_session([item])
        context |= {"item": item, "mehrere_herausgeber": len(tenants) > 1}
        return context
