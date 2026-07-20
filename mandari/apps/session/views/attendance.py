# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwesenheitserfassung für das Session RIS (Issue #30).

Views für:
- Anwesenheitsliste aus der Gremienbesetzung erzeugen
- Gäste/Verwaltungsvertreter manuell ergänzen und wieder entfernen
- (Schnellerfassung je Zeile läuft über AttendanceUpdateView, HTMX)
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from ..models import SessionAttendance, SessionMeeting, SessionPerson
from ..permissions import SessionViewMixin
from ..services import attendance_service


def _get_meeting(view, meeting_id):
    qs = SessionMeeting.objects.filter(tenant=view.session_tenant)
    if not view.has_permission("view_non_public_meetings"):
        qs = qs.filter(is_public=True)
    return get_object_or_404(qs, pk=meeting_id)


def _meeting_redirect(view, meeting):
    return redirect(
        "session:meeting_detail",
        tenant_slug=view.session_tenant.slug,
        meeting_id=meeting.id,
    )


class AttendanceGenerateView(SessionViewMixin, View):
    """Anwesenheitsliste aus der aktuellen Gremienbesetzung vorbefüllen."""

    permission_required = "manage_attendance"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, meeting_id):
        meeting = _get_meeting(self, meeting_id)
        created = attendance_service.generate_attendance(meeting)
        if created:
            messages.success(
                request,
                f"Anwesenheitsliste erzeugt: {created} Person(en) aus der Besetzung übernommen.",
            )
        else:
            messages.info(request, "Anwesenheitsliste ist bereits vollständig — keine neuen Einträge.")
        return _meeting_redirect(self, meeting)


class AttendanceAddView(SessionViewMixin, View):
    """Gast/Verwaltungsvertreter manuell zur Anwesenheitsliste ergänzen."""

    permission_required = "manage_attendance"
    http_method_names = ["post"]

    VALID_ROLES = {choice[0] for choice in SessionAttendance._meta.get_field("role").choices}

    def post(self, request, tenant_slug, meeting_id):
        meeting = _get_meeting(self, meeting_id)

        person = get_object_or_404(
            SessionPerson,
            pk=request.POST.get("person"),
            tenant=self.session_tenant,
            is_active=True,
        )
        role = request.POST.get("role", "guest")
        if role not in self.VALID_ROLES:
            role = "guest"

        _attendance, created = SessionAttendance.objects.get_or_create(
            meeting=meeting,
            person=person,
            defaults={
                "status": "present",
                "role": role,
                # Manuell ergänzte Gäste/Verwaltung sind nicht stimmberechtigt
                "has_voting_rights": False,
            },
        )
        if created:
            messages.success(request, f"{person.display_name} wurde zur Anwesenheitsliste hinzugefügt.")
        else:
            messages.info(request, f"{person.display_name} steht bereits auf der Anwesenheitsliste.")
        return _meeting_redirect(self, meeting)


class AttendanceDeleteView(SessionViewMixin, View):
    """Anwesenheitszeile entfernen (z. B. versehentlich ergänzter Gast)."""

    permission_required = "manage_attendance"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, attendance_id):
        attendance = get_object_or_404(
            SessionAttendance.objects.select_related("meeting", "person"),
            pk=attendance_id,
            meeting__tenant=self.session_tenant,
        )
        meeting = attendance.meeting
        name = attendance.person.display_name
        attendance.delete()
        messages.success(request, f"{name} wurde von der Anwesenheitsliste entfernt.")
        if request.headers.get("HX-Request") == "true":
            from django.http import HttpResponse

            return HttpResponse(status=204, headers={"HX-Refresh": "true"})
        return _meeting_redirect(self, meeting)
