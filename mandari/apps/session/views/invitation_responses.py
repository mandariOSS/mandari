# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ladung mit Empfangsbestätigung und Rückmeldung (Issue #225).

- öffentlicher Rückmeldelink (ohne Anmeldung, signiertes Token, Ratenbegrenzung): Erhalt
  bestätigen, Zusage, Absage mit Grund und Vertretungswunsch – GET zeigt nur an, erst ein
  Klick (POST) bestätigt oder meldet zurück
- Übersicht im Sitzungsdienst: Status je Empfänger, Erinnerung an alle ohne Bestätigung,
  manuelle Einträge des Sitzungsdienstes, Ladungsnachweis (PDF) und Serienbrief (PDF/CSV)
"""

from __future__ import annotations

from typing import Any, cast

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionInvitationDispatch, SessionMeeting, SessionPerson, SessionTenant
from ..permissions import SessionViewMixin
from ..services import invitation_export_service, invitation_response_service, invitation_token

_log_event = cast(Any, audit).log_event

PUBLIC_TEMPLATE = "session/public/invitation_response.html"


# =============================================================================
# Öffentlicher Rückmeldelink
# =============================================================================


def _no_store(response: HttpResponse) -> HttpResponse:
    """Token-Seiten weder zwischenspeichern noch indexieren, keinen Referrer weitergeben."""
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


class InvitationResponseView(View):
    """
    Rückmeldung per Link aus der Ladungsmail (ohne Anmeldung).

    Sicherheit: Das Token ist signiert, an Empfänger und Sitzung gebunden und läuft mit
    Sitzungsbeginn ab (apps/session/services/invitation_token.py). Aufrufe je IP-Adresse sind
    begrenzt, ungültige Tokens zählen strenger. Ein bloßer Aufruf (GET, z. B. durch
    Link-Vorschauen oder Virenscanner) bestätigt nichts.
    """

    http_method_names = ["get", "post"]

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if invitation_token.rate_limited(request):
            return self._render(request, {"state": "rate_limited"}, status=429)
        return cast(HttpResponse, super().dispatch(request, *args, **kwargs))

    def _render(self, request: HttpRequest, context: dict[str, Any], status: int = 200) -> HttpResponse:
        return _no_store(render(request, PUBLIC_TEMPLATE, context, status=status))

    def _check(self, request: HttpRequest, token: str) -> invitation_token.TokenCheck:
        check = invitation_token.check_token(token)
        if check.state == "invalid":
            invitation_token.record_failure(request)
        return check

    def _page(self, request: HttpRequest, check: invitation_token.TokenCheck, **extra: Any) -> HttpResponse:
        if check.recipient is None:
            return self._render(request, {"state": check.state}, status=404 if check.state == "invalid" else 410)
        recipient = check.recipient
        meeting = recipient.dispatch.meeting
        person = cast(SessionPerson, recipient.person)
        attendance = meeting.attendances.filter(person=person).first()
        context = {
            "state": check.state,
            "recipient": recipient,
            "meeting": meeting,
            "tenant": meeting.tenant,
            "person": person,
            "attendance": attendance,
            "reason": invitation_response_service.response_reason(attendance),
            "location": invitation_response_service.meeting_location(meeting),
            "has_substitutes": bool(invitation_response_service.substitute_memberships(meeting, person)),
            "substitution_rows": invitation_response_service.substitution_requests(attendance),
            "is_substitution": recipient.dispatch.dispatch_type == "substitution",
            "page_title": "Vertretungsanfrage"
            if recipient.dispatch.dispatch_type == "substitution"
            else "Rückmeldung zur Ladung",
            **extra,
        }
        return self._render(request, context)

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        return self._page(request, self._check(request, token), saved=request.GET.get("gespeichert") == "1")

    def post(self, request: HttpRequest, token: str) -> HttpResponse:
        check = self._check(request, token)
        if not check.ok or check.recipient is None:
            return self._page(request, check)
        recipient = check.recipient
        person = cast(SessionPerson, recipient.person)
        form = invitation_response_service.parse_response_form(request.POST)
        decision = form.decision
        if form.action == "acknowledge":
            invitation_response_service.acknowledge(recipient, via="link")
        elif decision is not None:
            invitation_response_service.record_response(
                recipient.dispatch.meeting,
                person,
                decision=decision,
                source="link",
                reason=form.reason,
                substitute_requested=form.substitute_requested,
                recipient=recipient,
            )
        else:
            return self._page(request, check, error="Unbekannte Aktion.")
        return redirect(f"{request.path}?gespeichert=1")


# =============================================================================
# Sitzungsdienst: Übersicht, Erinnerung, manuelle Einträge
# =============================================================================


def _get_meeting(view: Any, meeting_id: Any) -> SessionMeeting:
    """Sitzung tenant-gefiltert laden; NÖ-Sitzungen nur mit Berechtigung."""
    qs = SessionMeeting.objects.filter(tenant=view.session_tenant).select_related("organization", "tenant")
    if not view.has_permission("view_non_public_meetings"):
        qs = qs.filter(is_public=True)
    return get_object_or_404(qs, pk=meeting_id)


def _status_redirect(tenant: SessionTenant, meeting: SessionMeeting) -> HttpResponse:
    return redirect("session:meeting_invitation_status", tenant_slug=tenant.slug, meeting_id=meeting.id)


class MeetingInvitationStatusView(SessionViewMixin, TemplateView):
    """Übersicht je Sitzung: Status je Empfänger (Versand, Bestätigung, Rückmeldung, Zeitstempel)."""

    template_name = "session/meetings/invitation_status.html"
    permission_required = "edit_meetings"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        overview = invitation_response_service.meeting_overview(meeting, include_reasons=True)
        context.update(
            {
                "meeting": meeting,
                "overview": overview,
                "reminder_count": len(invitation_response_service.reminder_candidates(meeting, overview)),
                "is_open": invitation_response_service.is_open_for_responses(meeting),
                "letter_batches": invitation_export_service.letter_batches(meeting),
                "can_enter_responses": self.has_permission("manage_attendance"),
            }
        )
        return context


class MeetingInvitationReminderView(SessionViewMixin, View):
    """Erinnerung an alle ohne Empfangsbestätigung senden."""

    http_method_names = ["post"]
    permission_required = "edit_meetings"

    def post(self, request: HttpRequest, tenant_slug: str, meeting_id: Any) -> HttpResponse:
        meeting = _get_meeting(self, meeting_id)
        tenant = cast(SessionTenant, self.session_tenant)
        if not invitation_response_service.is_open_for_responses(meeting):
            messages.error(request, "Die Sitzung hat bereits begonnen oder ist abgesagt – keine Erinnerung möglich.")
            return _status_redirect(tenant, meeting)
        sent, failed = invitation_response_service.send_acknowledgement_reminders(meeting)
        if not sent and not failed:
            messages.info(request, "Alle Empfänger haben den Erhalt bestätigt oder sich zurückgemeldet.")
        elif failed:
            messages.warning(request, f"Erinnerung an {sent} Empfänger versandt, {failed} fehlgeschlagen.")
        else:
            messages.success(request, f"Erinnerung an {sent} Empfänger versandt.")
        return _status_redirect(tenant, meeting)


class MeetingResponseEntryView(SessionViewMixin, View):
    """Rückmeldung oder Empfangsbestätigung durch den Sitzungsdienst eintragen (z. B. nach Anruf oder Brief)."""

    http_method_names = ["post"]
    permission_required = ["edit_meetings", "manage_attendance"]

    def post(self, request: HttpRequest, tenant_slug: str, meeting_id: Any, person_id: Any) -> HttpResponse:
        meeting = _get_meeting(self, meeting_id)
        tenant = cast(SessionTenant, self.session_tenant)
        person = get_object_or_404(
            SessionPerson.objects.filter(tenant=tenant, invitation_receipts__dispatch__meeting=meeting).distinct(),
            pk=person_id,
        )
        if not invitation_response_service.is_open_for_responses(meeting):
            messages.error(request, "Die Sitzung hat begonnen oder ist abgesagt – bitte die Anwesenheitsliste nutzen.")
            return _status_redirect(tenant, meeting)
        form = invitation_response_service.parse_response_form(request.POST)
        decision = form.decision
        if form.action == "acknowledge":
            count = invitation_response_service.acknowledge_meeting(person, meeting, via="staff")
            if count:
                messages.success(request, f"Empfang für {person.display_name} vermerkt ({count} Versand/Versände).")
            else:
                messages.info(request, "Kein zugestellter Versand offen – Briefe bitte zuerst als versandt vermerken.")
        elif decision is not None:
            result = invitation_response_service.record_response(
                meeting,
                person,
                decision=decision,
                source="staff",
                reason=form.reason,
                substitute_requested=form.substitute_requested,
            )
            text = f"Rückmeldung für {person.display_name} gespeichert."
            if result.substitutes is not None and result.substitutes.notified:
                text += f" Stellvertretung benachrichtigt: {', '.join(result.substitutes.notified)}."
            elif result.substitutes is not None and result.substitutes.by_letter:
                text += " Die Stellvertretung erhält die Anfrage per Brief (Serienbrief)."
            messages.success(request, text)
        else:
            messages.error(request, "Unbekannte Aktion.")
        return _status_redirect(tenant, meeting)


# =============================================================================
# Ladungsnachweis und Serienbrief
# =============================================================================


class MeetingInvitationProofView(SessionViewMixin, View):
    """Ladungsnachweis als PDF für die Akte (ohne Absagegründe)."""

    http_method_names = ["get"]
    permission_required = "edit_meetings"

    def get(self, request: HttpRequest, tenant_slug: str, meeting_id: Any) -> HttpResponse:
        meeting = _get_meeting(self, meeting_id)
        overview = invitation_response_service.meeting_overview(meeting, include_reasons=False)
        user = cast(Any, request.user)
        pdf = invitation_export_service.build_proof_pdf(
            meeting, overview, generated_by=user.get_display_name() or user.email
        )
        _log_event("download", meeting, changes={"dokument": "Ladungsnachweis"}, request=request)
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = 'attachment; filename="ladungsnachweis.pdf"'
        return response


def _dispatch_for(view: Any, meeting: SessionMeeting, dispatch_id: Any) -> SessionInvitationDispatch:
    return get_object_or_404(
        SessionInvitationDispatch.objects.select_related("meeting__tenant", "meeting__organization"),
        pk=dispatch_id,
        meeting=meeting,
    )


class MeetingSerialLetterView(SessionViewMixin, View):
    """Serienbrief für Personen mit Zustellweg Brief: PDF (ein Anschreiben je Person) oder CSV."""

    http_method_names = ["get"]
    permission_required = "edit_meetings"

    def get(self, request: HttpRequest, tenant_slug: str, meeting_id: Any, dispatch_id: Any, fmt: str) -> HttpResponse:
        if fmt not in ("pdf", "csv"):
            raise Http404("Unbekanntes Format")
        meeting = _get_meeting(self, meeting_id)
        dispatch = _dispatch_for(self, meeting, dispatch_id)
        if not invitation_export_service.letter_recipients(dispatch):
            raise Http404("Keine Briefempfänger in diesem Versand")
        _log_event("download", meeting, changes={"dokument": f"Serienbrief ({fmt.upper()})"}, request=request)
        if fmt == "csv":
            csv_text = invitation_export_service.build_serial_letter_csv(dispatch)
            # BOM für Excel-kompatible UTF-8-Erkennung
            response = HttpResponse("﻿" + csv_text, content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = 'attachment; filename="serienbrief-ladung.csv"'
            return response
        response = HttpResponse(
            invitation_export_service.build_serial_letter_pdf(dispatch), content_type="application/pdf"
        )
        response["Content-Disposition"] = 'attachment; filename="serienbrief-ladung.pdf"'
        return response


class MeetingLettersSentView(SessionViewMixin, View):
    """Briefe eines Versands als zur Post gegeben vermerken (Versandzeitpunkt für den Nachweis)."""

    http_method_names = ["post"]
    permission_required = "edit_meetings"

    def post(self, request: HttpRequest, tenant_slug: str, meeting_id: Any, dispatch_id: Any) -> HttpResponse:
        meeting = _get_meeting(self, meeting_id)
        dispatch = _dispatch_for(self, meeting, dispatch_id)
        count = invitation_response_service.mark_letters_sent(dispatch)
        if count:
            messages.success(request, f"{count} Brief(e) als versandt vermerkt.")
        else:
            messages.info(request, "Keine offenen Briefe in diesem Versand.")
        return _status_redirect(cast(SessionTenant, self.session_tenant), meeting)
