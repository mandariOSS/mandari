# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ladungen der Verwaltung im Work-Portal (Issue #225).

Mandatstragende, deren Work-Konto eindeutig einer Person im Sitzungsdienst zugeordnet ist
(bestätigte, identische E-Mail-Adresse bei aktiver Verbindung Fraktion ↔ Verwaltung, siehe
``apps/session/services/portal_link_service.py``), sehen hier ihre Ladungen, laden die
Tagesordnung und melden sich zurück – ohne Rückmeldelink. Die Logik liegt in den Session-Services.
"""

from __future__ import annotations

from typing import Any, cast

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.session.models import SessionInvitationRecipient, SessionPerson
from apps.session.services import invitation_response_service, invitation_service, portal_link_service


def _portal_person(view: Any) -> SessionPerson | None:
    return portal_link_service.person_for_user(view.request.user, view.organization)


def _recipient_or_404(view: Any, recipient_id: Any) -> tuple[SessionPerson, SessionInvitationRecipient]:
    person = _portal_person(view)
    if person is None:
        raise Http404("Keine Zuordnung zum Sitzungsdienst")
    recipient = invitation_response_service.portal_recipient(person, recipient_id)
    if recipient is None:
        raise Http404("Ladung nicht gefunden")
    return person, recipient


class SessionInvitationListView(WorkViewMixin, TemplateView):
    """Anstehende Ladungen der Verwaltung mit Empfangsbestätigung und Rückmeldung."""

    template_name = "work/meetings/session_invitations.html"
    permission_required = "meetings.view"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        person = _portal_person(self)
        context["active_nav"] = "meetings"
        context["person"] = person
        context["connected_tenant"] = portal_link_service.tenant_for_organization(cast(Any, self).organization)
        context["invitations"] = invitation_response_service.portal_invitations(person) if person else []
        return context


class SessionInvitationRespondView(WorkViewMixin, View):
    """Erhalt bestätigen, zu- oder absagen (POST)."""

    http_method_names = ["post"]
    permission_required = "meetings.view"

    def post(self, request: HttpRequest, org_slug: str, recipient_id: Any) -> HttpResponse:
        person, recipient = _recipient_or_404(self, recipient_id)
        form = invitation_response_service.parse_response_form(request.POST)
        try:
            result = invitation_response_service.respond_via_portal(person, recipient, form)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("work:session_invitations", org_slug=org_slug)
        if result is None:
            messages.success(request, "Empfang bestätigt.")
        elif result.substitutes is not None and result.substitutes.notified:
            messages.success(
                request,
                f"Absage gespeichert. Ihre Stellvertretung wurde benachrichtigt: {', '.join(result.substitutes.notified)}.",
            )
        else:
            messages.success(request, "Ihre Rückmeldung ist gespeichert.")
        return redirect("work:session_invitations", org_slug=org_slug)


class SessionInvitationAgendaView(WorkViewMixin, View):
    """Tagesordnung als PDF in der Fassung, die die Person erhalten hat (Ö/NÖ, Nachtrag)."""

    http_method_names = ["get"]
    permission_required = "meetings.view"

    def get(self, request: HttpRequest, org_slug: str, recipient_id: Any) -> HttpResponse:
        _person, recipient = _recipient_or_404(self, recipient_id)
        supplementary = recipient.dispatch.dispatch_type == "supplementary"
        pdf = invitation_service.build_agenda_pdf(
            recipient.dispatch.meeting,
            include_non_public=recipient.includes_non_public,
            supplementary_only=supplementary,
        )
        filename = "nachtrags-tagesordnung.pdf" if supplementary else "einladung-tagesordnung.pdf"
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
