# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kanban-Board und Board-API für Aufgaben (Work-Modul).

Views parsen die Anfrage und prüfen Berechtigungen; Fachlogik liegt in
``apps.work.tasks.services`` und ``apps.work.tasks.selectors``.
"""

import json
import logging

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..forms import QuickTaskForm
from ..models import Task

logger = logging.getLogger(__name__)

# Platzhalter-UUID in der Panel-URL, die das Frontend clientseitig durch die Aufgaben-ID ersetzt
TASK_URL_PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


class TaskListView(WorkViewMixin, TemplateView):
    """Kanban board view for tasks."""

    template_name = "work/tasks/list.html"
    permission_required = "tasks.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "tasks"

        params = self.request.GET
        view_mode = params.get("view", "my")
        show_completed = params.get("completed", "0") == "1"
        search = params.get("q", "").strip()
        priority_filter = params.get("priority", "")
        label_filter = params.get("label", "")
        assignee_filter = params.get("assignee", "")
        overdue_only = params.get("overdue", "0") == "1"

        base = selectors.card_queryset(self.organization)
        if view_mode == "my":
            visible_tasks = selectors.own_tasks(self.membership, base=base)
            context["view_mode"] = "my"
        else:
            visible_tasks = selectors.visible_tasks(self.organization, self.membership, base=base)
            context["view_mode"] = "all"

        # Statistiken auf sichtbarkeitsgefilterter Basis, bevor Such-/Detailfilter greifen (Issue #6)
        tasks = selectors.apply_board_filters(
            visible_tasks,
            search=search,
            priority=priority_filter,
            label_id=label_filter,
            assignee_id=assignee_filter,
            overdue_only=overdue_only,
        )
        if search:
            context["search_query"] = search
        if priority_filter:
            context["priority_filter"] = priority_filter
        if label_filter:
            context["label_filter"] = label_filter
        if assignee_filter:
            context["assignee_filter"] = assignee_filter
        if overdue_only:
            context["overdue_only"] = True

        columns = selectors.board_columns(tasks, show_completed=show_completed)
        context["todo_tasks"] = columns["todo"]
        context["in_progress_tasks"] = columns["in_progress"]
        context["done_tasks"] = columns["done"]
        context["show_completed"] = show_completed
        context["stats"] = selectors.board_stats(visible_tasks)

        context["quick_form"] = QuickTaskForm()
        context["members"] = selectors.active_members(self.organization)
        context["priority_choices"] = Task.PRIORITY_CHOICES
        context["labels"] = selectors.labels_for_organization(self.organization)
        context["auto_open_task_id"] = params.get("open", "")
        # Import (Datei/Protokolle) nur mit Erstell-Berechtigung anbieten
        context["can_import"] = self.has_permission("tasks.create")

        # Konfiguration der Alpine-Komponenten kanbanBoard/importManager/fileImportManager
        # (frontend/alpine/task-board.ts, task-import.ts), geht per json_script ins Template;
        # die Panel-URL trägt eine Platzhalter-UUID, die das Frontend ersetzt.
        org_kwargs = {"org_slug": self.organization.slug}
        context["board_config"] = {
            "autoOpenTaskId": context["auto_open_task_id"],
            "urls": {
                "api": reverse("work:tasks_api", kwargs=org_kwargs),
                "panel": reverse("work:task_panel", kwargs={**org_kwargs, "task_id": TASK_URL_PLACEHOLDER}),
                "labels": reverse("work:task_labels", kwargs=org_kwargs),
                "importProtocol": reverse("work:tasks_import", kwargs=org_kwargs),
                "importFile": reverse("work:tasks_import_file", kwargs=org_kwargs),
            },
        }

        return context


class TaskBoardAPIView(WorkViewMixin, View):
    """API endpoint for Kanban board operations."""

    permission_required = "tasks.view"

    def post(self, request, *args, **kwargs):
        content_type = request.content_type or ""
        if "application/json" in content_type:
            try:
                data = json.loads(request.body)
                action = data.get("action", "move")
            except json.JSONDecodeError:
                return JsonResponse({"error": "Ungültiges JSON."}, status=400)

            if action == "move":
                return self._move_task(data)
            return JsonResponse({"error": "Unknown action"}, status=400)

        action = request.POST.get("action")

        if action == "quick_add":
            if not self.membership.has_permission("tasks.create"):
                return JsonResponse({"error": "Keine Berechtigung."}, status=403)
            return self._quick_add(request)
        if action == "update_status":
            return self._update_status(request)
        if action == "toggle_complete":
            return self._toggle_complete(request)

        return JsonResponse({"error": "Unknown action"}, status=400)

    def _load_task(self, task_id):
        """Aufgabe der Organisation laden; ``None``, wenn das Mitglied sie nicht ändern darf."""
        task = get_object_or_404(Task, id=task_id, organization=self.organization)
        if not services.can_edit_task(task, self.membership):
            return None
        return task

    def _move_task(self, data):
        try:
            task_id = data.get("task_id")
            new_status = data.get("status")
            new_position = data.get("position", 0)

            if not task_id or not new_status:
                return JsonResponse({"error": "task_id und status erforderlich."}, status=400)
            if new_status not in services.VALID_STATUSES:
                return JsonResponse({"error": "Ungültiger Status."}, status=400)

            task = self._load_task(task_id)
            if task is None:
                return JsonResponse({"error": "Keine Berechtigung."}, status=403)

            services.move_task(task, self.membership, new_status=new_status, new_position=new_position)
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

        task = services.quick_add_task(
            self.organization, self.membership, title=title, status=status, priority=priority
        )

        if self.is_htmx:
            context = {"task": task, "organization": self.organization}
            card_html = render_to_string("work/tasks/_card.html", context, request=request)
            counts = selectors.column_counts(self.organization)
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
        task = self._load_task(request.POST.get("task_id"))
        if task is None:
            return JsonResponse({"error": "Keine Berechtigung."}, status=403)
        services.set_status(task, request.POST.get("status"))
        return JsonResponse({"success": True})

    def _toggle_complete(self, request):
        task = self._load_task(request.POST.get("task_id"))
        if task is None:
            return JsonResponse({"error": "Keine Berechtigung."}, status=403)
        services.toggle_completion(task, self.membership, reopen_status="todo", record_activity=False)
        return JsonResponse({"success": True, "is_completed": task.is_completed, "status": task.status})
