# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Profil: Benachrichtigungseinstellungen und Abwesenheiten.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.work.notifications.models import NotificationType

from .. import selectors, services
from ..services import ServiceError
from ._helpers import flash_error


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

        prefs = services.ensure_notification_preferences(self.membership)
        context["preferences"] = prefs

        type_lookup = dict(NotificationType.choices)
        context["notification_types"] = [
            {
                "value": val,
                "label": type_lookup[val],
                "in_app_enabled": prefs.is_type_enabled(val, "in_app"),
                "email_enabled": prefs.is_type_enabled(val, "email"),
                "category": category_name,
            }
            for category_name, type_values in self.NOTIFICATION_CATEGORIES.items()
            for val in type_values
            if val in type_lookup
        ]
        return context

    def post(self, request, *args, **kwargs):
        """Update notification preferences."""
        services.save_notification_preferences(self.membership, request.POST)
        messages.success(request, "Benachrichtigungseinstellungen gespeichert.")
        return redirect("work:profile_notifications", org_slug=self.organization.slug)


class ProfileAbsenceView(WorkViewMixin, TemplateView):
    """Absence management within profile tabs."""

    template_name = "work/profile/absence.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "absence"
        context.update(selectors.absence_context(self.organization, self.membership, timezone.now().date()))
        context["available_deputies"] = selectors.available_deputies(self.organization, self.membership)
        return context

    def post(self, request, *args, **kwargs):
        """Handle absence actions."""
        action = request.POST.get("action")
        try:
            if action == "create_absence":
                services.create_absence(self.organization, self.membership, self._absence_input(request))
                messages.success(request, "Abwesenheit eingetragen.")
            elif action == "cancel_absence":
                services.cancel_absence(self.organization, self.membership, request.POST.get("absence_id"))
                messages.success(request, "Abwesenheit storniert.")
        except ServiceError as exc:
            flash_error(request, exc)
        return redirect("work:profile_absence", org_slug=self.organization.slug)

    @staticmethod
    def _absence_input(request) -> services.AbsenceInput:
        return services.AbsenceInput(
            start_date=request.POST.get("start_date"),
            end_date=request.POST.get("end_date"),
            reason=request.POST.get("reason", "").strip(),
            deputy_id=request.POST.get("deputy_id"),
            auto_decline_meetings=request.POST.get("auto_decline_meetings") == "on",
            notify_deputy=request.POST.get("notify_deputy") == "on",
        )
