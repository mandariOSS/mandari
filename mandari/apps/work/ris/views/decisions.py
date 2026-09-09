# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlusskontrolle für Mandatsträger (Issue #37, Sichtbarkeit in Work).

Zeigt den Umsetzungsstand der öffentlichen Beschlüsse aller Verwaltungen
(Session-Mandanten), die mit den Kommunen der Organisation verknüpft sind.
Nicht-öffentliche TOPs und Sitzungen erscheinen hier nie.
"""

from django.core.paginator import Paginator
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.session.models import SessionAgendaItem

from .. import selectors
from ._mixins import RISBodiesMixin

PAGE_SIZE = 50


class RISDecisionsView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """Öffentliche Beschlüsse mit Umsetzungsstand, Ampel und Filtern."""

    template_name = "work/ris/decisions.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_decisions"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        tenants = selectors.session_tenants(bodies)
        context["tenants"] = tenants
        if not tenants.exists():
            context["no_session_tenant"] = True
            return context

        params = self.request.GET
        organization_id = params.get("organization", "")
        year = params.get("year", "")
        status = params.get("status", "")
        overdue = params.get("overdue") == "1"
        query = (params.get("q") or "").strip()
        today = timezone.localdate()

        qs = selectors.filter_decisions(
            selectors.decided_items(tenants), organization_id=organization_id, year=year, query=query
        )
        context["tracking_stats"] = selectors.tracking_stats(qs, today)

        qs = selectors.filter_implementation(qs, status=status, overdue_only=overdue, today=today)

        paginator = Paginator(qs, PAGE_SIZE)
        page = paginator.get_page(params.get("page"))
        context.update(
            {
                "items": page.object_list,
                "page_obj": page,
                "paginator": paginator,
                "organizations": selectors.decision_organizations(tenants),
                "years": selectors.decision_years(tenants),
                "implementation_choices": SessionAgendaItem.IMPLEMENTATION_CHOICES,
                "filter_organization": organization_id,
                "filter_year": year,
                "filter_status": status,
                "filter_overdue": overdue,
                "search_query": query,
                "has_filter": bool(organization_id or year or status or overdue or query),
                "today": today,
            }
        )
        return context
