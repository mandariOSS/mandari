# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Übergreifende Suche im Session-Bereich (Issue #45).

ORM-basierte Suche über Vorlagen, Sitzungen, TOPs/Beschlüsse, Protokolle,
Anlagen (inkl. extrahiertem Text) und Anträge. Jede Trefferart wird über
die jeweilige Berechtigung und Ö/NÖ-Sichtbarkeit gefiltert. Verschlüsselte
NÖ-Inhalte (z. B. NÖ-Protokollteile) sind bewusst nicht durchsuchbar.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.views.generic import TemplateView

from ..models import (
    SessionAgendaItem,
    SessionApplication,
    SessionFile,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionProtocol,
)
from ..permissions import SessionViewMixin

RESULT_LIMIT = 25


class SessionSearchView(SessionViewMixin, TemplateView):
    """Suchseite mit Filtern nach Gremium, Wahlperiode, Jahr und Trefferart."""

    template_name = "session/search/results.html"
    permission_required = "view_dashboard"

    def _filters(self):
        request = self.request
        filters = {"organization": None, "year": None, "term": None, "kind": ""}

        org_id = request.GET.get("organization")
        if org_id:
            try:
                filters["organization"] = SessionOrganization.objects.filter(
                    tenant=self.session_tenant, pk=org_id
                ).first()
            except (ValueError, DjangoValidationError):
                pass

        year = request.GET.get("year", "")
        if year.isdigit() and 2000 <= int(year) <= 2100:
            filters["year"] = int(year)

        term_id = request.GET.get("term")
        if term_id:
            try:
                filters["term"] = SessionLegislativeTerm.objects.filter(tenant=self.session_tenant, pk=term_id).first()
            except (ValueError, DjangoValidationError):
                pass

        kind = request.GET.get("kind", "")
        if kind in ("papers", "meetings", "resolutions", "protocols", "files", "applications"):
            filters["kind"] = kind
        return filters

    def _meeting_scope(self, filters):
        """Sitzungs-Queryset gemäß Filtern und Ö/NÖ-Recht (Basis für TOPs/Protokolle)."""
        qs = SessionMeeting.objects.filter(tenant=self.session_tenant)
        if not self.has_permission("view_non_public_meetings"):
            qs = qs.filter(is_public=True)
        if filters["organization"]:
            qs = qs.filter(organization=filters["organization"])
        if filters["year"]:
            qs = qs.filter(start__year=filters["year"])
        if filters["term"]:
            qs = qs.filter(legislative_term=filters["term"])
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip()
        filters = self._filters()

        results = {}
        total = 0
        if len(query) >= 2:
            meetings_scope = self._meeting_scope(filters)
            wants = lambda kind: not filters["kind"] or filters["kind"] == kind  # noqa: E731

            # Vorlagen
            if wants("papers") and self.has_permission("view_papers"):
                papers = SessionPaper.objects.filter(tenant=self.session_tenant).filter(
                    Q(name__icontains=query)
                    | Q(reference__icontains=query)
                    | Q(main_text__icontains=query)
                    | Q(resolution_text__icontains=query)
                )
                if not self.has_permission("view_non_public_papers"):
                    papers = papers.filter(is_public=True)
                if filters["organization"]:
                    papers = papers.filter(
                        Q(main_organization=filters["organization"])
                        | Q(originator_organization=filters["organization"])
                    )
                if filters["year"]:
                    papers = papers.filter(
                        Q(date__year=filters["year"]) | Q(date__isnull=True, created_at__year=filters["year"])
                    )
                results["papers"] = list(
                    papers.select_related("main_organization").order_by("-created_at")[:RESULT_LIMIT]
                )

            # Sitzungen
            if wants("meetings") and self.has_permission("view_meetings"):
                meetings = meetings_scope.filter(
                    Q(name__icontains=query) | Q(location__icontains=query) | Q(room__icontains=query)
                )
                results["meetings"] = list(meetings.select_related("organization").order_by("-start")[:RESULT_LIMIT])

            # TOPs / Beschlüsse
            if wants("resolutions") and self.has_permission("view_meetings"):
                items = SessionAgendaItem.objects.filter(meeting__in=meetings_scope).filter(
                    Q(name__icontains=query)
                    | Q(resolution_text__icontains=query)
                    | Q(resolution_number__icontains=query)
                    | Q(protocol_note__icontains=query)
                )
                if not self.has_permission("view_non_public_meetings"):
                    items = items.filter(is_public=True)
                results["resolutions"] = list(
                    items.select_related("meeting__organization").order_by("-meeting__start")[:RESULT_LIMIT]
                )

            # Protokolle (nur unverschlüsselter Ö-Teil)
            if wants("protocols") and self.has_permission("view_protocols"):
                protocols = SessionProtocol.objects.filter(meeting__in=meetings_scope, content__icontains=query)
                results["protocols"] = list(
                    protocols.select_related("meeting__organization").order_by("-meeting__start")[:RESULT_LIMIT]
                )

            # Anlagen (Dateiname + extrahierter Text)
            if wants("files") and self.has_permission("view_meetings"):
                files = SessionFile.objects.filter(tenant=self.session_tenant).filter(
                    Q(name__icontains=query) | Q(text_content__icontains=query)
                )
                if not self.has_permission("view_non_public_meetings"):
                    files = files.filter(is_public=True)
                if filters["organization"]:
                    files = files.filter(
                        Q(meeting__organization=filters["organization"])
                        | Q(agenda_item__meeting__organization=filters["organization"])
                        | Q(paper__main_organization=filters["organization"])
                    )
                if filters["year"]:
                    files = files.filter(created_at__year=filters["year"])
                results["files"] = list(
                    files.select_related("paper", "meeting", "agenda_item__meeting").order_by("-created_at")[
                        :RESULT_LIMIT
                    ]
                )

            # Anträge
            if wants("applications") and self.has_permission("view_applications"):
                applications = SessionApplication.objects.filter(tenant=self.session_tenant).filter(
                    Q(title__icontains=query) | Q(reference__icontains=query) | Q(submitter_name__icontains=query)
                )
                if filters["year"]:
                    applications = applications.filter(submitted_at__year=filters["year"])
                results["applications"] = list(applications.order_by("-submitted_at")[:RESULT_LIMIT])

            total = sum(len(v) for v in results.values())

        context.update(
            {
                "query": query,
                "results": results,
                "total": total,
                "result_limit": RESULT_LIMIT,
                "filters": filters,
                "organizations": SessionOrganization.objects.filter(
                    tenant=self.session_tenant, is_active=True
                ).order_by("name"),
                "terms": SessionLegislativeTerm.objects.filter(tenant=self.session_tenant).order_by("-start_date"),
                "kind_choices": [
                    ("", "Alles"),
                    ("papers", "Vorlagen"),
                    ("meetings", "Sitzungen"),
                    ("resolutions", "TOPs & Beschlüsse"),
                    ("protocols", "Protokolle"),
                    ("files", "Anlagen"),
                    ("applications", "Anträge"),
                ],
            }
        )
        return context
