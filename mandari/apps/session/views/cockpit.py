# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungscockpit (Issue #140): Live-Ansicht der laufenden Sitzung.

- ``MeetingCockpitView``: Seite – mit dem Recht ``conduct_meetings`` zum Steuern, sonst Mitlese-Ansicht
- ``MeetingCockpitStateView``: Stand als HTMX-Fragment; unverändert (``?v=`` gleich ``state_version``) ohne
  Inhalt (204), damit Polling und Hinweise über den WebSocket nichts rendern, solange nichts passiert
- ``MeetingCockpitActionView``: alle Aktionen (POST ``aktion``); antwortet mit dem neuen Stand und einer
  Meldung (Toast), eine abgewiesene Aktion nur mit der Meldung (Eingaben bleiben stehen), ohne JavaScript
  mit Weiterleitung

Regeln, Audit und Benachrichtigung: ``services.cockpit_service``.
"""

from __future__ import annotations

import json
from typing import Any, cast

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionMeeting, SessionTenant
from ..permissions import SessionViewMixin
from ..services import cockpit_service

STATE_TEMPLATE = "session/cockpit/_stand.html"
_MESSAGE_LEVELS = {"success": messages.SUCCESS, "info": messages.INFO, "warning": messages.WARNING}


class _CockpitMixin(SessionViewMixin):
    """Sitzung des Mandanten; nichtöffentliche nur mit dem NÖ-Recht (sonst 404)."""

    kwargs: dict[str, Any]
    request: HttpRequest

    @property
    def tenant(self) -> SessionTenant:
        return cast(SessionTenant, self.session_tenant)

    def get_meeting(self) -> SessionMeeting:
        queryset = SessionMeeting.objects.filter(tenant=self.tenant).visible_to(self.session_permissions)
        return get_object_or_404(
            queryset.select_related("organization", "tenant__state_profile"), pk=self.kwargs["meeting_id"]
        )

    def state_context(self, meeting: SessionMeeting) -> dict[str, Any]:
        return {
            "meeting": meeting,
            "state": cockpit_service.build_state(meeting, self.session_permissions),
            "tenant_slug": self.tenant.slug,
        }

    def render_state(self, meeting: SessionMeeting, *, toast: dict[str, str] | None = None) -> HttpResponse:
        response = TemplateResponse(self.request, STATE_TEMPLATE, self.state_context(meeting))
        if toast:
            response["HX-Trigger"] = json.dumps({"showToast": toast})
        return response


class MeetingCockpitView(_CockpitMixin, TemplateView):
    """Cockpit bzw. Mitlese-Ansicht einer Sitzung."""

    template_name = "session/cockpit/page.html"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        meeting = self.get_meeting()
        context.update(self.state_context(meeting))
        if not meeting.is_public:
            # Lesezugriff auf eine nichtöffentliche Sitzung (Issue #221): nur Objekt, nie Inhalt
            audit.log_read(
                self.request,
                meeting,
                tenant=self.tenant,
                user=self.session_user,
                changes={"umfang": "Sitzungscockpit"},
            )
        return context


class MeetingCockpitStateView(_CockpitMixin, View):
    """Stand als Fragment für Polling und WebSocket-Hinweise."""

    permission_required = "view_meetings"
    http_method_names = ["get"]

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        meeting = self.get_meeting()
        if request.GET.get("v") == cockpit_service.state_version(meeting):
            return HttpResponse(status=204)
        return self.render_state(meeting)


class MeetingCockpitActionView(_CockpitMixin, View):
    """Aktionen der Sitzungsleitung und Protokollführung."""

    permission_required = ["view_meetings", cockpit_service.CONTROL_PERMISSION]
    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        meeting = self.get_meeting()
        try:
            outcome = cockpit_service.perform(
                meeting,
                str(request.POST.get("aktion", "")),
                request.POST,
                permissions=self.session_permissions,
                session_user=self.session_user,
            )
            text, level = outcome.message, outcome.level
        except cockpit_service.CockpitError as exc:
            text, level = exc.user_message, "error"
            if self.is_htmx:
                # Abgewiesen: Stand nicht austauschen, damit Eingaben (Stimmen, Vermerk) stehen bleiben;
                # die Ansicht holt den aktuellen Stand selbst nach (meetingCockpit, „cockpit:nachladen“)
                response = HttpResponse(status=200)
                response["HX-Reswap"] = "none"
                response["HX-Trigger"] = json.dumps(
                    {"showToast": {"message": text, "type": level}, "cockpit:nachladen": True}
                )
                return response
        if self.is_htmx:
            meeting.refresh_from_db()
            return self.render_state(meeting, toast={"message": text, "type": level})
        messages.add_message(request, _MESSAGE_LEVELS.get(level, messages.ERROR), text)
        return redirect("session:meeting_cockpit", tenant_slug=self.tenant.slug, meeting_id=meeting.pk)
