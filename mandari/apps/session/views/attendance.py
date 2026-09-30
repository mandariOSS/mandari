# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwesenheitserfassung für das Session RIS (Issue #30).

Views für:
- Anwesenheitsliste aus der Gremienbesetzung erzeugen
- Gäste/Verwaltungsvertreter manuell ergänzen und wieder entfernen
- (Schnellerfassung je Zeile läuft über AttendanceUpdateView, HTMX)
- Störungsvermerke Zugeschalteter erfassen, beenden und entfernen (Issue #139)
"""

from django import forms
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from apps.common.params import uuid_param

from ..models import SessionAttendance, SessionAttendanceDisruption, SessionMeeting, SessionPerson
from ..permissions import SessionViewMixin
from ..services import attendance_service, participation_service
from ..visibility import meeting_q

#: Meldung, wenn in einer Präsenzsitzung jemand zugeschaltet werden soll (Issue #139)
REMOTE_NOT_ALLOWED = "Zugeschaltet teilnehmen lässt sich nur in hybriden oder digitalen Sitzungen."


class SessionAttendanceForm(forms.ModelForm):
    """
    Schnellerfassung einer Anwesenheitszeile mit Teilnahmeart (Issue #139).

    Die Teilnahmeart fehlt im Formular von Präsenzsitzungen; dann bleibt sie unverändert. Zugeschaltet
    werden kann nur in hybriden und digitalen Sitzungen.
    """

    class Meta:
        model = SessionAttendance
        fields = ["status", "participation_mode", "arrival_time", "departure_time", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["participation_mode"].required = False

    def clean_participation_mode(self) -> str:
        current = self.instance.participation_mode
        value = self.cleaned_data.get("participation_mode") or current
        if (
            value == SessionAttendance.PARTICIPATION_REMOTE
            and value != current
            and not participation_service.remote_allowed(self.instance.meeting)
        ):
            raise forms.ValidationError(REMOTE_NOT_ALLOWED)
        return value


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
                # Digitale Sitzung: zugeschaltet (Issue #139)
                "participation_mode": participation_service.default_mode(meeting),
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
        # Anwesenheit nichtöffentlicher Sitzungen nur mit NÖ-Sichtrecht (wie das Anlegen)
        attendance = get_object_or_404(
            SessionAttendance.objects.select_related("meeting", "person").filter(
                meeting_q(self.session_permissions, "meeting__")
            ),
            pk=attendance_id,
            meeting__tenant=self.session_tenant,
        )
        meeting = attendance.meeting
        name = attendance.person.display_name
        attendance.delete()
        messages.success(request, f"{name} wurde von der Anwesenheitsliste entfernt.")
        if self.is_htmx:
            from django.http import HttpResponse

            return HttpResponse(status=204, headers={"HX-Refresh": "true"})
        return _meeting_redirect(self, meeting)


# =============================================================================
# Störungsvermerke Zugeschalteter (Issue #139)
# =============================================================================


def _attendance_for(meeting, attendance_id):
    """Anwesenheitszeile dieser Sitzung (die Sitzung ist bereits nach Mandant und Sichtrecht geprüft)."""
    attendance_uuid = uuid_param(attendance_id)
    if attendance_uuid is None:
        raise Http404
    return get_object_or_404(
        SessionAttendance.objects.select_related("meeting", "person"), pk=attendance_uuid, meeting=meeting
    )


#: Meldung bei einer nicht lesbaren Uhrzeit; der bisherige Stand bleibt unverändert
INVALID_TIME = "Die Uhrzeit ist nicht lesbar – bitte im Format HH:MM angeben. Es wurde nichts geändert."


class _InvalidTimeError(ValueError):
    """Uhrzeit im Formular nicht lesbar."""


def _time_value(raw):
    """Uhrzeit aus dem Formular (HH:MM); leer -> None, nicht lesbar -> ``_InvalidTimeError``."""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return forms.TimeField().clean(value)
    except forms.ValidationError:
        raise _InvalidTimeError from None


def _done(view, meeting):
    """Zurück zur Sitzung; die Schnellerfassung aus der Zeile (HTMX) lädt die Seite neu."""
    if view.is_htmx:
        from django.http import HttpResponse

        return HttpResponse(status=204, headers={"HX-Refresh": "true"})
    return _meeting_redirect(view, meeting)


def _cause(raw, default=SessionAttendanceDisruption.CAUSE_CONNECTION):
    causes = {value for value, _ in SessionAttendanceDisruption.CAUSE_CHOICES}
    return raw if raw in causes else default


class AttendanceDisruptionAddView(SessionViewMixin, View):
    """
    Störung einer zugeschalteten Person vermerken: ohne Uhrzeit „ab jetzt“ (Schnellerfassung in der
    Sitzung), sonst mit Beginn und optional Ende (Nacherfassung). Bis zum Ende zählt die Person nicht zur
    Beschlussfähigkeit.
    """

    permission_required = "manage_attendance"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, meeting_id):
        meeting = _get_meeting(self, meeting_id)
        attendance = _attendance_for(meeting, request.POST.get("attendance"))
        if not attendance.is_remote:
            messages.error(request, "Störungen lassen sich nur für zugeschaltete Personen vermerken.")
            return _meeting_redirect(self, attendance.meeting)
        try:
            started_at = _time_value(request.POST.get("started_at")) or participation_service.now()
            ended_at = _time_value(request.POST.get("ended_at"))
        except _InvalidTimeError:
            messages.error(request, INVALID_TIME)
            return _done(self, attendance.meeting)
        fehler = participation_service.period_error(attendance.meeting, started_at, ended_at)
        if fehler:
            messages.error(request, fehler)
            return _done(self, attendance.meeting)
        SessionAttendanceDisruption.objects.create(
            attendance=attendance,
            started_at=started_at,
            ended_at=ended_at,
            cause=_cause(request.POST.get("cause")),
            note=(request.POST.get("note") or "").strip()[:255],
        )
        if ended_at is None:
            messages.warning(
                request,
                f"Störung bei {attendance.person.display_name} ab {started_at:%H:%M} Uhr vermerkt – "
                "bis zum Ende zählt die Person nicht zur Beschlussfähigkeit.",
            )
        else:
            messages.success(request, f"Störung bei {attendance.person.display_name} vermerkt.")
        # Schnellerfassung aus der Zeile: Seite neu laden (Beschlussfähigkeit, Störungsliste)
        return _done(self, attendance.meeting)


class AttendanceDisruptionUpdateView(SessionViewMixin, View):
    """Störung beenden („jetzt“ oder mit Uhrzeit), korrigieren oder entfernen (Issue #139)."""

    permission_required = "manage_attendance"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, disruption_id):
        disruption = get_object_or_404(
            SessionAttendanceDisruption.objects.select_related("attendance__meeting", "attendance__person").filter(
                meeting_q(self.session_permissions, "attendance__meeting__")
            ),
            pk=disruption_id,
            attendance__meeting__tenant=self.session_tenant,
        )
        meeting = disruption.attendance.meeting
        name = disruption.attendance.person.display_name
        action = request.POST.get("action", "")
        if action == "delete":
            disruption.delete()
            messages.success(request, f"Störungsvermerk bei {name} entfernt.")
            return _meeting_redirect(self, meeting)
        # Nicht lesbare Uhrzeit: nichts ändern – sonst würde aus einer beendeten Störung stillschweigend
        # wieder eine andauernde, und die Person fiele live aus der Beschlussfähigkeit
        ending = action == "end"
        try:
            started_at = disruption.started_at
            if not ending:
                started_at = _time_value(request.POST.get("started_at")) or started_at
            ended_at = _time_value(request.POST.get("ended_at"))
        except _InvalidTimeError:
            messages.error(request, INVALID_TIME)
            return _meeting_redirect(self, meeting)
        # „Jetzt beenden“ nach Mitternacht ist echt (laufende Uhr), auch ohne eingetragenes Sitzungsende
        live = ending and ended_at is None
        if live:
            ended_at = participation_service.now()
        fehler = participation_service.period_error(meeting, started_at, ended_at, live=live)
        if fehler:
            messages.error(request, fehler)
            return _meeting_redirect(self, meeting)
        disruption.started_at, disruption.ended_at = started_at, ended_at
        if not ending:
            disruption.cause = _cause(request.POST.get("cause"), disruption.cause)
            disruption.note = (request.POST.get("note") or "").strip()[:255]
        disruption.save()
        if disruption.ended_at is None:
            messages.info(request, f"Störungsvermerk bei {name} gespeichert – die Störung dauert an.")
        else:
            messages.success(request, f"Störung bei {name} beendet ({disruption.ended_at:%H:%M} Uhr).")
        return _meeting_redirect(self, meeting)
