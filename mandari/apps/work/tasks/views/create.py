# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Task views for the Work module.

Provides Kanban-style task management with:
- 3-column board (TODO, In Progress, Done)
- Drag & drop reordering
- Slide-over panel with auto-save + explicit save
- Checklists, attachments, labels, activity feed
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from ..activity import log_activity
from ..forms import (
    TaskForm,
)
from ..models import Task, TaskShare

logger = logging.getLogger(__name__)


class TaskCreateView(WorkViewMixin, TemplateView):
    """Create a new task."""

    template_name = "work/tasks/create.html"
    permission_required = "tasks.create"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "tasks"
        context["form"] = TaskForm(organization=self.organization)

        # Prefill: Aufgabe aus einem Dokument heraus erstellen (?related_motion=)
        related_motion_id = self.request.GET.get("related_motion")
        if related_motion_id:
            from apps.work.motions.models import Motion

            try:
                context["related_motion"] = Motion.objects.get(id=related_motion_id, organization=self.organization)
            except (Motion.DoesNotExist, ValueError):
                pass

        from_protocol = self.request.GET.get("from_protocol")
        if from_protocol:
            from apps.work.faction.models import FactionProtocolEntry

            try:
                entry = FactionProtocolEntry.objects.get(id=from_protocol, meeting__organization=self.organization)
                context["form"] = TaskForm(
                    organization=self.organization,
                    initial={
                        "title": entry.content[:500] if entry.content else "",
                        "assigned_to": entry.action_assignee,
                        "due_date": entry.action_due_date,
                    },
                )
                context["from_protocol_entry"] = entry
            except FactionProtocolEntry.DoesNotExist:
                pass

        return context

    def post(self, request, *args, **kwargs):
        form = TaskForm(request.POST, organization=self.organization)

        if form.is_valid():
            task = form.save(commit=False)
            task.organization = self.organization
            task.created_by = self.membership
            if not task.assigned_to:
                task.assigned_to = self.membership

            # Verknüpfung mit Dokument (Prefill aus dem Editor)
            related_motion_id = request.POST.get("related_motion")
            if related_motion_id:
                from apps.work.motions.models import Motion

                try:
                    task.related_motion = Motion.objects.get(id=related_motion_id, organization=self.organization)
                except (Motion.DoesNotExist, ValueError):
                    pass

            task.position = Task.objects.filter(organization=self.organization, status=task.status).count()

            task.save()
            log_activity(task, self.membership, "created")

            if task.assigned_to and task.assigned_to != self.membership:
                from apps.work.notifications.services import NotificationHub

                NotificationHub.notify_task_assigned(task, task.assigned_to, self.membership)

            messages.success(request, "Aufgabe erfolgreich erstellt.")
            return redirect("work:tasks", org_slug=self.organization.slug)

        context = self.get_context_data()
        context["form"] = form
        return self.render_to_response(context)


class TaskShareView(WorkViewMixin, View):
    """Handle task visibility and sharing."""

    permission_required = "tasks.manage"

    def post(self, request, *args, **kwargs):
        task = get_object_or_404(Task, id=kwargs.get("task_id"), organization=self.organization)

        can_edit = (
            task.created_by == self.membership
            or task.assigned_to == self.membership
            or self.membership.has_permission("tasks.manage")
        )
        if not can_edit:
            messages.error(request, "Keine Berechtigung.")
            return redirect("work:tasks", org_slug=self.organization.slug)

        new_visibility = request.POST.get("visibility", "private")
        if new_visibility in ["private", "shared", "organization"]:
            task.visibility = new_visibility
            task.save(update_fields=["visibility"])

        if new_visibility == "shared":
            share_with_ids = request.POST.getlist("share_with[]")
            TaskShare.objects.filter(task=task).exclude(membership_id__in=share_with_ids).delete()
            for member_id in share_with_ids:
                TaskShare.objects.get_or_create(
                    task=task, membership_id=member_id, defaults={"shared_by": self.membership}
                )
        else:
            TaskShare.objects.filter(task=task).delete()

        messages.success(request, "Sichtbarkeit aktualisiert.")
        return redirect("work:tasks", org_slug=self.organization.slug)
