# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from datetime import timedelta

from django.utils import timezone
from django.views.generic import (
    TemplateView,
)

from ..models import (
    SessionApplication,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
)
from ..permissions import SessionViewMixin

# =============================================================================
# DASHBOARD
# =============================================================================


class DashboardView(SessionViewMixin, TemplateView):
    """Main dashboard for Session RIS."""

    template_name = "session/dashboard.html"
    permission_required = "view_dashboard"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tenant = self.session_tenant
        today = timezone.now().date()

        # Upcoming meetings (next 30 days) — Ö/NÖ nur für Berechtigte
        upcoming = SessionMeeting.objects.filter(
            tenant=tenant,
            start__date__gte=today,
            start__date__lte=today + timedelta(days=30),
            cancelled=False,
        )
        if not self.has_permission("view_non_public_meetings"):
            upcoming = upcoming.filter(is_public=True)
        context["upcoming_meetings"] = upcoming.select_related("organization").order_by("start")[:5]

        # Recent papers — Ö/NÖ nur für Berechtigte
        recent_papers = SessionPaper.objects.filter(tenant=tenant)
        if not self.has_permission("view_non_public_papers"):
            recent_papers = recent_papers.filter(is_public=True)
        context["recent_papers"] = recent_papers.select_related(
            "main_organization", "originator_organization"
        ).order_by("-created_at")[:5]

        # Pending applications
        context["pending_applications"] = SessionApplication.objects.filter(
            tenant=tenant,
            status__in=["submitted", "received", "in_review"],
        ).order_by("-submitted_at")[:5]

        # Statistics
        context["stats"] = {
            "meetings_total": SessionMeeting.objects.filter(tenant=tenant).count(),
            "meetings_upcoming": SessionMeeting.objects.filter(
                tenant=tenant,
                start__date__gte=today,
                cancelled=False,
            ).count(),
            "papers_total": SessionPaper.objects.filter(tenant=tenant).count(),
            "papers_draft": SessionPaper.objects.filter(tenant=tenant, status="draft").count(),
            "applications_pending": SessionApplication.objects.filter(
                tenant=tenant,
                status__in=["submitted", "received", "in_review"],
            ).count(),
            "organizations_count": SessionOrganization.objects.filter(tenant=tenant, is_active=True).count(),
            "persons_count": SessionPerson.objects.filter(tenant=tenant, is_active=True).count(),
        }

        return context
