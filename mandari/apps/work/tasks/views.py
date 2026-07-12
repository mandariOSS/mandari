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
import mimetypes

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from .activity import log_activity, log_field_change
from .forms import (
    QuickTaskForm,
    TaskAttachmentForm,
    TaskChecklistItemForm,
    TaskForm,
    TaskLabelForm,
    TaskPanelForm,
)
from .models import Task, TaskActivity, TaskAttachment, TaskChecklistItem, TaskLabel, TaskShare

logger = logging.getLogger(__name__)


def _task_base_queryset():
    """Base queryset with standard select_related."""
    return Task.objects.select_related(
        "assigned_to__user",
        "created_by__user",
        "related_meeting",
        "related_motion",
        "related_faction_meeting",
    )


def _task_panel_queryset():
    """Queryset with all relations needed for the panel."""
    return _task_base_queryset().prefetch_related(
        "labels",
        "checklist_items",
        "attachments",
        Prefetch(
            "activities",
            queryset=TaskActivity.objects.select_related("actor__user").order_by("created_at"),
        ),
    )


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


class TaskPanelView(WorkViewMixin, TemplateView):
    """Panel view for a task (HTMX fragment). Inline-editable when can_edit."""

    template_name = "work/tasks/_panel.html"
    permission_required = "tasks.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        task = get_object_or_404(
            _task_panel_queryset(),
            id=kwargs.get("task_id"),
            organization=self.organization,
        )
        # Sichtbarkeit prüfen: private/geteilte Aufgaben dürfen nur von
        # Zugriffsberechtigten geöffnet werden (die Listenansicht blendet sie
        # aus - das Panel muss dieselbe Grenze ziehen).
        if not task.can_access(self.membership):
            raise PermissionDenied("Kein Zugriff auf diese Aufgabe.")
        context["task"] = task
        can_edit = (
            task.created_by == self.membership
            or task.assigned_to == self.membership
            or self.membership.has_permission("tasks.manage")
        )
        context["can_edit"] = can_edit
        if can_edit:
            context["form"] = TaskPanelForm(instance=task, organization=self.organization)
        context["checklist_items"] = task.checklist_items.all()
        context["attachments"] = task.attachments.all()
        context["activities"] = task.activities.all()
        context["available_labels"] = TaskLabel.objects.filter(organization=self.organization)
        context["task_label_ids"] = list(task.labels.values_list("id", flat=True))
        context["checklist_form"] = TaskChecklistItemForm()
        context["shared_members"] = TaskShare.objects.filter(task=task).select_related("membership__user")
        return context


class TaskPanelActionView(WorkViewMixin, View):
    """Central POST handler for all panel actions."""

    permission_required = "tasks.view"

    def _get_column_counts(self):
        qs = Task.objects.filter(organization=self.organization)
        return {
            "todo_count": qs.filter(status="todo").count(),
            "in_progress_count": qs.filter(status="in_progress").count(),
            "done_count": qs.filter(status="done").count(),
        }

    def _panel_context(self, task):
        """Build full panel context."""
        can_edit = (
            task.created_by == self.membership
            or task.assigned_to == self.membership
            or self.membership.has_permission("tasks.manage")
        )
        context = {
            "task": task,
            "can_edit": can_edit,
            "organization": self.organization,
            "membership": self.membership,
            "checklist_items": task.checklist_items.all(),
            "attachments": task.attachments.all(),
            "activities": task.activities.select_related("actor__user").order_by("created_at"),
            "available_labels": TaskLabel.objects.filter(organization=self.organization),
            "task_label_ids": list(task.labels.values_list("id", flat=True)),
            "checklist_form": TaskChecklistItemForm(),
            "shared_members": TaskShare.objects.filter(task=task).select_related("membership__user"),
        }
        if can_edit:
            context["form"] = TaskPanelForm(instance=task, organization=self.organization)
        return context

    def _render_panel(self, task):
        context = self._panel_context(task)
        return render_to_string("work/tasks/_panel.html", context, request=self.request)

    def _render_oob_card(self, task):
        # Reload with prefetch for card rendering
        task = _task_base_queryset().prefetch_related("labels", "checklist_items", "attachments").get(id=task.id)
        context = {"task": task, "organization": self.organization}
        return render_to_string("work/tasks/_card_oob.html", context, request=self.request)

    def _render_oob_counts(self):
        context = self._get_column_counts()
        return render_to_string("work/tasks/_column_counts_oob.html", context, request=self.request)

    def _render_checklist(self, task):
        context = {
            "task": task,
            "checklist_items": task.checklist_items.all(),
            "checklist_form": TaskChecklistItemForm(),
            "can_edit": True,
            "organization": self.organization,
        }
        return render_to_string("work/tasks/_panel_checklist.html", context, request=self.request)

    def _render_attachments(self, task):
        context = {
            "task": task,
            "attachments": task.attachments.all(),
            "can_edit": True,
            "organization": self.organization,
        }
        return render_to_string("work/tasks/_panel_attachments.html", context, request=self.request)

    def _render_labels(self, task):
        context = {
            "task": task,
            "available_labels": TaskLabel.objects.filter(organization=self.organization),
            "task_label_ids": list(task.labels.values_list("id", flat=True)),
            "can_edit": True,
            "organization": self.organization,
        }
        return render_to_string("work/tasks/_panel_labels.html", context, request=self.request)

    def _render_activity(self, task):
        context = {
            "task": task,
            "activities": task.activities.select_related("actor__user").order_by("created_at"),
            "organization": self.organization,
        }
        return render_to_string("work/tasks/_panel_activity.html", context, request=self.request)

    def _make_response(self, html, toast_message, toast_type="success"):
        response = HttpResponse(html)
        response["HX-Trigger"] = json.dumps({"show-toast": {"message": toast_message, "type": toast_type}})
        return response

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        task_id = kwargs.get("task_id")
        task = get_object_or_404(
            _task_base_queryset(),
            id=task_id,
            organization=self.organization,
        )

        # Zugriffsgrenze wie in der Listenansicht: ohne can_access keine Aktion
        # (schützt insb. add_comment auf privaten/fremden Aufgaben).
        if not task.can_access(self.membership):
            return HttpResponse(status=403)

        can_edit = (
            task.created_by == self.membership
            or task.assigned_to == self.membership
            or self.membership.has_permission("tasks.manage")
        )

        if action == "update":
            return self._handle_update(request, task, can_edit)
        elif action == "save":
            return self._handle_save(request, task, can_edit)
        elif action == "add_comment":
            return self._handle_add_comment(request, task)
        elif action == "toggle_complete":
            return self._handle_toggle_complete(request, task, can_edit)
        elif action == "delete":
            return self._handle_delete(request, task, task_id, can_edit)
        elif action == "upload_attachment":
            return self._handle_upload_attachment(request, task, can_edit)
        elif action == "delete_attachment":
            return self._handle_delete_attachment(request, task, can_edit)
        elif action == "add_checklist_item":
            return self._handle_add_checklist_item(request, task, can_edit)
        elif action == "toggle_checklist_item":
            return self._handle_toggle_checklist_item(request, task, can_edit)
        elif action == "delete_checklist_item":
            return self._handle_delete_checklist_item(request, task, can_edit)
        elif action == "toggle_label":
            return self._handle_toggle_label(request, task, can_edit)
        elif action == "reorder_checklist":
            return self._handle_reorder_checklist(request, task, can_edit)

        return HttpResponse(status=400)

    def _capture_old_values(self, task):
        """Capture old values before update for activity logging."""
        return {
            "status": task.get_status_display(),
            "priority": task.get_priority_display(),
            "due_date": str(task.due_date) if task.due_date else "—",
            "assigned_to": task.assigned_to.user.get_display_name() if task.assigned_to else "—",
            "visibility": task.get_visibility_display(),
            "status_raw": task.status,
            "priority_raw": task.priority,
            "due_date_raw": task.due_date,
            "assigned_to_raw": task.assigned_to_id,
            "visibility_raw": task.visibility,
        }

    def _log_changes(self, task, old_values):
        """Log field changes as activities and notify affected members."""
        from apps.work.notifications.services import NotificationHub

        changes = []
        if old_values["status_raw"] != task.status:
            if task.status == "done":
                log_activity(task, self.membership, "completed")
                NotificationHub.notify_task_completed(task, self.membership)
            elif old_values["status_raw"] == "done":
                log_activity(task, self.membership, "reopened")
            else:
                changes.append(("status", old_values["status"], task.get_status_display(), "status_changed"))

        if (
            old_values["assigned_to_raw"] != task.assigned_to_id
            and task.assigned_to
            and task.assigned_to != self.membership
        ):
            NotificationHub.notify_task_assigned(task, task.assigned_to, self.membership)
        if old_values["priority_raw"] != task.priority:
            changes.append(("priority", old_values["priority"], task.get_priority_display(), "priority_changed"))
        if old_values["due_date_raw"] != task.due_date:
            new_due = str(task.due_date) if task.due_date else "—"
            changes.append(("due_date", old_values["due_date"], new_due, "due_date_changed"))
        if old_values["assigned_to_raw"] != task.assigned_to_id:
            new_assigned = task.assigned_to.user.get_display_name() if task.assigned_to else "—"
            changes.append(("assigned_to", old_values["assigned_to"], new_assigned, "assigned"))
        if old_values["visibility_raw"] != task.visibility:
            changes.append(
                ("visibility", old_values["visibility"], task.get_visibility_display(), "visibility_changed")
            )

        for field_name, old_val, new_val, activity_type in changes:
            log_field_change(task, self.membership, field_name, old_val, new_val, activity_type)

    def _handle_update(self, request, task, can_edit):
        """Auto-save: hx-swap=none, only OOB card + counts."""
        if not can_edit:
            return HttpResponse(status=403)

        old_values = self._capture_old_values(task)
        form = TaskPanelForm(request.POST, instance=task, organization=self.organization)
        if form.is_valid():
            updated_task = form.save(commit=False)
            if updated_task.status == "done" and not updated_task.is_completed:
                updated_task.is_completed = True
                updated_task.completed_at = timezone.now()
            elif updated_task.status != "done" and updated_task.is_completed:
                updated_task.is_completed = False
                updated_task.completed_at = None
            updated_task.save()

            self._log_changes(updated_task, old_values)

            # Reload for rendering
            task = _task_base_queryset().prefetch_related("labels", "checklist_items", "attachments").get(id=task.id)
            html = self._render_oob_card(task)
            html += self._render_oob_counts()
            return HttpResponse(html)
        else:
            # Validation error: re-render full panel
            task = _task_panel_queryset().get(id=task.id)
            context = self._panel_context(task)
            context["form"] = form
            html = render_to_string("work/tasks/_panel.html", context, request=request)
            response = HttpResponse(html)
            response["HX-Reswap"] = "innerHTML"
            response["HX-Retarget"] = "#task-panel-container"
            response["HX-Trigger"] = json.dumps({"show-toast": {"message": "Fehler beim Speichern.", "type": "error"}})
            return response

    def _handle_save(self, request, task, can_edit):
        """Explicit save button: re-render full panel."""
        if not can_edit:
            return HttpResponse(status=403)

        old_values = self._capture_old_values(task)
        form = TaskPanelForm(request.POST, instance=task, organization=self.organization)
        if form.is_valid():
            updated_task = form.save(commit=False)
            if updated_task.status == "done" and not updated_task.is_completed:
                updated_task.is_completed = True
                updated_task.completed_at = timezone.now()
            elif updated_task.status != "done" and updated_task.is_completed:
                updated_task.is_completed = False
                updated_task.completed_at = None
            updated_task.save()

            self._log_changes(updated_task, old_values)

            # Re-render full panel + OOB card + counts
            task = _task_panel_queryset().get(id=task.id)
            html = self._render_panel(task)
            html += self._render_oob_card(task)
            html += self._render_oob_counts()
            return self._make_response(html, "Gespeichert.")
        else:
            task = _task_panel_queryset().get(id=task.id)
            context = self._panel_context(task)
            context["form"] = form
            html = render_to_string("work/tasks/_panel.html", context, request=request)
            return self._make_response(html, "Fehler beim Speichern.", "error")

    def _handle_add_comment(self, request, task):
        content = request.POST.get("content", "").strip()
        if not content:
            return HttpResponse(status=400)

        activity = log_activity(task, self.membership, "comment", content=content)

        from apps.work.notifications.services import NotificationHub

        NotificationHub.notify_task_comment(task, activity, self.membership)

        html = self._render_activity(task)
        return self._make_response(html, "Kommentar hinzugefügt.")

    def _handle_toggle_complete(self, request, task, can_edit):
        if not can_edit:
            return HttpResponse(status=403)

        if task.is_completed:
            task.is_completed = False
            task.completed_at = None
            task.status = "in_progress"
            msg = "Aufgabe wieder geöffnet."
            log_activity(task, self.membership, "reopened")
        else:
            task.is_completed = True
            task.completed_at = timezone.now()
            task.status = "done"
            msg = "Aufgabe als erledigt markiert."
            log_activity(task, self.membership, "completed")
        task.save()

        if task.is_completed:
            from apps.work.notifications.services import NotificationHub

            NotificationHub.notify_task_completed(task, self.membership)

        task = _task_panel_queryset().get(id=task.id)
        html = self._render_panel(task)
        html += self._render_oob_card(task)
        html += self._render_oob_counts()
        return self._make_response(html, msg)

    def _handle_delete(self, request, task, task_id, can_edit):
        if not can_edit:
            return HttpResponse(status=403)

        card_id = f"task-card-{task.id}"
        task.delete()

        oob_delete = f'<div id="{card_id}" hx-swap-oob="delete"></div>'
        html = oob_delete + self._render_oob_counts()
        response = HttpResponse(html)
        response["HX-Trigger"] = json.dumps(
            {
                "show-toast": {"message": "Aufgabe gelöscht.", "type": "success"},
                "taskDeleted": {"taskId": str(task_id)},
            }
        )
        return response

    def _handle_upload_attachment(self, request, task, can_edit):
        if not can_edit:
            return HttpResponse(status=403)

        form = TaskAttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            attachment = form.save(commit=False)
            attachment.task = task
            attachment.uploaded_by = self.membership
            f = request.FILES["file"]
            attachment.filename = f.name
            attachment.mime_type = f.content_type or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            attachment.file_size = f.size
            attachment.save()

            log_activity(task, self.membership, "attachment_added", details={"filename": f.name})

            html = self._render_attachments(task)
            # Also update activity section via OOB
            activity_html = (
                f'<div id="panel-activity" hx-swap-oob="innerHTML:#panel-activity">{self._render_activity(task)}</div>'
            )
            return self._make_response(html + activity_html, f'"{f.name}" hochgeladen.')

        return self._make_response("", "Fehler beim Hochladen.", "error")

    def _handle_delete_attachment(self, request, task, can_edit):
        if not can_edit:
            return HttpResponse(status=403)

        attachment_id = request.POST.get("attachment_id")
        attachment = get_object_or_404(TaskAttachment, id=attachment_id, task=task)
        filename = attachment.filename
        attachment.file.delete(save=False)
        attachment.delete()

        log_activity(task, self.membership, "attachment_removed", details={"filename": filename})

        html = self._render_attachments(task)
        activity_html = (
            f'<div id="panel-activity" hx-swap-oob="innerHTML:#panel-activity">{self._render_activity(task)}</div>'
        )
        return self._make_response(html + activity_html, f'"{filename}" entfernt.')

    def _handle_add_checklist_item(self, request, task, can_edit):
        if not can_edit:
            return HttpResponse(status=403)

        form = TaskChecklistItemForm(request.POST)
        if form.is_valid():
            item = form.save(commit=False)
            item.task = task
            item.position = task.checklist_items.count()
            item.save()

            log_activity(task, self.membership, "checklist_item_added", details={"title": item.title})

            html = self._render_checklist(task)
            activity_html = (
                f'<div id="panel-activity" hx-swap-oob="innerHTML:#panel-activity">{self._render_activity(task)}</div>'
            )
            # OOB card update for checklist progress
            oob_card = self._render_oob_card(task)
            return self._make_response(html + activity_html + oob_card, "Punkt hinzugefügt.")

        return HttpResponse(status=400)

    def _handle_toggle_checklist_item(self, request, task, can_edit):
        if not can_edit:
            return HttpResponse(status=403)

        item_id = request.POST.get("item_id")
        item = get_object_or_404(TaskChecklistItem, id=item_id, task=task)
        item.is_completed = not item.is_completed
        item.save(update_fields=["is_completed"])

        if item.is_completed:
            log_activity(task, self.membership, "checklist_item_completed", details={"title": item.title})
        else:
            log_activity(task, self.membership, "checklist_item_unchecked", details={"title": item.title})

        html = self._render_checklist(task)
        oob_card = self._render_oob_card(task)
        return HttpResponse(html + oob_card)

    def _handle_delete_checklist_item(self, request, task, can_edit):
        if not can_edit:
            return HttpResponse(status=403)

        item_id = request.POST.get("item_id")
        item = get_object_or_404(TaskChecklistItem, id=item_id, task=task)
        item.delete()

        html = self._render_checklist(task)
        oob_card = self._render_oob_card(task)
        return HttpResponse(html + oob_card)

    def _handle_toggle_label(self, request, task, can_edit):
        if not can_edit:
            return HttpResponse(status=403)

        label_id = request.POST.get("label_id")
        label = get_object_or_404(TaskLabel, id=label_id, organization=self.organization)

        if task.labels.filter(id=label_id).exists():
            task.labels.remove(label)
            log_activity(task, self.membership, "label_removed", details={"label": label.name})
        else:
            task.labels.add(label)
            log_activity(task, self.membership, "label_added", details={"label": label.name})

        html = self._render_labels(task)
        oob_card = self._render_oob_card(task)
        activity_html = (
            f'<div id="panel-activity" hx-swap-oob="innerHTML:#panel-activity">{self._render_activity(task)}</div>'
        )
        return HttpResponse(html + oob_card + activity_html)

    def _handle_reorder_checklist(self, request, task, can_edit):
        if not can_edit:
            return JsonResponse({"error": "Keine Berechtigung."}, status=403)

        try:
            order = json.loads(request.POST.get("order", "[]"))
            for idx, item_id in enumerate(order):
                TaskChecklistItem.objects.filter(id=item_id, task=task).update(position=idx)
            return JsonResponse({"success": True})
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Ungültige Daten."}, status=400)


class TaskLabelManageView(WorkViewMixin, View):
    """Manage organization labels (create, delete)."""

    permission_required = "tasks.manage"

    def get(self, request, *args, **kwargs):
        """Return labels as JSON."""
        labels = TaskLabel.objects.filter(organization=self.organization).values("id", "name", "color")
        return JsonResponse({"labels": list(labels)})

    def post(self, request, *args, **kwargs):
        """Create a new label."""
        form = TaskLabelForm(request.POST)
        if form.is_valid():
            label = form.save(commit=False)
            label.organization = self.organization
            label.save()
            return JsonResponse({"success": True, "id": str(label.id), "name": label.name, "color": label.color})
        return JsonResponse({"error": "Ungültige Daten."}, status=400)

    def delete(self, request, *args, **kwargs):
        """Delete a label."""
        label_id = kwargs.get("label_id")
        label = get_object_or_404(TaskLabel, id=label_id, organization=self.organization)
        label.delete()
        return JsonResponse({"success": True})


class TaskImportView(WorkViewMixin, View):
    """Import tasks from faction protocol entries."""

    permission_required = "tasks.create"

    def get(self, request, *args, **kwargs):
        from apps.work.faction.models import FactionProtocolEntry

        action_items = (
            FactionProtocolEntry.objects.filter(
                meeting__organization=self.organization,
                entry_type="action",
                action_completed=False,
            )
            .exclude(
                id__in=Task.objects.filter(organization=self.organization).values_list(
                    "related_faction_meeting", flat=True
                )
            )
            .select_related("meeting", "agenda_item", "action_assignee__user")
            .order_by("-created_at")[:50]
        )

        return JsonResponse(
            {
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
            }
        )

    def post(self, request, *args, **kwargs):
        from apps.work.faction.models import FactionProtocolEntry

        entry_ids = request.POST.getlist("entry_ids[]")

        created = 0
        for entry_id in entry_ids:
            try:
                entry = FactionProtocolEntry.objects.get(
                    id=entry_id, meeting__organization=self.organization, entry_type="action"
                )

                task = Task.objects.create(
                    organization=self.organization,
                    title=entry.content[:500] if entry.content else "Protokoll-Aufgabe",
                    created_by=self.membership,
                    assigned_to=entry.action_assignee or self.membership,
                    due_date=entry.action_due_date,
                    status="todo",
                    priority="medium",
                    position=Task.objects.filter(organization=self.organization, status="todo").count(),
                    related_faction_meeting=entry.meeting,
                )
                log_activity(task, self.membership, "created")
                created += 1

            except FactionProtocolEntry.DoesNotExist:
                continue

        return JsonResponse(
            {
                "success": True,
                "created": created,
            }
        )
