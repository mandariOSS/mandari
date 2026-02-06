# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dashboard views for the Work module.
"""

from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin


class DashboardView(WorkViewMixin, TemplateView):
    """Main dashboard view showing overview of all work areas."""

    template_name = "work/dashboard/index.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "dashboard"

        # Upcoming meetings (faction + RIS)
        context["upcoming_meetings"] = self.get_upcoming_meetings()

        # My open tasks
        context["my_tasks"] = self.get_my_tasks()

        # Recent documents
        context["recent_documents"] = self.get_recent_documents()

        return context

    def get_upcoming_meetings(self):
        """
        Get upcoming meetings combining faction meetings and RIS committee meetings.
        Returns a unified list sorted by start time.
        """
        from django.db.models import Prefetch

        from apps.work.faction.models import FactionMeeting
        from insight_core.models import OParlMeeting, OParlOrganization

        now = timezone.now()
        meetings = []

        # Faction meetings (not completed/cancelled, starting from now)
        # select_related for any FK fields that might be accessed
        faction_meetings = (
            FactionMeeting.objects.filter(
                organization=self.organization,
                start__gte=now,
                status__in=["draft", "planned", "invited", "ongoing"],
            )
            .select_related("organization")
            .order_by("start")[:5]
        )

        for meeting in faction_meetings:
            meetings.append(
                {
                    "type": "faction",
                    "id": meeting.id,
                    "title": meeting.title,
                    "start": meeting.start,
                    "location": meeting.location if not meeting.is_virtual else "Online",
                    "status": meeting.status,
                    "url_name": "work:faction_detail",
                    "url_kwargs": {"org_slug": self.organization.slug, "meeting_id": meeting.id},
                }
            )

        # RIS/Committee meetings (if organization has OParl body)
        if self.organization.body:
            # Optimize with Prefetch to only fetch needed fields
            ris_meetings = (
                OParlMeeting.objects.filter(body=self.organization.body, start__gte=now, cancelled=False)
                .prefetch_related(
                    Prefetch(
                        "organizations",
                        queryset=OParlOrganization.objects.only("id", "name", "short_name"),
                    )
                )
                .order_by("start")[:5]
            )

            for meeting in ris_meetings:
                # Get the committee name (first organization, typically the main committee)
                # Use prefetched cache - don't trigger new query
                orgs = list(meeting.organizations.all())
                if orgs:
                    committee_name = orgs[0].name or orgs[0].short_name or "Gremium"
                else:
                    committee_name = meeting.name or "RIS-Sitzung"

                meetings.append(
                    {
                        "type": "ris",
                        "id": meeting.id,
                        "title": committee_name,
                        "start": meeting.start,
                        "location": meeting.location_name or "",
                        "status": meeting.meeting_state or "",
                        "url_name": "work:meeting_detail",
                        "url_kwargs": {
                            "org_slug": self.organization.slug,
                            "meeting_id": meeting.id,
                        },
                    }
                )

        # Sort all meetings by start time and limit to 5
        meetings.sort(key=lambda x: x["start"])
        return meetings[:5]

    def get_my_tasks(self):
        """Get open tasks assigned to the current user."""
        from apps.work.tasks.models import Task

        return (
            Task.objects.filter(
                organization=self.organization,
                assigned_to=self.membership,
                status__in=["todo", "in_progress"],
            )
            .select_related("assigned_to__user", "created_by__user")
            .order_by("-priority", "due_date", "-created_at")[:5]
        )

    def get_recent_documents(self):
        """Get recently updated documents/motions."""
        from apps.work.motions.models import Motion

        return (
            Motion.objects.filter(organization=self.organization)
            .exclude(status__in=["deleted", "archived"])
            .select_related("author__user")
            .order_by("-updated_at")[:5]
        )
