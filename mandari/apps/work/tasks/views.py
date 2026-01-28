# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Task views for the Work module.

Provides Kanban-style task management with:
- 3-column board (TODO, In Progress, Done)
- Drag & drop reordering
- Quick task creation
- Priority and due date management
"""

import json

from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin
from .forms import TaskForm, QuickTaskForm, TaskCommentForm
from .models import Task, TaskComment


class TaskListView(WorkViewMixin, TemplateView):
    """
    Kanban board view for tasks.

    Displays tasks in three columns: TODO, In Progress, Done
    with drag & drop support.
    """

    template_name = "work/tasks/list.html"
    permission_required = "tasks.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "tasks"

        # Filter parameters
        view_mode = self.request.GET.get("view", "my")  # my, all
        show_completed = self.request.GET.get("completed", "0") == "1"
        search = self.request.GET.get("q", "").strip()
        priority_filter = self.request.GET.get("priority", "")

        # Base queryset
        tasks = Task.objects.filter(
            organization=self.organization
        ).select_related(
            "assigned_to__user",
            "created_by__user",
            "related_meeting",
            "related_motion",
            "related_faction_meeting"
        )

        # Apply filters
        if view_mode == "my":
            tasks = tasks.filter(
                Q(assigned_to=self.membership) | Q(created_by=self.membership)
            )
            context["view_mode"] = "my"
        else:
            context["view_mode"] = "all"

        if search:
            tasks = tasks.filter(
                Q(title__icontains=search) | Q(description__icontains=search)
            )
            context["search_query"] = search

        if priority_filter:
            tasks = tasks.filter(priority=priority_filter)
            context["priority_filter"] = priority_filter

        # Group by status for Kanban
        context["todo_tasks"] = tasks.filter(status="todo").order_by("position", "-priority", "due_date")
        context["in_progress_tasks"] = tasks.filter(status="in_progress").order_by("position", "-priority", "due_date")

        if show_completed:
            context["done_tasks"] = tasks.filter(status="done").order_by("-completed_at", "position")[:50]
            context["show_completed"] = True
        else:
            context["done_tasks"] = tasks.filter(status="done").order_by("-completed_at")[:10]
            context["show_completed"] = False

        # Statistics
        all_tasks = Task.objects.filter(organization=self.organization)
        if view_mode == "my":
            all_tasks = all_tasks.filter(
                Q(assigned_to=self.membership) | Q(created_by=self.membership)
            )

        context["stats"] = {
            "total": all_tasks.count(),
            "todo": all_tasks.filter(status="todo").count(),
            "in_progress": all_tasks.filter(status="in_progress").count(),
            "done": all_tasks.filter(status="done").count(),
            "overdue": all_tasks.filter(
                due_date__lt=timezone.now().date(),
                status__in=["todo", "in_progress"]
            ).count(),
        }

        # Form for quick add
        context["quick_form"] = QuickTaskForm()

        # All members for assignment
        context["members"] = self.organization.memberships.filter(
            is_active=True
        ).select_related("user")

        context["priority_choices"] = Task.PRIORITY_CHOICES

        return context


class TaskBoardAPIView(WorkViewMixin, View):
    """API endpoint for Kanban board operations."""

    permission_required = "tasks.manage"

    def post(self, request, *args, **kwargs):
        """Handle various board actions."""
        action = request.POST.get("action")

        if action == "move":
            return self._move_task(request)
        elif action == "quick_add":
            return self._quick_add(request)
        elif action == "update_status":
            return self._update_status(request)
        elif action == "toggle_complete":
            return self._toggle_complete(request)

        return JsonResponse({"error": "Unknown action"}, status=400)

    def _move_task(self, request):
        """Move task to a different status column and position."""
        try:
            data = json.loads(request.body)
            task_id = data.get("task_id")
            new_status = data.get("status")
            new_position = data.get("position", 0)

            task = get_object_or_404(Task, id=task_id, organization=self.organization)

            old_status = task.status
            task.status = new_status
            task.position = new_position

            # If moved to done, mark as completed
            if new_status == "done" and old_status != "done":
                task.is_completed = True
                task.completed_at = timezone.now()
            elif new_status != "done" and old_status == "done":
                task.is_completed = False
                task.completed_at = None

            task.save()

            # Reorder other tasks in the same column
            tasks_in_column = Task.objects.filter(
                organization=self.organization,
                status=new_status
            ).exclude(id=task_id).order_by("position")

            for idx, t in enumerate(tasks_in_column):
                new_pos = idx if idx < new_position else idx + 1
                if t.position != new_pos:
                    t.position = new_pos
                    t.save(update_fields=["position"])

            return JsonResponse({
                "success": True,
                "task_id": str(task.id),
                "status": task.status,
                "is_completed": task.is_completed,
            })

        except (json.JSONDecodeError, KeyError) as e:
            return JsonResponse({"error": str(e)}, status=400)

    def _quick_add(self, request):
        """Quick add a task from the board."""
        title = request.POST.get("title", "").strip()
        status = request.POST.get("status", "todo")
        priority = request.POST.get("priority", "medium")

        if not title:
            return JsonResponse({"error": "Title required"}, status=400)

        # Get max position in the column
        max_pos = Task.objects.filter(
            organization=self.organization,
            status=status
        ).count()

        task = Task.objects.create(
            organization=self.organization,
            title=title,
            status=status,
            priority=priority,
            position=max_pos,
            created_by=self.membership,
            assigned_to=self.membership,
        )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({
                "success": True,
                "task": {
                    "id": str(task.id),
                    "title": task.title,
                    "status": task.status,
                    "priority": task.priority,
                }
            })

        messages.success(request, "Aufgabe erstellt.")
        return redirect("work:tasks", org_slug=self.organization.slug)

    def _update_status(self, request):
        """Update task status."""
        task_id = request.POST.get("task_id")
        new_status = request.POST.get("status")

        task = get_object_or_404(Task, id=task_id, organization=self.organization)
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
        """Toggle task completion."""
        task_id = request.POST.get("task_id")
        task = get_object_or_404(Task, id=task_id, organization=self.organization)

        if task.is_completed:
            task.is_completed = False
            task.completed_at = None
            task.status = "todo"
        else:
            task.is_completed = True
            task.completed_at = timezone.now()
            task.status = "done"

        task.save()

        return JsonResponse({
            "success": True,
            "is_completed": task.is_completed,
            "status": task.status,
        })


class TaskCreateView(WorkViewMixin, TemplateView):
    """Create a new task."""

    template_name = "work/tasks/create.html"
    permission_required = "tasks.create"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "tasks"
        context["form"] = TaskForm(organization=self.organization)

        # Pre-fill from protocol entry if provided
        from_protocol = self.request.GET.get("from_protocol")
        if from_protocol:
            from apps.work.faction.models import FactionProtocolEntry
            try:
                entry = FactionProtocolEntry.objects.get(
                    id=from_protocol,
                    meeting__organization=self.organization
                )
                context["form"] = TaskForm(
                    organization=self.organization,
                    initial={
                        "title": entry.content[:500] if entry.content else "",
                        "assigned_to": entry.action_assignee,
                        "due_date": entry.action_due_date,
                    }
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

            # Set position
            task.position = Task.objects.filter(
                organization=self.organization,
                status=task.status
            ).count()

            task.save()

            messages.success(request, "Aufgabe erfolgreich erstellt.")
            return redirect("work:tasks", org_slug=self.organization.slug)

        context = self.get_context_data()
        context["form"] = form
        return self.render_to_response(context)


class TaskDetailView(WorkViewMixin, TemplateView):
    """Detail view of a task with comments."""

    template_name = "work/tasks/detail.html"
    permission_required = "tasks.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "tasks"

        task = get_object_or_404(
            Task.objects.select_related(
                "assigned_to__user",
                "created_by__user",
                "related_meeting",
                "related_motion",
                "related_faction_meeting",
                "related_agenda_item"
            ),
            id=kwargs.get("task_id"),
            organization=self.organization
        )

        context["task"] = task
        context["comments"] = task.comments.select_related("author__user").order_by("created_at")
        context["comment_form"] = TaskCommentForm()
        context["form"] = TaskForm(instance=task, organization=self.organization)

        # Can edit
        context["can_edit"] = (
            task.created_by == self.membership or
            task.assigned_to == self.membership or
            self.membership.has_permission("tasks.manage")
        )

        return context

    def post(self, request, *args, **kwargs):
        task = get_object_or_404(
            Task,
            id=kwargs.get("task_id"),
            organization=self.organization
        )

        action = request.POST.get("action")

        if action == "update":
            form = TaskForm(request.POST, instance=task, organization=self.organization)
            if form.is_valid():
                updated_task = form.save(commit=False)

                # Handle status -> completion sync
                if updated_task.status == "done" and not updated_task.is_completed:
                    updated_task.is_completed = True
                    updated_task.completed_at = timezone.now()
                elif updated_task.status != "done" and updated_task.is_completed:
                    updated_task.is_completed = False
                    updated_task.completed_at = None

                updated_task.save()
                messages.success(request, "Aufgabe aktualisiert.")
            else:
                messages.error(request, "Fehler beim Speichern.")

        elif action == "comment":
            comment_form = TaskCommentForm(request.POST)
            if comment_form.is_valid():
                comment = comment_form.save(commit=False)
                comment.task = task
                comment.author = self.membership
                comment.save()
                messages.success(request, "Kommentar hinzugefügt.")

        elif action == "delete":
            task.delete()
            messages.success(request, "Aufgabe gelöscht.")
            return redirect("work:tasks", org_slug=self.organization.slug)

        elif action == "toggle_complete":
            if task.is_completed:
                task.is_completed = False
                task.completed_at = None
                task.status = "in_progress"
            else:
                task.is_completed = True
                task.completed_at = timezone.now()
                task.status = "done"
            task.save()
            messages.success(request, "Status aktualisiert.")

        return redirect("work:task_detail", org_slug=self.organization.slug, task_id=task.id)


class TaskImportView(WorkViewMixin, View):
    """Import tasks from faction protocol entries."""

    permission_required = "tasks.create"

    def get(self, request, *args, **kwargs):
        """Show pending action items from protocols."""
        from apps.work.faction.models import FactionProtocolEntry

        # Get action items that haven't been converted to tasks yet
        action_items = FactionProtocolEntry.objects.filter(
            meeting__organization=self.organization,
            entry_type="action",
            action_completed=False,
        ).exclude(
            # Exclude items that already have tasks (need to track this relationship)
            id__in=Task.objects.filter(
                organization=self.organization
            ).values_list("related_faction_meeting", flat=True)
        ).select_related(
            "meeting",
            "agenda_item",
            "action_assignee__user"
        ).order_by("-created_at")[:50]

        return JsonResponse({
            "items": [
                {
                    "id": str(item.id),
                    "content": item.content[:200] if item.content else "",
                    "meeting": item.meeting.title,
                    "meeting_date": item.meeting.start.strftime("%d.%m.%Y") if item.meeting.start else "",
                    "assignee": item.action_assignee.user.get_display_name() if item.action_assignee else None,
                    "due_date": item.action_due_date.strftime("%Y-%m-%d") if item.action_due_date else None,
                }
                for item in action_items
            ]
        })

    def post(self, request, *args, **kwargs):
        """Import selected protocol entries as tasks."""
        from apps.work.faction.models import FactionProtocolEntry

        entry_ids = request.POST.getlist("entry_ids[]")

        created = 0
        for entry_id in entry_ids:
            try:
                entry = FactionProtocolEntry.objects.get(
                    id=entry_id,
                    meeting__organization=self.organization,
                    entry_type="action"
                )

                # Create task
                Task.objects.create(
                    organization=self.organization,
                    title=entry.content[:500] if entry.content else "Protokoll-Aufgabe",
                    created_by=self.membership,
                    assigned_to=entry.action_assignee or self.membership,
                    due_date=entry.action_due_date,
                    status="todo",
                    priority="medium",
                    position=Task.objects.filter(
                        organization=self.organization,
                        status="todo"
                    ).count(),
                    related_faction_meeting=entry.meeting,
                )
                created += 1

            except FactionProtocolEntry.DoesNotExist:
                continue

        return JsonResponse({
            "success": True,
            "created": created,
        })
