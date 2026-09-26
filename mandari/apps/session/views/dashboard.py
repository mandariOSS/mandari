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
    SessionAgendaItem,
    SessionApplication,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
)
from ..permissions import SessionViewMixin
from ..services import joint_meeting_service

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
        permissions = self.session_permissions
        # Jede Kachel nur mit ihrem Fachrecht (Funktionstrennung, Issue #221): Das Dashboard-Recht
        # allein zeigt keine Sitzungen, Vorlagen oder Anträge
        can_meetings = "view_meetings" in permissions
        can_papers = "view_papers" in permissions
        can_applications = "view_applications" in permissions
        context.update(
            {
                "can_view_meetings": can_meetings,
                "can_view_papers": can_papers,
                "can_view_applications": can_applications,
            }
        )
        meetings = SessionMeeting.objects.filter(tenant=tenant).visible_to(permissions)
        papers = SessionPaper.objects.filter(tenant=tenant).visible_to(permissions)

        # Upcoming meetings (next 30 days) — Ö/NÖ nur für Berechtigte
        if can_meetings:
            upcoming = meetings.filter(
                start__date__gte=today,
                start__date__lte=today + timedelta(days=30),
                cancelled=False,
            )
            context["upcoming_meetings"] = upcoming.select_related("organization").order_by("start")[:5]

        # Fristwarnung Ladung (Issue #29): kommende Sitzungen ohne versandte
        # Einladung — „Ladung muss bis TT.MM. raus" (überfällige zuerst)
        if can_meetings:
            # Gemeinsame Sitzungen (Issue #317): längste Ladungsfrist der beteiligten Gremien
            pending_invitations = SessionMeeting.with_joint_flag(
                SessionMeeting.objects.filter(
                    tenant=tenant,
                    start__gte=timezone.now(),
                    cancelled=False,
                    invitation_sent_at__isnull=True,
                    meeting_state__in=["draft", "scheduled"],
                )
                .select_related("organization")
                .order_by("start")
            )
            if not self.has_permission("view_non_public_meetings"):
                pending_invitations = pending_invitations.filter(is_public=True)
            candidates = list(pending_invitations[:20])
            joint_meeting_service.prefetch_joint(candidates)
            warnings = sorted(candidates, key=lambda m: m.invitation_deadline)
            context["invitation_warnings"] = warnings
            context["invitation_overdue_count"] = sum(1 for m in warnings if m.invitation_overdue)

        # Beschlusskontrolle (Issue #37): überfällige Beschlüsse prominent warnen
        if can_meetings:
            overdue_resolutions = (
                SessionAgendaItem.objects.filter(
                    meeting__tenant=tenant,
                    vote_result="approved",
                    implementation_deadline__lt=today,
                )
                .exclude(implementation_status="done")
                .select_related("meeting__organization")
                .order_by("implementation_deadline")
            )
            if not self.has_permission("view_non_public_meetings"):
                overdue_resolutions = overdue_resolutions.filter(is_public=True, meeting__is_public=True)
            context["overdue_resolutions"] = list(overdue_resolutions[:5])
            context["overdue_resolutions_count"] = overdue_resolutions.count()

        # Recent papers — Ö/NÖ nur für Berechtigte
        if can_papers:
            context["recent_papers"] = papers.select_related("main_organization", "originator_organization").order_by(
                "-created_at"
            )[:5]

        # Pending applications
        open_applications = SessionApplication.objects.filter(
            tenant=tenant,
            status__in=["submitted", "received", "in_review"],
        )
        if can_applications:
            context["pending_applications"] = open_applications.order_by("-submitted_at")[:5]

        # Arbeitsvorrat „Meine zu prüfenden Vorlagen" (Issue #33)
        if "approve_papers" in permissions:
            review_papers = papers.filter(status="review")
            context["review_papers"] = review_papers.select_related("main_organization").order_by("created_at")[:5]

        # Statistics – nur Zahlen, die die Person auch in den Listen sähe
        stats = {}
        if can_meetings:
            stats["meetings_total"] = meetings.count()
            stats["meetings_upcoming"] = meetings.filter(start__date__gte=today, cancelled=False).count()
            stats["organizations_count"] = SessionOrganization.objects.filter(tenant=tenant, is_active=True).count()
            stats["persons_count"] = SessionPerson.objects.filter(tenant=tenant, is_active=True).count()
        if can_papers:
            stats["papers_total"] = papers.count()
            stats["papers_draft"] = papers.filter(status="draft").count()
        if can_applications:
            stats["applications_pending"] = open_applications.count()
        context["stats"] = stats

        return context
