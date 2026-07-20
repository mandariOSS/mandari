# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ladungs-/Einladungsversand für das Session RIS (Issue #29).

Views für:
- Versandseite (Empfängervorschau aus der Besetzung, Fristanzeige,
  Versandhistorie, Erstladung + Nachtrags-Tagesordnung)
- Einladungs-PDF-Download (Ö-/NÖ-Variante je Berechtigung)
- ICS-Download
"""

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView

from ..models import SessionMeeting
from ..permissions import SessionViewMixin
from ..services import invitation_service


def _get_meeting(view, meeting_id, require_non_public_permission=True):
    """Sitzung tenant-gefiltert laden; NÖ-Sitzungen nur mit Berechtigung."""
    qs = SessionMeeting.objects.filter(tenant=view.session_tenant).select_related("organization", "tenant")
    if require_non_public_permission and not view.has_permission("view_non_public_meetings"):
        qs = qs.filter(is_public=True)
    return get_object_or_404(qs, pk=meeting_id)


class MeetingInvitationView(SessionViewMixin, TemplateView):
    """
    Versandseite: Empfängerkreis, Ladungsfrist, Versandhistorie und Versand.

    POST versendet die Ladung (dispatch_type=invitation) oder die
    Nachtrags-Tagesordnung (dispatch_type=supplementary).
    """

    template_name = "session/meetings/invitation.html"
    permission_required = "edit_meetings"

    def get_meeting(self):
        return _get_meeting(self, self.kwargs["meeting_id"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        meeting = self.get_meeting()
        recipients = invitation_service.get_recipients(meeting)

        supplementary_count = meeting.agenda_items.filter(is_supplementary=True).count()

        context.update(
            {
                "meeting": meeting,
                "recipients": recipients,
                "recipients_with_email": [r for r in recipients if not r["missing_email"]],
                "recipients_without_email": [r for r in recipients if r["missing_email"]],
                "invitation_deadline": meeting.invitation_deadline,
                "invitation_overdue": meeting.invitation_overdue,
                "invitation_period_days": meeting.organization.invitation_period_days,
                "dispatches": meeting.invitation_dispatches.select_related("sent_by__user").prefetch_related(
                    "recipients"
                ),
                "supplementary_count": supplementary_count,
                "default_subject": invitation_service._default_subject(meeting, supplementary=False),
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        meeting = self.get_meeting()
        dispatch_type = request.POST.get("dispatch_type", "invitation")
        if dispatch_type not in ("invitation", "supplementary"):
            messages.error(request, "Ungültige Versandart.")
            return redirect(
                "session:meeting_invitation",
                tenant_slug=self.session_tenant.slug,
                meeting_id=meeting.id,
            )

        if dispatch_type == "supplementary" and meeting.invitation_sent_at is None:
            messages.error(request, "Eine Nachladung ist erst nach Versand der Erstladung möglich.")
            return redirect(
                "session:meeting_invitation",
                tenant_slug=self.session_tenant.slug,
                meeting_id=meeting.id,
            )

        recipients = [r for r in invitation_service.get_recipients(meeting) if not r["missing_email"]]
        if not recipients:
            messages.error(
                request,
                "Kein Empfänger mit E-Mail-Adresse in der Gremienbesetzung — Versand nicht möglich.",
            )
            return redirect(
                "session:meeting_invitation",
                tenant_slug=self.session_tenant.slug,
                meeting_id=meeting.id,
            )

        dispatch = invitation_service.send_invitations(
            meeting,
            sent_by=self.session_user,
            dispatch_type=dispatch_type,
            subject=request.POST.get("subject", ""),
            message=request.POST.get("message", ""),
            request=request,
        )

        sent = dispatch.recipients.filter(status="sent").count()
        failed = dispatch.recipients.filter(status="failed").count()
        if failed:
            messages.warning(
                request,
                f"{dispatch.get_dispatch_type_display()} versandt: {sent} erfolgreich, {failed} fehlgeschlagen.",
            )
        else:
            messages.success(request, f"{dispatch.get_dispatch_type_display()} wurde an {sent} Empfänger versandt.")
        return redirect(
            "session:meeting_invitation",
            tenant_slug=self.session_tenant.slug,
            meeting_id=meeting.id,
        )


class MeetingAgendaPdfView(SessionViewMixin, TemplateView):
    """
    Einladungs-PDF mit Tagesordnung herunterladen.

    Ö/NÖ: Die vollständige Variante (inkl. NÖ-Teil) erhalten nur Nutzer
    mit view_non_public_meetings; alle anderen die Ö-Fassung.
    """

    permission_required = "view_meetings"

    def get(self, request, *args, **kwargs):
        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        include_np = self.has_permission("view_non_public_meetings")
        supplementary = request.GET.get("variante") == "nachtrag"
        pdf_bytes = invitation_service.build_agenda_pdf(
            meeting,
            include_non_public=include_np,
            supplementary_only=supplementary,
        )
        filename = "nachtrags-tagesordnung.pdf" if supplementary else "einladung-tagesordnung.pdf"
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class MeetingIcsView(SessionViewMixin, TemplateView):
    """ICS-Kalenderdatei einer Sitzung herunterladen."""

    permission_required = "view_meetings"

    def get(self, request, *args, **kwargs):
        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        ics_bytes = invitation_service.build_meeting_ics(meeting)
        response = HttpResponse(ics_bytes, content_type="text/calendar; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="sitzung.ics"'
        return response
