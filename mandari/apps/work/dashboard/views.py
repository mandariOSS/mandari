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
        context["today"] = timezone.now()

        # "Meine Gremien" personalization: filter meetings/documents to the
        # user's committees unless they explicitly requested the org-wide view.
        show_all = self.request.GET.get("alle") == "1"
        my_committee_ids = self.get_my_committee_ids()
        context["has_my_committees"] = bool(my_committee_ids)
        context["dashboard_personalized"] = bool(my_committee_ids) and not show_all
        context["my_committees"] = self.get_my_committees_display()
        if show_all:
            my_committee_ids = None

        # Upcoming meetings (faction + RIS)
        context["upcoming_meetings"] = self.get_upcoming_meetings(my_committee_ids)

        # My open tasks
        context["my_tasks"] = self.get_my_tasks()

        # Recent documents
        context["recent_documents"] = self.get_recent_documents(my_committee_ids)

        return context

    def get_my_committee_ids(self):
        """
        IDs of the committees relevant for the current member ("Meine Gremien").

        Preference order: self-selected followed committees, then the
        admin-assigned committees. Returns None when neither is set, meaning
        the dashboard stays org-wide.
        """
        followed = set(self.membership.followed_organizations.values_list("id", flat=True))
        if followed:
            return followed
        assigned = set(self.membership.oparl_committees.values_list("id", flat=True))
        return assigned or None

    def get_my_committees_display(self):
        """Committee objects for the header badges (followed, else assigned)."""
        followed = list(self.membership.followed_organizations.all().order_by("name")[:4])
        if followed:
            return followed
        return list(self.membership.oparl_committees.all().order_by("name")[:4])

    def get_upcoming_meetings(self, my_committee_ids=None):
        """
        Get upcoming meetings combining faction meetings and RIS committee meetings.
        Returns a unified list sorted by start time.
        """
        from django.db.models import Prefetch

        from apps.work.faction.models import FactionMeeting
        from insight_core.models import OParlMeeting, OParlOrganization

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        meetings = []

        # Faction meetings (not completed/cancelled, starting from today)
        faction_meetings = (
            FactionMeeting.objects.filter(
                organization=self.organization,
                start__gte=today_start,
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

        # RIS/Committee meetings (if organization has OParl bodies)
        org_bodies = self.organization.get_all_bodies()
        if org_bodies.exists():
            # When filtering on "Meine Gremien" fetch a wider window, because
            # committee references may only exist in raw_json and can only be
            # matched after resolution below.
            ris_limit = 25 if my_committee_ids else 5

            # Optimize with Prefetch to only fetch needed fields
            ris_meetings = (
                OParlMeeting.objects.filter(body__in=org_bodies, start__gte=today_start, cancelled=False)
                .prefetch_related(
                    Prefetch(
                        "organizations",
                        queryset=OParlOrganization.objects.only("id", "name", "short_name"),
                    )
                )
                .order_by("start")[:ris_limit]
            )

            ris_meetings = list(ris_meetings)

            # Fallback für Meetings ohne verknüpfte Gremien: Referenzen aus
            # raw_json auflösen (eine Batch-Query für alle betroffenen Meetings)
            unresolved_refs = set()
            for meeting in ris_meetings:
                if not meeting.organizations.all():
                    refs = (meeting.raw_json or {}).get("organization", [])
                    if isinstance(refs, str):
                        refs = [refs]
                    unresolved_refs.update(refs)

            orgs_by_external_id = {}
            if unresolved_refs:
                orgs_by_external_id = {
                    org.external_id: org
                    for org in OParlOrganization.objects.filter(external_id__in=unresolved_refs).only(
                        "id", "external_id", "name", "short_name"
                    )
                }

            ris_count = 0
            for meeting in ris_meetings:
                # Get the committee name (first organization, typically the main committee)
                # Use prefetched cache - don't trigger new query
                orgs = list(meeting.organizations.all())
                if not orgs:
                    refs = (meeting.raw_json or {}).get("organization", [])
                    if isinstance(refs, str):
                        refs = [refs]
                    orgs = [orgs_by_external_id[ref] for ref in refs if ref in orgs_by_external_id]

                # "Meine Gremien" filter: skip meetings of other committees
                if my_committee_ids and not any(org.id in my_committee_ids for org in orgs):
                    continue

                ris_count += 1
                if ris_count > 5:
                    break

                if orgs:
                    committee_name = orgs[0].name or orgs[0].short_name or "Gremium"
                    subtitle = meeting.name or ""
                else:
                    committee_name = meeting.name or "RIS-Sitzung"
                    subtitle = ""

                meetings.append(
                    {
                        "type": "ris",
                        "id": meeting.id,
                        "title": committee_name,
                        "subtitle": subtitle,
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

    def get_recent_documents(self, my_committee_ids=None):
        """Get recently updated documents/motions."""
        from django.db.models import Q

        from apps.work.motions.models import Motion

        queryset = Motion.objects.filter(organization=self.organization).exclude(status__in=["deleted", "archived"])

        if my_committee_ids:
            # "Meine Gremien": keep internal documents (no committee link) and
            # documents targeted at one of the user's committees.
            queryset = queryset.filter(
                Q(related_meeting__isnull=True) | Q(related_meeting__organizations__id__in=my_committee_ids)
            ).distinct()

        return queryset.select_related("author__user").order_by("-updated_at")[:5]
