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

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.generic import View

from apps.common.mixins import WorkViewMixin

from ..activity import log_activity
from ..forms import (
    TaskLabelForm,
)
from ..models import Task, TaskLabel

logger = logging.getLogger(__name__)


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
