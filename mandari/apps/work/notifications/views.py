# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Notification views for the Work module.
"""

from datetime import datetime

from django.http import JsonResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .models import Notification, NotificationType
from .services import NotificationHub


class NotificationCenterView(WorkViewMixin, TemplateView):
    """Full notification center page."""

    template_name = "work/notifications/center.html"
    permission_required = None  # All members can view their notifications

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "notifications"

        # Get notifications with pagination
        page = int(self.request.GET.get("page", 1))
        per_page = 20
        offset = (page - 1) * per_page

        notifications = Notification.objects.filter(recipient=self.membership).select_related("actor__user")[
            offset : offset + per_page
        ]

        total = Notification.objects.filter(recipient=self.membership).count()

        context["notifications"] = notifications
        context["total_count"] = total
        context["unread_count"] = NotificationHub.get_unread_count(self.membership)
        context["page"] = page
        context["has_more"] = (offset + per_page) < total
        context["notification_types"] = NotificationType.choices

        return context


class NotificationPreferencesView(WorkViewMixin, View):
    """Redirect to profile notification settings."""

    permission_required = None

    def get(self, request, *args, **kwargs):
        return redirect("work:profile_notifications", org_slug=self.organization.slug)

    def post(self, request, *args, **kwargs):
        return redirect("work:profile_notifications", org_slug=self.organization.slug)


class NotificationListPartialView(WorkViewMixin, View):
    """HTMX partial for notification dropdown."""

    permission_required = None

    def get(self, request, *args, **kwargs):
        """Return recent notifications as HTML partial."""
        notifications = Notification.objects.filter(recipient=self.membership).select_related("actor__user")[:10]

        unread_count = NotificationHub.get_unread_count(self.membership)

        from django.template.loader import render_to_string

        html = render_to_string(
            "work/notifications/partials/dropdown_list.html",
            {
                "notifications": notifications,
                "unread_count": unread_count,
                "organization": self.organization,
            },
            request=request,
        )

        return JsonResponse(
            {
                "html": html,
                "unread_count": unread_count,
            }
        )


class NotificationMarkReadView(WorkViewMixin, View):
    """Mark notification(s) as read."""

    permission_required = None

    def post(self, request, *args, **kwargs):
        """Mark notification as read."""
        notification_id = kwargs.get("notification_id")

        if notification_id:
            # Mark single notification
            try:
                notification = Notification.objects.get(
                    id=notification_id,
                    recipient=self.membership,
                )
                notification.mark_as_read()
                # Invalidate count cache
                NotificationHub.invalidate_count_cache(self.membership)
                return JsonResponse({"success": True})
            except Notification.DoesNotExist:
                return JsonResponse({"success": False, "error": "Not found"}, status=404)
        else:
            # Mark all as read (also invalidates cache)
            count = NotificationHub.mark_all_as_read(self.membership)
            return JsonResponse({"success": True, "count": count})


class NotificationCountView(WorkViewMixin, View):
    """Get unread notification count (for polling/SSE)."""

    permission_required = None

    def get(self, request, *args, **kwargs):
        """Return unread count as JSON."""
        count = NotificationHub.get_unread_count(self.membership)
        return JsonResponse({"count": count})


class NotificationLatestView(WorkViewMixin, View):
    """Get latest unread notifications (for polling fallback)."""

    permission_required = None

    def get(self, request, *args, **kwargs):
        """Return latest unread notifications."""
        since = request.GET.get("since")
        limit = min(int(request.GET.get("limit", 5)), 20)

        notifications = Notification.objects.filter(
            recipient=self.membership,
            is_read=False,
        ).select_related("actor__user")

        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                notifications = notifications.filter(created_at__gt=since_dt)
            except (ValueError, TypeError):
                pass

        notifications = notifications[:limit]

        data = []
        for n in notifications:
            data.append(
                {
                    "id": str(n.id),
                    "title": n.title,
                    "message": n.message[:100],
                    "type": n.notification_type,
                    "icon": n.icon,
                    "color": n.color,
                    "link": n.link,
                    "created_at": n.created_at.isoformat(),
                }
            )

        return JsonResponse(
            {
                "notifications": data,
                "count": NotificationHub.get_unread_count(self.membership),
            }
        )
