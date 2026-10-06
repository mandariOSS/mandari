# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben aus einem TOP der Sitzungsvorbereitung (#856).

GET listet die Aufgaben, die das Mitglied zu diesem TOP sehen darf (Sichtbarkeitsregeln des Aufgaben-Moduls,
Gastzugänge sehen keine). POST legt eine Aufgabe an (``services.create_agenda_task``): Titel, optional
Zuständige:r aus der eigenen Organisation und Fälligkeit, verknüpft mit TOP und Sitzung, für die Organisation
sichtbar. Lesen braucht „Aufgaben anzeigen“, Anlegen „Aufgaben erstellen“. Abhaken läuft über den Endpunkt des
Aufgabenboards (``work:tasks_api``, Aktion ``toggle_complete``); hier steht je Aufgabe nur, ob das Mitglied es darf.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, JsonResponse
from django.views import View

from apps.common.mixins import WorkViewMixin
from apps.work.tasks import selectors as task_selectors
from apps.work.tasks import services as task_services

from .. import selectors, services
from ..serializers import serialize_agenda_task
from ..services import PreparationError
from ._helpers import error_response, request_payload, unauthorized


class AgendaTasksAPIView(WorkViewMixin, View):
    """API: Aufgaben zu einem TOP lesen und anlegen."""

    permission_required = "meetings.prepare"

    def _agenda_item(self) -> Any:
        """TOP der Sitzung innerhalb der Gremien der Organisation (sonst 404); ohne verknüpfte Kommune ``None``."""
        bodies = selectors.organization_bodies(self.organization)
        if bodies is None:
            return None
        meeting = selectors.get_meeting_or_404(bodies, self.kwargs["meeting_id"])
        return selectors.get_agenda_item_or_404(self.kwargs["item_id"], meeting)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        agenda_item = self._agenda_item()
        if agenda_item is None or self.membership is None or self.organization is None:
            return unauthorized()
        if self.membership.is_guest or not self.has_permission("tasks.view"):
            return JsonResponse({"tasks": []})
        membership = self.membership
        tasks = task_selectors.tasks_for_agenda_item(self.organization, membership, agenda_item.id)
        return JsonResponse(
            {
                "tasks": [
                    serialize_agenda_task(
                        task, self.organization.slug, darf_abhaken=task_services.can_edit_task(task, membership)
                    )
                    for task in tasks
                ]
            }
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        agenda_item = self._agenda_item()
        if agenda_item is None or self.membership is None or self.organization is None:
            return unauthorized()
        if self.membership.is_guest or not self.has_permission("tasks.create"):
            return unauthorized()
        try:
            task = services.create_agenda_task(
                self.organization, agenda_item, self.membership, request_payload(request)
            )
        except PreparationError as exc:
            return error_response(str(exc), exc.status)
        # Die anlegende Person ist Erstellerin und darf die Aufgabe damit immer abhaken
        return JsonResponse(
            {"success": True, "task": serialize_agenda_task(task, self.organization.slug, darf_abhaken=True)}
        )
