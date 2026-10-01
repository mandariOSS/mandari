# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tagesordnungs-Verwaltung für das Session RIS (Issue #26).

Vollständiges TOP-Management: Anlegen, Bearbeiten, Absetzen (dokumentiert
statt gelöscht), Löschen, Umsortieren (Drag-and-drop + Auf/Ab) mit
automatischer Ö/NÖ-getrennter Neu-Nummerierung, Unterpunkten (5.1, 5.2)
und Nachtrags-Kennzeichnung nach Ladungsversand.

Nach der Genehmigung der Niederschrift sind Tagesordnung und Anwesenheit gesperrt
(``protocol_lock``); die Views melden das vorab, statt in die Sperre des Modells zu laufen.
"""

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import (
    CreateView,
    UpdateView,
)

from ..models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionMeeting,
    SessionPaper,
)
from ..permissions import SessionViewMixin
from ..services import agenda_service, participation_service, protocol_lock
from ..visibility import meeting_q
from .attendance import SessionAttendanceForm

# =============================================================================
# HELPERS
# =============================================================================


def _meetings(view):
    """Sitzungen des Mandanten, nichtöffentliche nur mit dem NÖ-Sichtrecht (wie die Sitzungsansicht)."""
    return SessionMeeting.objects.filter(tenant=view.session_tenant).visible_to(view.session_permissions)


def _items(view):
    """TOPs des Mandanten; nichtöffentliche TOPs und TOPs nichtöffentlicher Sitzungen nur mit NÖ-Sichtrecht.

    Das Bearbeitungsrecht allein genügt nicht: Sonst ließen sich Betreff und Vorlage eines NÖ-TOPs
    über die Bearbeitungsseite lesen bzw. der TOP absetzen, löschen oder verschieben.
    """
    return (
        SessionAgendaItem.objects.filter(meeting__tenant=view.session_tenant)
        .visible_to(view.session_permissions)
        .select_related("meeting")
    )


def _papers(view):
    """Auswahl „Vorlage“: nichtöffentliche Vorlagen nur mit dem NÖ-Sichtrecht für Vorlagen."""
    return SessionPaper.objects.filter(tenant=view.session_tenant).visible_to(view.session_permissions)


def _get_meeting(view, meeting_id):
    return get_object_or_404(_meetings(view), pk=meeting_id)


def _get_item(view, item_id):
    return get_object_or_404(_items(view), pk=item_id)


def _meeting_redirect(view, meeting):
    return redirect(
        "session:meeting_detail",
        tenant_slug=view.session_tenant.slug,
        meeting_id=meeting.id,
    )


def _locked(view, meeting_id, message=protocol_lock.MESSAGE_AGENDA):
    """
    Genehmigte Niederschrift: Hinweis auf der Sitzungsseite statt Änderung (bzw. statt einer 403-Seite
    aus der Sperre im Modell). Gibt die Antwort zurück oder ``None``, wenn die Sitzung offen ist.
    """
    if not protocol_lock.is_locked(meeting_id):
        return None
    messages.error(view.request, message)
    if view.is_htmx:
        return HttpResponse(status=204, headers={"HX-Refresh": "true"})
    return redirect("session:meeting_detail", tenant_slug=view.session_tenant.slug, meeting_id=meeting_id)


# =============================================================================
# AGENDA ITEMS
# =============================================================================


class AgendaItemCreateView(SessionViewMixin, CreateView):
    """Create a new agenda item via HTMX."""

    model = SessionAgendaItem
    template_name = "session/partials/agenda_item_form.html"
    fields = ["name", "is_public", "paper", "parent"]
    permission_required = "edit_meetings"

    def get_form(self, form_class=None):
        # Sitzung des Mandanten mit Sichtrecht – schon für das Formular, nicht erst beim Speichern
        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        form = super().get_form(form_class)
        form.fields["paper"].queryset = _papers(self)
        form.fields["parent"].queryset = _items(self).filter(meeting=meeting, parent__isnull=True).order_by("order")
        return form

    def form_valid(self, form):
        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        gesperrt = _locked(self, meeting.pk)
        if gesperrt:
            return gesperrt
        for feld, meldung in agenda_service.visibility_errors(form.instance).items():
            form.add_error(feld, meldung)
        if form.errors:
            return self.form_invalid(form)
        form.instance.meeting = meeting
        # Vor den Ende-TOPs (z. B. „Verschiedenes“) einreihen, nicht dahinter
        form.instance.order = agenda_service.insertion_order(
            meeting, is_public=form.instance.is_public, parent_id=form.instance.parent_id
        )
        form.instance.number = "?"  # wird durch renumber_agenda gesetzt

        # Nachtrag: nach Versand der Ladung hinzugefügte TOPs kennzeichnen
        if meeting.invitation_sent_at or meeting.meeting_state == "invitation_sent":
            form.instance.is_supplementary = True

        self.object = form.save()
        agenda_service.renumber_agenda(meeting)

        if self.is_htmx:
            return HttpResponse(
                status=204,
                headers={"HX-Trigger": "agendaItemCreated", "HX-Refresh": "true"},
            )
        return _meeting_redirect(self, meeting)


class AgendaItemUpdateView(SessionViewMixin, UpdateView):
    """TOP bearbeiten (Betreff, Ö/NÖ, Vorlagenzuordnung, Unterpunkt-Zuordnung)."""

    model = SessionAgendaItem
    template_name = "session/meetings/agenda_form.html"
    fields = ["name", "is_public", "paper", "parent"]
    pk_url_kwarg = "item_id"
    permission_required = "edit_meetings"

    def get_queryset(self):
        return _items(self)

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        item = self.object
        # Lesezugriff auf einen nichtöffentlichen TOP protokollieren (Issue #221): nur Objekt, nie Inhalt
        if not item.is_public or not item.meeting.is_public:
            from .. import audit

            audit.log_read(
                request,
                item,
                tenant=self.session_tenant,
                user=self.session_user,
                changes={"umfang": "nichtöffentlicher Tagesordnungspunkt"},
            )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Genehmigte Niederschrift: nur noch Rücknahme auf nichtöffentlich (protocol_lock.is_retraction)
        context["protocol_locked"] = protocol_lock.is_locked(self.object.meeting_id)
        return context

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["paper"].queryset = _papers(self)
        form.fields["parent"].queryset = (
            _items(self)
            .filter(meeting=self.object.meeting, parent__isnull=True)
            .exclude(pk=self.object.pk)
            .order_by("order")
        )
        return form

    def form_valid(self, form):
        # Ein TOP mit Unterpunkten kann nicht selbst Unterpunkt werden
        if form.instance.parent_id and form.instance.sub_items.exists():
            form.add_error("parent", "Ein TOP mit Unterpunkten kann nicht selbst Unterpunkt sein.")
            return self.form_invalid(form)
        for feld, meldung in agenda_service.visibility_errors(form.instance).items():
            form.add_error(feld, meldung)
        if form.errors:
            return self.form_invalid(form)
        # Genehmigte Niederschrift: Nur die Rücknahme auf nichtöffentlich bleibt möglich, Nummer und Platz bleiben
        locked = protocol_lock.is_locked(form.instance.meeting_id)
        if locked and not (form.changed_data == ["is_public"] and not form.instance.is_public):
            messages.error(self.request, protocol_lock.MESSAGE_RETRACT_ONLY)
            return redirect(self.get_success_url())
        # Wechsel Ö <-> NÖ: im Zielteil hinter den regulären TOPs einreihen, aber vor den Ende-TOPs
        # („Verschiedenes“) – wie ein neu ergänzter TOP
        if "is_public" in form.changed_data and not locked:
            form.instance.order = agenda_service.insertion_order(
                form.instance.meeting, is_public=form.instance.is_public, parent_id=form.instance.parent_id
            )
        response = super().form_valid(form)
        if "is_public" in form.changed_data:
            agenda_service.cascade_visibility(self.object)
        agenda_service.renumber_agenda(self.object.meeting)
        messages.success(self.request, f"TOP „{self.object.name}“ wurde aktualisiert.")
        return response

    def get_success_url(self):
        return reverse(
            "session:meeting_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "meeting_id": self.object.meeting_id,
            },
        )


class AgendaItemWithdrawView(SessionViewMixin, View):
    """TOP absetzen (dokumentiert statt gelöscht) bzw. Absetzung aufheben."""

    permission_required = "edit_meetings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, item_id):
        item = _get_item(self, item_id)
        gesperrt = _locked(self, item.meeting_id)
        if gesperrt:
            return gesperrt
        if request.POST.get("restore") == "1":
            item.is_withdrawn = False
            item.withdrawn_reason = ""
            item.save()
            messages.success(request, f"Absetzung von TOP {item.number} wurde aufgehoben.")
        else:
            item.is_withdrawn = True
            item.withdrawn_reason = request.POST.get("reason", "").strip()
            item.save()  # Audit: withdraw-Aktion über Signal
            messages.success(request, f"TOP {item.number} „{item.name}“ wurde abgesetzt.")
        return _meeting_redirect(self, item.meeting)


class AgendaItemDeleteView(SessionViewMixin, View):
    """TOP löschen (für versehentlich angelegte Punkte; Absetzen bevorzugen)."""

    permission_required = "edit_meetings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, item_id):
        item = _get_item(self, item_id)
        gesperrt = _locked(self, item.meeting_id, protocol_lock.MESSAGE_DELETE_ITEM)
        if gesperrt:
            return gesperrt
        meeting = item.meeting
        name = f"TOP {item.number} „{item.name}“"
        item.delete()  # Audit: delete-Eintrag über Signal (auch für Unterpunkte via CASCADE)
        agenda_service.renumber_agenda(meeting)
        messages.success(request, f"{name} wurde gelöscht.")
        return _meeting_redirect(self, meeting)


class AgendaItemMoveView(SessionViewMixin, View):
    """TOP per Auf-/Ab-Schaltfläche innerhalb seines Teils verschieben."""

    permission_required = "edit_meetings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, item_id):
        item = _get_item(self, item_id)
        gesperrt = _locked(self, item.meeting_id)
        if gesperrt:
            return gesperrt
        direction = request.POST.get("direction")
        if direction not in ("up", "down"):
            messages.error(request, "Ungültige Richtung.")
        else:
            agenda_service.move_item(item, direction)
        return _meeting_redirect(self, item.meeting)


class AgendaReorderView(SessionViewMixin, View):
    """Drag-and-drop-Reihenfolge übernehmen (Liste von TOP-IDs)."""

    permission_required = "edit_meetings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, meeting_id):
        meeting = _get_meeting(self, meeting_id)
        if protocol_lock.is_locked(meeting.pk):
            messages.error(request, protocol_lock.MESSAGE_AGENDA)
            if self.is_htmx or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "message": protocol_lock.MESSAGE_AGENDA}, status=409)
            return _meeting_redirect(self, meeting)
        raw = request.POST.get("order", "")
        ordered_ids = [part.strip() for part in raw.split(",") if part.strip()]
        agenda_service.apply_order(meeting, ordered_ids)
        if self.is_htmx or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": True})
        return _meeting_redirect(self, meeting)


class AttendanceUpdateView(SessionViewMixin, UpdateView):
    """Anwesenheitszeile speichern (Schnellerfassung per HTMX, nur POST)."""

    model = SessionAttendance
    template_name = "session/partials/attendance_row.html"
    context_object_name = "attendance"  # auch bei ungültiger Eingabe rendert die Zeile
    # Teilnahmeart (Issue #139): nur in hybriden und digitalen Sitzungen zugeschaltet
    form_class = SessionAttendanceForm
    pk_url_kwarg = "attendance_id"
    permission_required = "manage_attendance"
    http_method_names = ["post"]

    def get_queryset(self):
        # Anwesenheit nichtöffentlicher Sitzungen nur mit NÖ-Sichtrecht (wie das Anlegen)
        return SessionAttendance.objects.filter(
            meeting_q(self.session_permissions, "meeting__"), meeting__tenant=self.session_tenant
        )

    def _row_context(self, attendance, form=None):
        """Zeile mit Teilnahmeart-Auswahl und Vermerk (Zuschaltung, Störungen) wie in der Sitzungsansicht."""
        # Spalte „Teilnahme“ wie in der Tabelle, aus der die Zeile kommt (dort kann sie auch in einer
        # Präsenzsitzung stehen, solange jemand zugeschaltet erfasst ist) – sonst verrutscht die Zeile
        mode_column = (
            participation_service.remote_allowed(attendance.meeting) or self.request.POST.get("mode_column") == "1"
        )
        attendance.participation_note = participation_service.participation_note(attendance, show_mode=mode_column)
        return {
            "attendance": attendance,
            "form": form,
            "tenant_slug": self.session_tenant.slug,
            "mode_column": mode_column,
        }

    def form_invalid(self, form):
        return self.render_to_response(self._row_context(self.object, form))

    def post(self, request, *args, **kwargs):
        gesperrt = _locked(self, self.get_object().meeting_id, protocol_lock.MESSAGE_ATTENDANCE)
        return gesperrt or super().post(request, *args, **kwargs)

    def form_valid(self, form):
        attendance = form.save(commit=False)
        # Zu-/Absage durch den Sitzungsdienst: Zeitstempel und Herkunft der Rückmeldung (Issue #225)
        if "status" in form.changed_data and attendance.status in ("confirmed", "declined"):
            attendance.responded_at = timezone.now()
            attendance.response_source = "staff"
        # Zusage nach einer Absage mit Vertretungswunsch: wie bei der Rückmeldung über den Link entfällt der
        # Wunsch samt Grund, und eine bereits angefragte Stellvertretung wird entlastet
        confirmed = "status" in form.changed_data and attendance.status == "confirmed"
        if confirmed:
            attendance.substitute_requested = False
            attendance.set_response_reason_encrypted("")
        attendance.save()
        if confirmed and attendance.substitutes_notified_at is not None:
            from ..services import invitation_response_service

            invitation_response_service.withdraw_substitution(attendance)
        self.object = attendance

        if self.is_htmx:
            return self.render_to_response(self._row_context(self.object))
        return redirect(
            "session:meeting_detail",
            tenant_slug=self.session_tenant.slug,
            meeting_id=self.object.meeting_id,
        )
