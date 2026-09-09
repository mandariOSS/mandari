# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben anlegen und teilen (Work-Modul).

Views parsen die Anfrage und prüfen Berechtigungen; Fachlogik liegt in
``apps.work.tasks.services`` und ``apps.work.tasks.selectors``.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..forms import TaskForm
from ..models import Task

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
            motion = selectors.find_motion(self.organization, related_motion_id)
            if motion is not None:
                context["related_motion"] = motion

        from_protocol = self.request.GET.get("from_protocol")
        if from_protocol:
            entry = selectors.find_protocol_entry(self.organization, from_protocol)
            if entry is not None:
                context["form"] = TaskForm(
                    organization=self.organization,
                    initial={
                        "title": entry.content[:500] if entry.content else "",
                        "assigned_to": entry.action_assignee,
                        "due_date": entry.action_due_date,
                    },
                )
                context["from_protocol_entry"] = entry

        return context

    def post(self, request, *args, **kwargs):
        form = TaskForm(request.POST, organization=self.organization)

        if form.is_valid():
            services.create_task(
                form.save(commit=False),
                self.organization,
                self.membership,
                related_motion_id=request.POST.get("related_motion"),
            )
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

        if not services.can_edit_task(task, self.membership):
            messages.error(request, "Keine Berechtigung.")
            return redirect("work:tasks", org_slug=self.organization.slug)

        services.update_visibility(
            task,
            self.membership,
            request.POST.get("visibility", "private"),
            request.POST.getlist("share_with[]"),
        )

        messages.success(request, "Sichtbarkeit aktualisiert.")
        return redirect("work:tasks", org_slug=self.organization.slug)
