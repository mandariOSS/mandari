# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Label-Verwaltung und Protokoll-Import für Aufgaben (Work-Modul).

Views parsen die Anfrage und prüfen Berechtigungen; Fachlogik liegt in
``apps.work.tasks.services`` und ``apps.work.tasks.selectors``.
"""

import logging

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.generic import View

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..forms import TaskLabelForm
from ..models import TaskLabel

logger = logging.getLogger(__name__)


class TaskLabelManageView(WorkViewMixin, View):
    """Manage organization labels (create, delete)."""

    permission_required = "tasks.manage"

    def get(self, request, *args, **kwargs):
        """Return labels as JSON."""
        return JsonResponse({"labels": selectors.label_rows(self.organization)})

    def post(self, request, *args, **kwargs):
        """Create a new label."""
        form = TaskLabelForm(request.POST)
        if form.is_valid():
            label = services.save_label(self.organization, form.save(commit=False))
            return JsonResponse({"success": True, "id": str(label.id), "name": label.name, "color": label.color})
        return JsonResponse({"error": "Ungültige Daten."}, status=400)

    def delete(self, request, *args, **kwargs):
        """Delete a label."""
        label = get_object_or_404(TaskLabel, id=kwargs.get("label_id"), organization=self.organization)
        services.delete_label(label)
        return JsonResponse({"success": True})


class TaskImportView(WorkViewMixin, View):
    """Import tasks from faction protocol entries."""

    permission_required = "tasks.create"

    def get(self, request, *args, **kwargs):
        action_items = selectors.open_protocol_action_items(self.organization)
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
        created = services.import_protocol_entries(
            self.organization, self.membership, request.POST.getlist("entry_ids[]")
        )
        return JsonResponse({"success": True, "created": created})
