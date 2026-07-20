# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tagesordnungs-Verwaltung für das Session RIS (Issue #26).

Vollständiges TOP-Management: Anlegen, Bearbeiten, Absetzen (dokumentiert
statt gelöscht), Löschen, Umsortieren (Drag-and-drop + Auf/Ab) mit
automatischer Ö/NÖ-getrennter Neu-Nummerierung, Unterpunkten (5.1, 5.2)
und Nachtrags-Kennzeichnung nach Ladungsversand.
"""

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
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
from ..services import agenda_service

# =============================================================================
# HELPERS
# =============================================================================


def _get_meeting(view, meeting_id):
    return get_object_or_404(SessionMeeting, pk=meeting_id, tenant=view.session_tenant)


def _get_item(view, item_id):
    return get_object_or_404(
        SessionAgendaItem.objects.select_related("meeting"),
        pk=item_id,
        meeting__tenant=view.session_tenant,
    )


def _meeting_redirect(view, meeting):
    return redirect(
        "session:meeting_detail",
        tenant_slug=view.session_tenant.slug,
        meeting_id=meeting.id,
    )


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
        form = super().get_form(form_class)
        form.fields["paper"].queryset = SessionPaper.objects.filter(tenant=self.session_tenant)
        form.fields["parent"].queryset = SessionAgendaItem.objects.filter(
            meeting_id=self.kwargs["meeting_id"],
            meeting__tenant=self.session_tenant,
            parent__isnull=True,
        ).order_by("order")
        return form

    def form_valid(self, form):
        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        form.instance.meeting = meeting
        form.instance.order = (meeting.agenda_items.count() + 1) * 100
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
        return SessionAgendaItem.objects.filter(meeting__tenant=self.session_tenant).select_related("meeting")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["paper"].queryset = SessionPaper.objects.filter(tenant=self.session_tenant)
        form.fields["parent"].queryset = (
            SessionAgendaItem.objects.filter(
                meeting=self.object.meeting,
                parent__isnull=True,
            )
            .exclude(pk=self.object.pk)
            .order_by("order")
        )
        return form

    def form_valid(self, form):
        # Ein TOP mit Unterpunkten kann nicht selbst Unterpunkt werden
        if form.instance.parent_id and form.instance.sub_items.exists():
            form.add_error("parent", "Ein TOP mit Unterpunkten kann nicht selbst Unterpunkt sein.")
            return self.form_invalid(form)
        # Wechsel Ö <-> NÖ: TOP am Ende des Zielteils einreihen
        if "is_public" in form.changed_data:
            from django.db.models import Max

            max_order = (
                form.instance.meeting.agenda_items.exclude(pk=form.instance.pk).aggregate(m=Max("order"))["m"] or 0
            )
            form.instance.order = max_order + 1
        response = super().form_valid(form)
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
        raw = request.POST.get("order", "")
        ordered_ids = [part.strip() for part in raw.split(",") if part.strip()]
        agenda_service.apply_order(meeting, ordered_ids)
        if request.headers.get("HX-Request") == "true" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": True})
        return _meeting_redirect(self, meeting)


class AttendanceUpdateView(SessionViewMixin, UpdateView):
    """Update attendance status via HTMX."""

    model = SessionAttendance
    template_name = "session/partials/attendance_row.html"
    fields = ["status", "arrival_time", "departure_time", "notes"]
    pk_url_kwarg = "attendance_id"
    permission_required = "manage_attendance"

    def get_queryset(self):
        return SessionAttendance.objects.filter(meeting__tenant=self.session_tenant)

    def form_valid(self, form):
        self.object = form.save()

        if self.is_htmx:
            context = {"attendance": self.object}
            return self.render_to_response(context)
        return redirect(
            "session:meeting_detail",
            tenant_slug=self.session_tenant.slug,
            meeting_id=self.object.meeting_id,
        )
