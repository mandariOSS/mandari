# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Leitstellen-Übersicht und mandantenübergreifende Suche (Issue #317).

Zugang nur über eine aktive Mitgliedschaft in der Leitstelle einer aktiven Mandantengruppe – sonst
404, auch für Superuser. Regeln zur Sichtbarkeit und zum Protokoll: ``leitstelle_service``.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.utils import timezone
from django.views.generic import TemplateView

from ..models import SessionTenantGroupMembership
from ..permissions import user_leitstellen
from ..services import leitstelle_service


class LeitstelleMixin(LoginRequiredMixin):
    """Leitstellen-Mitgliedschaft prüfen, bevor die Seite etwas lädt."""

    membership: SessionTenantGroupMembership
    request: HttpRequest

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        membership = leitstelle_service.membership_for(request.user, str(kwargs.get("group_slug", "")))
        if membership is None:
            # Keine Auskunft, ob es die Gruppe gibt
            raise Http404("Leitstelle nicht gefunden")
        self.membership = membership
        return super().dispatch(request, *args, **kwargs)

    def leitstelle_context(self) -> dict[str, Any]:
        return {
            "group": self.membership.group,
            "membership": self.membership,
            "user_leitstellen": user_leitstellen(self.request),
            "search_limit": leitstelle_service.SEARCH_LIMIT,
            "list_limit": leitstelle_service.LIST_LIMIT,
        }


class LeitstelleOverviewView(LeitstelleMixin, TemplateView):
    """Arbeitsvorräte, Fristen, Sitzungen und Kennzahlen aller Mandanten der Gruppe auf einer Seite."""

    template_name = "session/leitstelle/overview.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        overview = leitstelle_service.build_overview(self.membership, self.request.user)
        leitstelle_service.log_view(
            self.request, self.membership, overview.accesses, view=leitstelle_service.VIEW_OVERVIEW
        )
        totals = overview.totals
        overdue = totals["invitation_overdue"] + totals["paper_deadlines_overdue"]
        context.update(self.leitstelle_context())
        context.update(
            {
                "overview": overview,
                "accesses": overview.accesses,
                "stand": timezone.localtime(),
                "deadline_total": totals["invitation_deadlines"] + totals["paper_deadlines"],
                "deadline_tone": "red" if overdue else "amber",
                "deadline_days": leitstelle_service.DEADLINE_DAYS,
                "meeting_days": leitstelle_service.MEETING_DAYS,
            }
        )
        return context


class LeitstelleSearchView(LeitstelleMixin, TemplateView):
    """Suche über Titel und Nummern aller Mandanten der Gruppe (nur Gruppenrolle Leitstelle)."""

    template_name = "session/leitstelle/search.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not self.membership.sees_worklists:
            raise PermissionDenied("Die Gruppenrolle umfasst keine Suche.")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        raw = self.request.GET.get("q", "")
        query = leitstelle_service.normalize_query(raw)
        result = leitstelle_service.search(self.membership, self.request.user, query)
        if query:
            leitstelle_service.log_view(
                self.request,
                self.membership,
                result.accesses,
                view=leitstelle_service.VIEW_SEARCH,
                hits=result.hits_by_tenant(),
            )
        context.update(self.leitstelle_context())
        context.update(
            {
                "query": query,
                "raw_query": raw[: leitstelle_service.SEARCH_MAX_LENGTH],
                "result": result,
                "accesses": result.accesses,
                "too_short": bool(raw.strip()) and not query,
            }
        )
        return context
