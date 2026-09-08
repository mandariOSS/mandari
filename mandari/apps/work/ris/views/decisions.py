# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlusskontrolle für Mandatsträger (Issue #37, Sichtbarkeit in Work).

Zeigt den Umsetzungsstand der öffentlichen Beschlüsse aller Verwaltungen
(Session-Mandanten), die mit den Kommunen der Organisation verknüpft sind.
Nicht-öffentliche TOPs und Sitzungen erscheinen hier nie.
"""

from django.core.paginator import Paginator
from django.db.models import Q
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from ._mixins import RISBodiesMixin

PAGE_SIZE = 50


class RISDecisionsView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """Öffentliche Beschlüsse mit Umsetzungsstand, Ampel und Filtern."""

    template_name = "work/ris/decisions.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        from apps.session.models import SessionAgendaItem, SessionOrganization, SessionTenant
        from apps.session.services.resolution_service import DECIDED_RESULTS

        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_decisions"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        tenants = SessionTenant.objects.filter(oparl_body__in=bodies, is_active=True)
        context["tenants"] = tenants
        if not tenants.exists():
            context["no_session_tenant"] = True
            return context

        params = self.request.GET
        qs = (
            SessionAgendaItem.objects.filter(
                meeting__tenant__in=tenants,
                vote_result__in=DECIDED_RESULTS,
                is_public=True,
                meeting__is_public=True,
            )
            .exclude(is_withdrawn=True)
            .select_related("meeting__organization", "meeting__tenant", "paper")
            .order_by("-meeting__start", "order")
        )

        organization_id = params.get("organization", "")
        year = params.get("year", "")
        status = params.get("status", "")
        overdue = params.get("overdue") == "1"
        query = (params.get("q") or "").strip()

        if organization_id:
            qs = qs.filter(meeting__organization_id=organization_id)
        if year.isdigit():
            qs = qs.filter(meeting__start__year=int(year))
        if query:
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(resolution_text__icontains=query)
                | Q(resolution_number__icontains=query)
                | Q(implementation_recipient__icontains=query)
            )

        today = timezone.localdate()
        approved = qs.filter(vote_result="approved")
        overdue_q = Q(vote_result="approved", implementation_deadline__lt=today) & ~Q(implementation_status="done")
        context["tracking_stats"] = {
            "open": approved.filter(implementation_status="open").count(),
            "in_progress": approved.filter(implementation_status="in_progress").count(),
            "done": approved.filter(implementation_status="done").count(),
            "deferred": approved.filter(implementation_status="deferred").count(),
            "overdue": qs.filter(overdue_q).count(),
            "total": qs.count(),
        }

        if status in dict(SessionAgendaItem.IMPLEMENTATION_CHOICES):
            qs = qs.filter(vote_result="approved", implementation_status=status)
        if overdue:
            qs = qs.filter(overdue_q)

        paginator = Paginator(qs, PAGE_SIZE)
        page = paginator.get_page(params.get("page"))
        context.update(
            {
                "items": page.object_list,
                "page_obj": page,
                "paginator": paginator,
                "organizations": SessionOrganization.objects.filter(
                    tenant__in=tenants, is_active=True, organization_type__in=["council", "committee", "advisory"]
                ).order_by("name"),
                "years": sorted(
                    {
                        d.year
                        for d in SessionAgendaItem.objects.filter(
                            meeting__tenant__in=tenants, vote_result__in=DECIDED_RESULTS, is_public=True
                        ).values_list("meeting__start", flat=True)
                        if d
                    },
                    reverse=True,
                ),
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
