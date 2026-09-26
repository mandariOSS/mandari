# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

import contextlib

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.urls import reverse
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    UpdateView,
)

from ..models import (
    SessionAttendance,
    SessionMeeting,
    SessionOrganization,
    SessionPerson,
)
from ..permissions import SessionViewMixin
from ..services import joint_meeting_service
from ..visibility import paper_visible

# =============================================================================
# MEETINGS
# =============================================================================


def _joint_selected(form) -> set[str]:
    """Gewählte weitere Gremien als Zeichenketten (Formular-Vorbelegung, Issue #317)."""
    return {str(value) for value in (form["joint_organizations"].value() or [])}


def _joint_valid(form) -> bool:
    """Das federführende Gremium ist nicht zugleich weiteres Gremium der gemeinsamen Sitzung."""
    lead = form.cleaned_data.get("organization")
    if lead is not None and lead in form.cleaned_data.get("joint_organizations", []):
        form.add_error("joint_organizations", "Das federführende Gremium ist bereits beteiligt – bitte abwählen.")
        return False
    return True


class MeetingListView(SessionViewMixin, ListView):
    """List of meetings."""

    model = SessionMeeting
    template_name = "session/meetings/list.html"
    context_object_name = "meetings"
    paginate_by = 20
    permission_required = "view_meetings"

    def get_queryset(self):
        qs = super().get_queryset()
        # Gemeinsame Sitzungen (Issue #317) vorab markieren – weitere Gremien nur für diese nachladen
        qs = SessionMeeting.with_joint_flag(qs.select_related("organization")).order_by("-start")

        # Ö/NÖ: Nichtöffentliche Sitzungen nur für Berechtigte
        if not self.has_permission("view_non_public_meetings"):
            qs = qs.filter(is_public=True)

        # Filter nach Gremium: federführend oder als weiteres Gremium beteiligt (Issue #317)
        org_id = self.request.GET.get("organization")
        if org_id:
            organization = None
            with contextlib.suppress(ValueError, DjangoValidationError):
                organization = SessionOrganization.objects.filter(tenant=self.session_tenant, pk=org_id).first()
            qs = qs.filter(SessionMeeting.organization_q(organization)).distinct() if organization else qs.none()

        # Filter by state
        state = self.request.GET.get("state")
        if state:
            qs = qs.filter(meeting_state=state)

        # Perioden-Filter (Issue #39)
        term_id = self.request.GET.get("term")
        if term_id:
            qs = qs.filter(legislative_term_id=term_id)

        # Filter by date range
        date_from = self.request.GET.get("from")
        date_to = self.request.GET.get("to")
        if date_from:
            qs = qs.filter(start__date__gte=date_from)
        if date_to:
            qs = qs.filter(start__date__lte=date_to)

        # Search
        search = self.request.GET.get("q")
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(organization__name__icontains=search)
                | Q(joint_organizations__name__icontains=search)
            ).distinct()

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        joint_meeting_service.prefetch_joint(context["meetings"])
        context["organizations"] = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        ).order_by("name")
        context["meeting_states"] = SessionMeeting._meta.get_field("meeting_state").choices

        # Perioden-Filter (Issue #39)
        from ..models import SessionLegislativeTerm

        context["legislative_terms"] = SessionLegislativeTerm.objects.filter(tenant=self.session_tenant)
        context["selected_term"] = self.request.GET.get("term", "")
        return context


class MeetingDetailView(SessionViewMixin, DetailView):
    """Meeting detail view."""

    model = SessionMeeting
    template_name = "session/meetings/detail.html"
    context_object_name = "meeting"
    pk_url_kwarg = "meeting_id"
    permission_required = "view_meetings"

    def get_queryset(self):
        qs = super().get_queryset()
        # Ö/NÖ: Nichtöffentliche Sitzungen nur für Berechtigte
        if not self.has_permission("view_non_public_meetings"):
            qs = qs.filter(is_public=True)
        # Gemeinsame Sitzung (Issue #317): weitere Gremien nur laden, wenn es welche gibt
        return SessionMeeting.with_joint_flag(qs.select_related("organization", "created_by__user"))

    def get_context_data(self, **kwargs):
        from ..services import agenda_service

        context = super().get_context_data(**kwargs)
        meeting = self.object

        # Agenda items — Ö/NÖ-gruppiert, NÖ-Teil nur für Berechtigte
        can_view_np = self.has_permission("view_non_public_meetings")
        agenda = agenda_service.grouped_agenda(meeting, include_non_public=can_view_np)
        context["agenda_public"] = agenda["public"]
        context["agenda_non_public"] = agenda["non_public"]
        context["agenda_can_edit"] = self.has_permission("edit_meetings")

        # Lesezugriff auf Nichtöffentliches protokollieren (Issue #221): nur Objekt, nie Inhalt
        if not meeting.is_public or agenda["non_public"]:
            from .. import audit

            audit.log_read(
                self.request,
                meeting,
                tenant=self.session_tenant,
                user=self.session_user,
                changes={"umfang": "nichtöffentliche Sitzung" if not meeting.is_public else "nichtöffentlicher Teil"},
            )

        # Beratungsfolge (Issue #34): Kette je Vorlagen-TOP anzeigen, damit
        # z. B. das Vorberatungsergebnis in der Ratssitzung sichtbar ist.
        from ..models import SessionConsultation

        all_items = []
        for top in context["agenda_public"] + context["agenda_non_public"]:
            all_items.append(top)
            all_items.extend(getattr(top, "children_list", []))
        # Vorlage eines TOPs nur, wenn die Person sie sehen darf – das Sitzungs-NÖ-Recht allein nennt
        # keine nichtöffentliche Vorlage (Nummer, Link, Beratungsfolge)
        permissions = self.session_permissions
        for item in all_items:
            item.paper_visible = item.paper is not None and paper_visible(permissions, item.paper)
        paper_ids = {item.paper_id for item in all_items if item.paper_id and item.paper_visible}
        if paper_ids:
            chains = {}
            stations = (
                SessionConsultation.objects.filter(paper_id__in=paper_ids)
                .select_related("organization", "meeting")
                .order_by("paper_id", "order", "created_at")
            )
            for station in stations:
                chains.setdefault(station.paper_id, []).append(station)
            for item in all_items:
                if item.paper_id and item.paper_visible:
                    item.consultation_chain = chains.get(item.paper_id, [])

        # Attendances (Issue #30): Schnellerfassung, Quorum, Gäste-Ergänzung
        from ..services import attendance_service

        context["attendances"] = meeting.attendances.select_related("person").order_by("person__family_name")
        context["attendance_can_manage"] = self.has_permission("manage_attendance")
        context["quorum"] = attendance_service.quorum_status(meeting)
        if context["attendance_can_manage"]:
            present_ids = meeting.attendances.values_list("person_id", flat=True)
            context["addable_persons"] = (
                SessionPerson.objects.filter(tenant=self.session_tenant, is_active=True)
                .exclude(pk__in=present_ids)
                .order_by("family_name", "given_name")
            )
            context["attendance_roles"] = SessionAttendance._meta.get_field("role").choices

        # Files — NÖ-Anlagen nur für Berechtigte sichtbar
        files = meeting.files.order_by("name")
        if not self.has_permission("view_non_public_meetings"):
            files = files.filter(is_public=True)
        context["files"] = list(files)
        context["file_can_edit"] = self.has_permission("edit_meetings")

        # Protocol
        context["protocol"] = getattr(meeting, "protocol", None)

        # Sitzungsmappe (Issue #218): abrufbare Fassungen – reine Rechteprüfung, Stand lädt per HTMX nach
        from ..services.meeting_package_plan import variants_for

        context["package_variants"] = variants_for(context["permission_checker"].permissions, meeting)

        return context


class MeetingCreateView(SessionViewMixin, CreateView):
    """Create a new meeting."""

    model = SessionMeeting
    template_name = "session/meetings/form.html"
    fields = [
        "name",
        "organization",
        "joint_organizations",
        "start",
        "end",
        "location",
        "room",
        "is_public",
        "invitation_text",
    ]
    permission_required = "create_meetings"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Limit organization choices to current tenant
        form.fields["organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        ).exclude(organization_type="department")
        form.fields["joint_organizations"].queryset = joint_meeting_service.selectable_organizations(
            self.session_tenant
        )
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["joint_selected"] = _joint_selected(context["form"])
        return context

    def form_valid(self, form):
        if not _joint_valid(form):
            return self.form_invalid(form)
        form.instance.tenant = self.session_tenant
        form.instance.created_by = self.session_user

        # Wahlperiode automatisch aus dem Sitzungsdatum ableiten (Issue #39)
        if form.instance.legislative_term_id is None and form.instance.start:
            from ..models import SessionLegislativeTerm

            form.instance.legislative_term = SessionLegislativeTerm.for_date(
                self.session_tenant, form.instance.start.date()
            )

        messages.success(self.request, "Sitzung wurde erstellt.")
        response = super().form_valid(form)

        # Standard-TOPs des Gremiums automatisch übernehmen (Issue #85)
        from ..services import textblock_service

        applied = textblock_service.apply_standard_items(self.object)
        if applied:
            messages.info(
                self.request,
                f"{applied} Standard-Tagesordnungspunkt(e) wurden übernommen.",
            )
        return response

    def get_success_url(self):
        return reverse(
            "session:meeting_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "meeting_id": self.object.id,
            },
        )


class MeetingUpdateView(SessionViewMixin, UpdateView):
    """Update a meeting."""

    model = SessionMeeting
    template_name = "session/meetings/form.html"
    fields = [
        "name",
        "organization",
        "joint_organizations",
        "start",
        "end",
        "location",
        "room",
        "is_public",
        "meeting_state",
        "invitation_text",
        "cancelled",
        "cancellation_reason",
    ]
    pk_url_kwarg = "meeting_id"
    permission_required = "edit_meetings"

    def get_queryset(self):
        qs = super().get_queryset()
        # Ö/NÖ: Nichtöffentliche Sitzungen nur für Berechtigte bearbeitbar
        if not self.has_permission("view_non_public_meetings"):
            qs = qs.filter(is_public=True)
        return qs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        ).exclude(organization_type="department")
        form.fields["joint_organizations"].queryset = joint_meeting_service.selectable_organizations(
            self.session_tenant
        )
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["joint_selected"] = _joint_selected(context["form"])
        return context

    def form_valid(self, form):
        if not _joint_valid(form):
            return self.form_invalid(form)
        messages.success(self.request, "Sitzung wurde aktualisiert.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "session:meeting_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "meeting_id": self.object.id,
            },
        )
