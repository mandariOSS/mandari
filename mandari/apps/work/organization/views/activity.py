# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import logging

from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


# =============================================================================
# PROFILE: ACTIVITY OVERVIEW
# =============================================================================


class ProfileActivityView(WorkViewMixin, TemplateView):
    """Activity overview with statistics and timeline."""

    template_name = "work/profile/activity.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "activity"

        membership = self.membership
        org = self.organization

        # --- Statistics ---
        from django.db.models import Q

        from apps.work.faction.models import FactionAttendance, FactionMeeting
        from apps.work.motions.models import Motion, MotionComment
        from apps.work.tasks.models import Task

        # Tasks
        my_tasks = Task.objects.filter(organization=org).filter(Q(created_by=membership) | Q(assigned_to=membership))
        context["tasks_total"] = my_tasks.count()
        context["tasks_completed"] = my_tasks.filter(is_completed=True).count()
        context["tasks_open"] = my_tasks.filter(is_completed=False).count()

        # Motions
        context["motions_authored"] = Motion.objects.filter(organization=org, author=membership).count()

        # Motion comments
        context["motion_comments"] = MotionComment.objects.filter(motion__organization=org, author=membership).count()

        # Faction meetings
        attendance_qs = FactionAttendance.objects.filter(membership=membership, meeting__organization=org)
        context["meetings_present"] = attendance_qs.filter(status="present").count()
        context["meetings_excused"] = attendance_qs.filter(status="excused").count()
        context["meetings_absent"] = attendance_qs.filter(status="absent").count()
        context["meetings_total"] = FactionMeeting.objects.filter(organization=org, status="completed").count()

        # Meeting preparations
        from apps.work.meetings.models import AgendaItemNote, MeetingPreparation

        context["preparations"] = MeetingPreparation.objects.filter(organization=org, membership=membership).count()

        context["agenda_notes"] = AgendaItemNote.objects.filter(organization=org, author=membership).count()

        # --- Timeline (last 20 activities) ---
        timeline = []

        # Recent tasks (created or completed)
        recent_tasks = my_tasks.order_by("-updated_at")[:5]
        for t in recent_tasks:
            timeline.append(
                {
                    "date": t.updated_at,
                    "icon": "check-square",
                    "color": "green" if t.is_completed else "blue",
                    "title": f"Aufgabe: {t.title}",
                    "detail": "Erledigt" if t.is_completed else f"Status: {t.get_status_display()}",
                }
            )

        # Recent motions
        recent_motions = Motion.objects.filter(organization=org, author=membership).order_by("-updated_at")[:5]
        for m in recent_motions:
            timeline.append(
                {
                    "date": m.updated_at,
                    "icon": "file-text",
                    "color": "indigo",
                    "title": f"Antrag: {m.title}",
                    "detail": m.get_status_display(),
                }
            )

        # Recent attendance
        recent_attendance = (
            FactionAttendance.objects.filter(membership=membership, meeting__organization=org)
            .select_related("meeting")
            .order_by("-meeting__start")[:5]
        )
        for a in recent_attendance:
            timeline.append(
                {
                    "date": a.meeting.start if a.meeting.start else a.meeting.created_at,
                    "icon": "users",
                    "color": "purple",
                    "title": f"Sitzung: {a.meeting.title}",
                    "detail": a.get_status_display(),
                }
            )

        # Recent meeting preparations
        recent_preps = MeetingPreparation.objects.filter(organization=org, membership=membership).order_by(
            "-updated_at"
        )[:5]
        for p in recent_preps:
            timeline.append(
                {
                    "date": p.updated_at,
                    "icon": "clipboard-check",
                    "color": "amber",
                    "title": "Sitzungsvorbereitung",
                    "detail": f"Aktualisiert am {p.updated_at.strftime('%d.%m.%Y')}",
                }
            )

        # Sort by date descending, take top 20
        timeline.sort(key=lambda x: x["date"] if x["date"] else timezone.now(), reverse=True)
        context["timeline"] = timeline[:20]

        return context
