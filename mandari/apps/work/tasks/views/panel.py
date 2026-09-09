# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Slide-over-Panel einer Aufgabe (HTMX-Fragmente) und zentrale Aktions-View.

Views parsen die Anfrage und prüfen Berechtigungen; Fachlogik liegt in
``apps.work.tasks.services`` und ``apps.work.tasks.selectors``.
"""

import json
import logging

from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..forms import TaskAttachmentForm, TaskChecklistItemForm, TaskPanelForm
from ..models import TaskAttachment, TaskChecklistItem, TaskLabel

logger = logging.getLogger(__name__)


def panel_context(task, organization, membership) -> dict:
    """Gemeinsamer Template-Kontext für ``work/tasks/_panel.html``."""
    can_edit = services.can_edit_task(task, membership)
    context = {
        "task": task,
        "can_edit": can_edit,
        "organization": organization,
        "membership": membership,
        "checklist_items": task.checklist_items.all(),
        "attachments": task.attachments.all(),
        "activities": selectors.task_activities(task),
        "available_labels": selectors.labels_for_organization(organization),
        "task_label_ids": selectors.task_label_ids(task),
        "checklist_form": TaskChecklistItemForm(),
        "shared_members": selectors.task_shares(task),
    }
    if can_edit:
        context["form"] = TaskPanelForm(instance=task, organization=organization)
    return context


def oob_activity(html: str) -> str:
    """Aktivitätsbereich als Out-of-band-Swap verpacken."""
    return f'<div id="panel-activity" hx-swap-oob="innerHTML:#panel-activity">{html}</div>'


class TaskPanelView(WorkViewMixin, TemplateView):
    """Panel view for a task (HTMX fragment). Inline-editable when can_edit."""

    template_name = "work/tasks/_panel.html"
    permission_required = "tasks.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        task = get_object_or_404(selectors.panel_queryset(self.organization), id=kwargs.get("task_id"))
        # Sichtbarkeit prüfen: private/geteilte Aufgaben dürfen nur von
        # Zugriffsberechtigten geöffnet werden (die Listenansicht blendet sie
        # aus - das Panel muss dieselbe Grenze ziehen).
        if not task.can_access(self.membership):
            raise PermissionDenied("Kein Zugriff auf diese Aufgabe.")
        context.update(panel_context(task, self.organization, self.membership))
        return context


class TaskPanelActionView(WorkViewMixin, View):
    """Central POST handler for all panel actions."""

    permission_required = "tasks.view"

    # ----- Rendering -------------------------------------------------------

    def _render(self, template: str, context: dict) -> str:
        return render_to_string(template, context, request=self.request)

    def _render_panel(self, task, form=None):
        context = panel_context(task, self.organization, self.membership)
        if form is not None:
            context["form"] = form
        return self._render("work/tasks/_panel.html", context)

    def _render_oob_card(self, task):
        task = selectors.reload_for_card(task)
        return self._render("work/tasks/_card_oob.html", {"task": task, "organization": self.organization})

    def _render_oob_counts(self):
        return self._render("work/tasks/_column_counts_oob.html", selectors.column_counts(self.organization))

    def _render_checklist(self, task):
        context = {
            "task": task,
            "checklist_items": task.checklist_items.all(),
            "checklist_form": TaskChecklistItemForm(),
            "can_edit": True,
            "organization": self.organization,
        }
        return self._render("work/tasks/_panel_checklist.html", context)

    def _render_attachments(self, task):
        context = {
            "task": task,
            "attachments": task.attachments.all(),
            "can_edit": True,
            "organization": self.organization,
        }
        return self._render("work/tasks/_panel_attachments.html", context)

    def _render_labels(self, task):
        context = {
            "task": task,
            "available_labels": selectors.labels_for_organization(self.organization),
            "task_label_ids": selectors.task_label_ids(task),
            "can_edit": True,
            "organization": self.organization,
        }
        return self._render("work/tasks/_panel_labels.html", context)

    def _render_activity(self, task):
        context = {"task": task, "activities": selectors.task_activities(task), "organization": self.organization}
        return self._render("work/tasks/_panel_activity.html", context)

    def _make_response(self, html, toast_message, toast_type="success"):
        response = HttpResponse(html)
        response["HX-Trigger"] = json.dumps({"show-toast": {"message": toast_message, "type": toast_type}})
        return response

    def _full_panel_response(self, task, toast_message):
        """Panel neu rendern plus OOB-Karte und -Zähler."""
        task = selectors.reload_for_panel(task)
        html = self._render_panel(task) + self._render_oob_card(task) + self._render_oob_counts()
        return self._make_response(html, toast_message)

    # ----- Dispatch --------------------------------------------------------

    ACTIONS = {
        "update": "_handle_update",
        "save": "_handle_save",
        "add_comment": "_handle_add_comment",
        "toggle_complete": "_handle_toggle_complete",
        "delete": "_handle_delete",
        "upload_attachment": "_handle_upload_attachment",
        "delete_attachment": "_handle_delete_attachment",
        "add_checklist_item": "_handle_add_checklist_item",
        "toggle_checklist_item": "_handle_toggle_checklist_item",
        "delete_checklist_item": "_handle_delete_checklist_item",
        "toggle_label": "_handle_toggle_label",
        "reorder_checklist": "_handle_reorder_checklist",
    }
    # Aktionen, die auch ohne Bearbeitungsrecht erlaubt sind (nur Zugriff nötig)
    READ_ACTIONS = {"add_comment"}

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        task = get_object_or_404(selectors.tasks_for_organization(self.organization), id=kwargs.get("task_id"))

        # Zugriffsgrenze wie in der Listenansicht: ohne can_access keine Aktion
        # (schützt insb. add_comment auf privaten/fremden Aufgaben).
        if not task.can_access(self.membership):
            return HttpResponse(status=403)

        handler_name = self.ACTIONS.get(action or "")
        if handler_name is None:
            return HttpResponse(status=400)
        if action not in self.READ_ACTIONS and not services.can_edit_task(task, self.membership):
            if action == "reorder_checklist":
                return JsonResponse({"error": "Keine Berechtigung."}, status=403)
            return HttpResponse(status=403)
        return getattr(self, handler_name)(request, task)

    # ----- Formular --------------------------------------------------------

    def _bind_panel_form(self, request, task):
        """Formular binden; alte Werte vorher sichern (ModelForm überschreibt die Instanz)."""
        old_values = services.capture_old_values(task)
        form = TaskPanelForm(request.POST, instance=task, organization=self.organization)
        return form, old_values

    def _handle_update(self, request, task):
        """Auto-save: hx-swap=none, only OOB card + counts."""
        form, old_values = self._bind_panel_form(request, task)
        if form.is_valid():
            services.apply_panel_update(form.save(commit=False), self.membership, old_values)
            return HttpResponse(self._render_oob_card(task) + self._render_oob_counts())
        # Validation error: re-render full panel
        html = self._render_panel(selectors.reload_for_panel(task), form=form)
        response = HttpResponse(html)
        response["HX-Reswap"] = "innerHTML"
        response["HX-Retarget"] = "#task-panel-container"
        response["HX-Trigger"] = json.dumps({"show-toast": {"message": "Fehler beim Speichern.", "type": "error"}})
        return response

    def _handle_save(self, request, task):
        """Explicit save button: re-render full panel."""
        form, old_values = self._bind_panel_form(request, task)
        if form.is_valid():
            services.apply_panel_update(form.save(commit=False), self.membership, old_values)
            return self._full_panel_response(task, "Gespeichert.")
        html = self._render_panel(selectors.reload_for_panel(task), form=form)
        return self._make_response(html, "Fehler beim Speichern.", "error")

    # ----- Kommentar, Status, Löschen ----------------------------------------

    def _handle_add_comment(self, request, task):
        content = request.POST.get("content", "").strip()
        if not content:
            return HttpResponse(status=400)
        services.add_comment(task, self.membership, content)
        return self._make_response(self._render_activity(task), "Kommentar hinzugefügt.")

    def _handle_toggle_complete(self, request, task):
        services.toggle_completion(task, self.membership, reopen_status="in_progress")
        msg = "Aufgabe als erledigt markiert." if task.is_completed else "Aufgabe wieder geöffnet."
        return self._full_panel_response(task, msg)

    def _handle_delete(self, request, task):
        task_id = task.id
        card_id = f"task-card-{task_id}"
        services.delete_task(task)

        oob_delete = f'<div id="{card_id}" hx-swap-oob="delete"></div>'
        response = HttpResponse(oob_delete + self._render_oob_counts())
        response["HX-Trigger"] = json.dumps(
            {
                "show-toast": {"message": "Aufgabe gelöscht.", "type": "success"},
                "taskDeleted": {"taskId": str(task_id)},
            }
        )
        return response

    # ----- Anhänge -----------------------------------------------------------

    def _handle_upload_attachment(self, request, task):
        form = TaskAttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded = request.FILES["file"]
            services.add_attachment(task, self.membership, form.save(commit=False), uploaded)
            html = self._render_attachments(task) + oob_activity(self._render_activity(task))
            return self._make_response(html, f'"{uploaded.name}" hochgeladen.')
        return self._make_response("", "Fehler beim Hochladen.", "error")

    def _handle_delete_attachment(self, request, task):
        attachment = get_object_or_404(TaskAttachment, id=request.POST.get("attachment_id"), task=task)
        filename = services.remove_attachment(task, self.membership, attachment)
        html = self._render_attachments(task) + oob_activity(self._render_activity(task))
        return self._make_response(html, f'"{filename}" entfernt.')

    # ----- Checkliste --------------------------------------------------------

    def _handle_add_checklist_item(self, request, task):
        form = TaskChecklistItemForm(request.POST)
        if not form.is_valid():
            return HttpResponse(status=400)
        services.add_checklist_item(task, self.membership, form.save(commit=False))
        # OOB card update for checklist progress
        html = self._render_checklist(task) + oob_activity(self._render_activity(task)) + self._render_oob_card(task)
        return self._make_response(html, "Punkt hinzugefügt.")

    def _handle_toggle_checklist_item(self, request, task):
        item = get_object_or_404(TaskChecklistItem, id=request.POST.get("item_id"), task=task)
        services.toggle_checklist_item(task, self.membership, item)
        return HttpResponse(self._render_checklist(task) + self._render_oob_card(task))

    def _handle_delete_checklist_item(self, request, task):
        item = get_object_or_404(TaskChecklistItem, id=request.POST.get("item_id"), task=task)
        services.delete_checklist_item(item)
        return HttpResponse(self._render_checklist(task) + self._render_oob_card(task))

    def _handle_reorder_checklist(self, request, task):
        try:
            order = json.loads(request.POST.get("order", "[]"))
            services.reorder_checklist(task, order)
            return JsonResponse({"success": True})
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Ungültige Daten."}, status=400)

    # ----- Labels ------------------------------------------------------------

    def _handle_toggle_label(self, request, task):
        label = get_object_or_404(TaskLabel, id=request.POST.get("label_id"), organization=self.organization)
        services.toggle_label(task, self.membership, label)
        html = self._render_labels(task) + self._render_oob_card(task) + oob_activity(self._render_activity(task))
        return HttpResponse(html)
