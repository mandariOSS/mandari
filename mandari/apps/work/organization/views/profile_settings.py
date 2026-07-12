# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


# =============================================================================
# PROFILE: NOTIFICATIONS TAB
# =============================================================================


class ProfileNotificationsView(WorkViewMixin, TemplateView):
    """Notification preferences within profile tabs."""

    template_name = "work/profile/notifications.html"
    permission_required = "dashboard.view"

    # Notification type categories for grouping
    NOTIFICATION_CATEGORIES = {
        "meetings": ["meeting_reminder", "meeting_updated", "meeting_cancelled"],
        "tasks": ["task_assigned", "task_due_soon", "task_completed", "task_comment"],
        "motions": ["motion_shared", "motion_comment", "motion_status"],
        "faction": ["faction_reminder", "faction_updated"],
        "organization": ["member_joined", "role_changed"],
        "support": [
            "support_created",
            "support_reply",
            "support_status",
            "support_resolved",
            "support_escalated",
        ],
        "system": ["change_request_new", "change_request_decided", "absence_deputy", "system", "announcement"],
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "notifications"

        from apps.work.notifications.models import NotificationPreference, NotificationType

        prefs, _ = NotificationPreference.objects.get_or_create(membership=self.membership)
        context["preferences"] = prefs

        # Build categorized notification types
        type_lookup = dict(NotificationType.choices)
        categorized = []
        for category_name, type_values in self.NOTIFICATION_CATEGORIES.items():
            items = []
            for val in type_values:
                if val in type_lookup:
                    items.append(
                        {
                            "value": val,
                            "label": type_lookup[val],
                            "in_app_enabled": prefs.is_type_enabled(val, "in_app"),
                            "email_enabled": prefs.is_type_enabled(val, "email"),
                            "category": category_name,
                        }
                    )
            if items:
                categorized.extend(items)

        context["notification_types"] = categorized

        return context

    def post(self, request, *args, **kwargs):
        """Update notification preferences."""
        from apps.work.notifications.models import NotificationPreference, NotificationType

        prefs, _ = NotificationPreference.objects.get_or_create(membership=self.membership)

        prefs.email_enabled = request.POST.get("email_enabled") == "on"
        prefs.email_digest = request.POST.get("email_digest", "instant")

        prefs.quiet_hours_enabled = request.POST.get("quiet_hours_enabled") == "on"
        if prefs.quiet_hours_enabled:
            start = request.POST.get("quiet_hours_start")
            end = request.POST.get("quiet_hours_end")
            if start:
                prefs.quiet_hours_start = start
            if end:
                prefs.quiet_hours_end = end

        type_settings = {}
        for ntype, _ in NotificationType.choices:
            type_settings[ntype] = {
                "in_app": request.POST.get(f"type_{ntype}_in_app") == "on",
                "email": request.POST.get(f"type_{ntype}_email") == "on",
            }
        prefs.type_settings = type_settings
        prefs.save()

        messages.success(request, "Benachrichtigungseinstellungen gespeichert.")
        return redirect("work:profile_notifications", org_slug=self.organization.slug)


# =============================================================================
# PROFILE: ABSENCE TAB
# =============================================================================


class ProfileAbsenceView(WorkViewMixin, TemplateView):
    """Absence management within profile tabs."""

    template_name = "work/profile/absence.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "absence"

        from apps.tenants.models import Membership

        from ..models import MemberAbsence

        today = timezone.now().date()

        # My absences
        my_absences = MemberAbsence.objects.filter(
            membership=self.membership,
            organization=self.organization,
        )

        current = my_absences.filter(
            is_active=True,
            start_date__lte=today,
            end_date__gte=today,
        ).first()

        active_absences = (
            my_absences.filter(is_active=True, end_date__gte=today)
            .select_related("deputy__user")
            .order_by("start_date")
        )

        past_absences = (
            my_absences.filter(end_date__lt=today).select_related("deputy__user").order_by("-start_date")[:10]
        )

        # Where I'm deputy
        deputy_for = (
            MemberAbsence.objects.filter(
                deputy=self.membership,
                organization=self.organization,
                is_active=True,
                end_date__gte=today,
            )
            .select_related("membership__user")
            .order_by("start_date")
        )

        # Available deputies (other active members)
        available_deputies = (
            Membership.objects.filter(
                organization=self.organization,
                is_active=True,
            )
            .exclude(id=self.membership.id)
            .select_related("user")
            .order_by("user__first_name", "user__last_name")
        )

        context["current_absence"] = current
        context["active_absences"] = active_absences
        context["past_absences"] = past_absences
        context["deputy_for"] = deputy_for
        context["available_deputies"] = available_deputies

        return context

    def post(self, request, *args, **kwargs):
        """Handle absence actions."""
        action = request.POST.get("action")

        if action == "create_absence":
            return self._create_absence(request)
        elif action == "cancel_absence":
            return self._cancel_absence(request)

        return redirect("work:profile_absence", org_slug=self.organization.slug)

    def _create_absence(self, request):
        from datetime import datetime

        from apps.tenants.models import Membership

        from ..models import MemberAbsence

        start_date = request.POST.get("start_date")
        end_date = request.POST.get("end_date")
        reason = request.POST.get("reason", "").strip()
        deputy_id = request.POST.get("deputy_id")
        auto_decline = request.POST.get("auto_decline_meetings") == "on"
        notify_dep = request.POST.get("notify_deputy") == "on"

        if not start_date or not end_date:
            messages.error(request, "Von- und Bis-Datum sind erforderlich.")
            return redirect("work:profile_absence", org_slug=self.organization.slug)

        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Ungültiges Datumsformat.")
            return redirect("work:profile_absence", org_slug=self.organization.slug)

        if end < start:
            messages.error(request, "Das Enddatum muss nach dem Startdatum liegen.")
            return redirect("work:profile_absence", org_slug=self.organization.slug)

        deputy = None
        if deputy_id:
            deputy = Membership.objects.filter(
                id=deputy_id,
                organization=self.organization,
                is_active=True,
            ).first()

        MemberAbsence.objects.create(
            organization=self.organization,
            membership=self.membership,
            start_date=start,
            end_date=end,
            reason=reason,
            deputy=deputy,
            auto_decline_meetings=auto_decline,
            notify_deputy=notify_dep,
        )

        # Auto-decline faction meetings in the absence period
        if auto_decline:
            self._auto_decline_meetings(start, end)

        # Notify deputy
        if deputy and notify_dep:
            from apps.work.notifications.models import NotificationType
            from apps.work.notifications.services import NotificationHub

            user_name = request.user.get_full_name() or request.user.email
            NotificationHub.send(
                recipient=deputy,
                notification_type=NotificationType.ABSENCE_DEPUTY,
                title="Stellvertretung zugewiesen",
                message=f"{user_name} hat Sie als Stellvertreter eingetragen ({start.strftime('%d.%m.')} – {end.strftime('%d.%m.%Y')}).",
                link=f"/work/{self.organization.slug}/profile/absence/",
                actor=self.membership,
            )

        messages.success(request, "Abwesenheit eingetragen.")
        return redirect("work:profile_absence", org_slug=self.organization.slug)

    def _auto_decline_meetings(self, start_date, end_date):
        """Set FactionAttendance to excused for meetings in the absence period."""
        try:
            from apps.work.faction.models import FactionAttendance, FactionMeeting

            meetings = FactionMeeting.objects.filter(
                organization=self.organization,
                date__range=[start_date, end_date],
                status__in=["draft", "invited", "scheduled"],
            )

            for meeting in meetings:
                FactionAttendance.objects.update_or_create(
                    meeting=meeting,
                    membership=self.membership,
                    defaults={"status": "excused"},
                )
        except Exception as e:
            logger.error(f"Failed to auto-decline meetings: {e}")

    def _cancel_absence(self, request):
        from ..models import MemberAbsence

        absence_id = request.POST.get("absence_id")
        absence = get_object_or_404(
            MemberAbsence,
            id=absence_id,
            membership=self.membership,
            organization=self.organization,
        )
        absence.is_active = False
        absence.save()
        messages.success(request, "Abwesenheit storniert.")
        return redirect("work:profile_absence", org_slug=self.organization.slug)
