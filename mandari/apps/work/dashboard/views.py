# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dashboard views for the Work module.
"""

from typing import Any

from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.work.dashboard import selectors
from apps.work.dashboard.hinweise import hinweis_fuer_start
from apps.work.organization.selectors import my_committees
from apps.work.rahmen import neues_design


class DashboardView(WorkViewMixin, TemplateView):
    """Main dashboard view showing overview of all work areas."""

    template_name = "work/dashboard/index.html"
    permission_required = "dashboard.view"

    def get_template_names(self) -> list[str]:
        """Im neuen Erscheinungsbild (Schalter je Organisation, Issue #852) die neue Startseite."""
        if neues_design(self.organization):
            return ["work/dashboard/start.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "dashboard"
        context["today"] = timezone.now()
        # Hinweisband oben: höchstens ein Hinweis, etwa die neueste ungelesene Ankündigung (Issue #857)
        context["hinweis"] = hinweis_fuer_start(self.membership)

        # "Meine Gremien" personalization: filter meetings/documents to the
        # user's committees unless they explicitly requested the org-wide view.
        # Followed committees first, else assigned ones; without either the
        # dashboard stays org-wide. Same rule as the meeting list (Issue #647).
        show_all = self.request.GET.get("alle") == "1"
        mine = my_committees(self.membership)
        my_committee_ids = mine.ids or None
        context["has_my_committees"] = bool(mine)
        context["dashboard_personalized"] = bool(mine) and not show_all
        # Badges im Seitenkopf: höchstens vier Gremien
        context["my_committees"] = list(mine.committees[:4])
        if show_all:
            my_committee_ids = None

        # Upcoming meetings (faction + RIS)
        context["upcoming_meetings"] = self.get_upcoming_meetings(my_committee_ids)

        # My open tasks
        context["my_tasks"] = self.get_my_tasks()

        # Recent documents
        context["recent_documents"] = self.get_recent_documents(my_committee_ids)

        if neues_design(self.organization):
            context.update(self.get_start_context(context["upcoming_meetings"], my_committee_ids))
        return context

    def get_start_context(self, sitzungen: list[dict[str, Any]], committee_ids: set[Any] | None) -> dict[str, Any]:
        """Startseite im neuen Rahmen: Vorbereitungsstand je Sitzung, Satz und Hauptaktion, Für Sie, neue Vorlagen."""
        stand = selectors.vorbereitungsstand(self.organization, [s["id"] for s in sitzungen if s["type"] == "ris"])
        for sitzung in sitzungen:
            sitzung["stand"] = stand.get(sitzung["id"])
            if sitzung["type"] == "faction":
                sitzung["stand_text"] = selectors.stand_fraktionssitzung(sitzung)
        # Ohne „meetings.prepare“ (z. B. Parteimitglieder) führen Hauptaktion und Zeilenlink auf die Sitzung selbst
        darf_vorbereiten = self.has_permission("meetings.prepare")
        satz, aktion = selectors.satz_und_hauptaktion(
            self.organization, sitzungen, stand, darf_vorbereiten=darf_vorbereiten
        )
        return {
            "start_satz": satz,
            "start_aktion": aktion,
            "darf_vorbereiten": darf_vorbereiten,
            "fuer_sie": selectors.fuer_sie(self.organization, self.membership),
            "neu_in_gremien": selectors.neu_in_gremien(
                self.organization, list(committee_ids) if committee_ids else None
            ),
        }

    def get_upcoming_meetings(self, my_committee_ids=None):
        """
        Get upcoming meetings combining faction meetings and RIS committee meetings.
        Returns a unified list sorted by start time.
        """
        from django.db.models import Prefetch

        from apps.work.faction.models import FactionMeeting
        from hub.ris import selectors as ris
        from insight_core.models import OParlOrganization

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
                    "status_display": meeting.get_status_display(),
                    "invitation_sent": meeting.invitation_sent,
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
            ris_meetings = ris.upcoming_meetings(org_bodies, since=today_start).prefetch_related(
                Prefetch(
                    "organizations",
                    queryset=OParlOrganization.objects.only("id", "name", "short_name"),
                )
            )[:ris_limit]

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
                    for org in ris.organizations_by_external_id(unresolved_refs).only(
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

        queryset = Motion.visible_to(self.membership).exclude(status="archived")

        if my_committee_ids:
            # "Meine Gremien": keep internal documents (no committee link) and
            # documents targeted at one of the user's committees.
            queryset = queryset.filter(
                Q(related_meeting__isnull=True) | Q(related_meeting__organizations__id__in=my_committee_ids)
            ).distinct()

        return queryset.select_related("author__user").order_by("-updated_at")[:5]
