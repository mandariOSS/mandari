# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Task views for the Work module.

Provides Kanban-style task management with:
- 3-column board (TODO, In Progress, Done)
- Drag & drop reordering
- Slide-over panel with auto-save + explicit save
- Checklists, attachments, labels, activity feed
"""

import json
import logging

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from ..activity import log_activity, log_field_change
from ..forms import (
    QuickTaskForm,
)
from ..models import Task, TaskLabel

logger = logging.getLogger(__name__)
from ._helpers import _task_base_queryset


class TaskListView(WorkViewMixin, TemplateView):
    """Kanban board view for tasks."""

    template_name = "work/tasks/list.html"
    permission_required = "tasks.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "tasks"

        # Filter parameters
        view_mode = self.request.GET.get("view", "my")
        show_completed = self.request.GET.get("completed", "0") == "1"
        search = self.request.GET.get("q", "").strip()
        priority_filter = self.request.GET.get("priority", "")
        label_filter = self.request.GET.get("label", "")
        assignee_filter = self.request.GET.get("assignee", "")
        overdue_only = self.request.GET.get("overdue", "0") == "1"

        # Base queryset
        tasks = (
            _task_base_queryset()
            .filter(organization=self.organization)
            .prefetch_related("labels", "checklist_items", "attachments")
        )

        # Apply filters based on visibility
        if view_mode == "my":
            tasks = tasks.filter(
                Q(assigned_to=self.membership) | Q(created_by=self.membership) | Q(shares__membership=self.membership)
            ).distinct()
            context["view_mode"] = "my"
        else:
            tasks = tasks.filter(
                Q(visibility="organization")
                | Q(created_by=self.membership)
                | Q(assigned_to=self.membership)
                | Q(shares__membership=self.membership)
            ).distinct()
            context["view_mode"] = "all"

        # Sichtbarkeitsgefilterte Basis für die Statistiken festhalten,
        # bevor Such-/Detailfilter greifen (sonst zählen fremde private
        # Aufgaben in die Kacheln, siehe Issue #6)
        visible_tasks = tasks

        if search:
            tasks = tasks.filter(Q(title__icontains=search) | Q(description__icontains=search))
            context["search_query"] = search

        if priority_filter:
            tasks = tasks.filter(priority=priority_filter)
            context["priority_filter"] = priority_filter

        if label_filter:
            tasks = tasks.filter(labels__id=label_filter)
            context["label_filter"] = label_filter

        if assignee_filter:
            tasks = tasks.filter(assigned_to__id=assignee_filter)
            context["assignee_filter"] = assignee_filter

        if overdue_only:
            tasks = tasks.filter(due_date__lt=timezone.now().date(), status__in=["todo", "in_progress"])
            context["overdue_only"] = True

        # Group by status for Kanban
        context["todo_tasks"] = tasks.filter(status="todo").order_by("position", "-priority", "due_date")
        context["in_progress_tasks"] = tasks.filter(status="in_progress").order_by("position", "-priority", "due_date")

        if show_completed:
            context["done_tasks"] = tasks.filter(status="done").order_by("-completed_at", "position")[:50]
            context["show_completed"] = True
        else:
            context["done_tasks"] = tasks.filter(status="done").order_by("-completed_at")[:10]
            context["show_completed"] = False

        # Statistics — auf Basis der sichtbarkeitsgefilterten Aufgaben,
        # damit keine Zahlen fremder privater Aufgaben auftauchen (Issue #6)
        context["stats"] = {
            "total": visible_tasks.count(),
            "todo": visible_tasks.filter(status="todo").count(),
            "in_progress": visible_tasks.filter(status="in_progress").count(),
            "done": visible_tasks.filter(status="done").count(),
            "overdue": visible_tasks.filter(
                due_date__lt=timezone.now().date(), status__in=["todo", "in_progress"]
            ).count(),
        }

        # Form for quick add
        context["quick_form"] = QuickTaskForm()

        # All members for assignment
        context["members"] = self.organization.memberships.filter(is_active=True).select_related("user")

        context["priority_choices"] = Task.PRIORITY_CHOICES
        context["labels"] = TaskLabel.objects.filter(organization=self.organization)

        # Auto-open panel via URL param
        context["auto_open_task_id"] = self.request.GET.get("open", "")

        # Import (Datei/Protokolle) nur mit Erstell-Berechtigung anbieten
        context["can_import"] = self.has_permission("tasks.create")

        return context


class TaskBoardAPIView(WorkViewMixin, View):
    """API endpoint for Kanban board operations."""

    permission_required = "tasks.view"

    def _can_modify_task(self, task):
        """Prüft ob der User die Aufgabe ändern darf."""
        return (
            task.created_by == self.membership
            or task.assigned_to == self.membership
            or self.membership.has_permission("tasks.manage")
        )

    def post(self, request, *args, **kwargs):
        content_type = request.content_type or ""
        if "application/json" in content_type:
            try:
                data = json.loads(request.body)
                action = data.get("action", "move")
            except json.JSONDecodeError:
                return JsonResponse({"error": "Ungültiges JSON."}, status=400)

            if action == "move":
                return self._move_task(request, data=data)
            return JsonResponse({"error": "Unknown action"}, status=400)

        action = request.POST.get("action")

        if action == "quick_add":
            if not self.membership.has_permission("tasks.create"):
                return JsonResponse({"error": "Keine Berechtigung."}, status=403)
            return self._quick_add(request)
        elif action == "update_status":
            return self._update_status(request)
        elif action == "toggle_complete":
            return self._toggle_complete(request)

        return JsonResponse({"error": "Unknown action"}, status=400)

    def _move_task(self, request, data=None):
        try:
            if data is None:
                data = json.loads(request.body)

            task_id = data.get("task_id")
            new_status = data.get("status")
            new_position = data.get("position", 0)

            if not task_id or not new_status:
                return JsonResponse({"error": "task_id und status erforderlich."}, status=400)

            if new_status not in ("todo", "in_progress", "done"):
                return JsonResponse({"error": "Ungültiger Status."}, status=400)

            task = get_object_or_404(Task, id=task_id, organization=self.organization)

            if not self._can_modify_task(task):
                return JsonResponse({"error": "Keine Berechtigung."}, status=403)

            old_status = task.status
            task.status = new_status
            task.position = new_position

            if new_status == "done" and old_status != "done":
                task.is_completed = True
                task.completed_at = timezone.now()
            elif new_status != "done" and old_status == "done":
                task.is_completed = False
                task.completed_at = None

            task.save()

            # Log activity for status changes
            if old_status != new_status:
                old_label = dict(Task.STATUS_CHOICES).get(old_status, old_status)
                new_label = dict(Task.STATUS_CHOICES).get(new_status, new_status)
                if new_status == "done":
                    log_activity(task, self.membership, "completed")

                    from apps.work.notifications.services import NotificationHub

                    NotificationHub.notify_task_completed(task, self.membership)
                elif old_status == "done":
                    log_activity(task, self.membership, "reopened")
                else:
                    log_field_change(task, self.membership, "status", old_label, new_label, "status_changed")

            # Reorder tasks in the new column
            other_tasks = list(
                Task.objects.filter(organization=self.organization, status=new_status)
                .exclude(id=task_id)
                .order_by("position")
            )

            for idx, t in enumerate(other_tasks):
                correct_pos = idx if idx < new_position else idx + 1
                if t.position != correct_pos:
                    Task.objects.filter(id=t.id).update(position=correct_pos)

            if old_status != new_status:
                old_column_tasks = list(
                    Task.objects.filter(organization=self.organization, status=old_status).order_by("position")
                )
                for idx, t in enumerate(old_column_tasks):
                    if t.position != idx:
                        Task.objects.filter(id=t.id).update(position=idx)

            return JsonResponse(
                {
                    "success": True,
                    "task_id": str(task.id),
                    "status": task.status,
                    "is_completed": task.is_completed,
                }
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"[Tasks] Invalid move request: {e}")
            return JsonResponse({"error": "Ungültige Anfrage."}, status=400)

    def _quick_add(self, request):
        title = request.POST.get("title", "").strip()
        status = request.POST.get("status", "todo")
        priority = request.POST.get("priority", "medium")

        if not title:
            return JsonResponse({"error": "Title required"}, status=400)

        max_pos = Task.objects.filter(organization=self.organization, status=status).count()

        task = Task.objects.create(
            organization=self.organization,
            title=title,
            status=status,
            priority=priority,
            position=max_pos,
            created_by=self.membership,
            assigned_to=self.membership,
        )

        log_activity(task, self.membership, "created")

        if self.is_htmx:
            context = {"task": task, "organization": self.organization}
            card_html = render_to_string("work/tasks/_card.html", context, request=request)
            counts = {
                "todo_count": Task.objects.filter(organization=self.organization, status="todo").count(),
                "in_progress_count": Task.objects.filter(organization=self.organization, status="in_progress").count(),
                "done_count": Task.objects.filter(organization=self.organization, status="done").count(),
            }
            counts_html = render_to_string("work/tasks/_column_counts_oob.html", counts, request=request)
            response = HttpResponse(card_html + counts_html)
            response["HX-Trigger"] = json.dumps({"show-toast": {"message": "Aufgabe erstellt.", "type": "success"}})
            return response

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {
                    "success": True,
                    "task": {
                        "id": str(task.id),
                        "title": task.title,
                        "status": task.status,
                        "priority": task.priority,
                    },
                }
            )

        messages.success(request, "Aufgabe erstellt.")
        return redirect("work:tasks", org_slug=self.organization.slug)

    def _update_status(self, request):
        task_id = request.POST.get("task_id")
        new_status = request.POST.get("status")

        task = get_object_or_404(Task, id=task_id, organization=self.organization)

        if not self._can_modify_task(task):
            return JsonResponse({"error": "Keine Berechtigung."}, status=403)

        old_status = task.status
        task.status = new_status

        if new_status == "done" and old_status != "done":
            task.is_completed = True
            task.completed_at = timezone.now()
        elif new_status != "done":
            task.is_completed = False
            task.completed_at = None

        task.save()

        return JsonResponse({"success": True})

    def _toggle_complete(self, request):
        task_id = request.POST.get("task_id")
        task = get_object_or_404(Task, id=task_id, organization=self.organization)

        if not self._can_modify_task(task):
            return JsonResponse({"error": "Keine Berechtigung."}, status=403)

        if task.is_completed:
            task.is_completed = False
            task.completed_at = None
            task.status = "todo"
        else:
            task.is_completed = True
            task.completed_at = timezone.now()
            task.status = "done"

        task.save()

        return JsonResponse(
            {
                "success": True,
                "is_completed": task.is_completed,
                "status": task.status,
            }
        )
