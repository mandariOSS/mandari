# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

import contextlib

from django import forms
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

from apps.common.params import date_param, uuid_param

from ..models import (
    SessionAttendance,
    SessionAttendanceDisruption,
    SessionMeeting,
    SessionOrganization,
    SessionPerson,
)
from ..permissions import SessionViewMixin
from ..services import (
    agenda_template_service,
    body_service,
    joint_meeting_service,
    meeting_format_service,
    state_law_service,
)
from ..visibility import paper_visible
from .bodies import BodyFilterMixin

# =============================================================================
# MEETINGS
# =============================================================================


def _joint_selected(form) -> set[str]:
    """Gewählte weitere Gremien als Zeichenketten (Formular-Vorbelegung, Issue #317)."""
    return {str(value) for value in (form["joint_organizations"].value() or [])}


def _joint_valid(form) -> bool:
    """
    Das federführende Gremium ist nicht zugleich weiteres Gremium der gemeinsamen Sitzung, und alle Gremien
    gehören zu einer Körperschaft (Issue #756) – sonst wiese erst die Zuordnung selbst sie zurück.
    """
    lead = form.cleaned_data.get("organization")
    joint = list(form.cleaned_data.get("joint_organizations") or [])
    if lead is not None and lead in joint:
        form.add_error("joint_organizations", "Das federführende Gremium ist bereits beteiligt – bitte abwählen.")
        return False
    if lead is not None and joint:
        others = SessionOrganization.objects.filter(pk__in=[org.pk for org in joint])
        if not body_service.same_body(lead, others):
            form.add_error(
                "joint_organizations", "Gemeinsame Sitzungen sind nur mit Gremien derselben Körperschaft möglich."
            )
            return False
    return True


MEETING_FORM_FIELDS = [
    "name",
    "organization",
    "joint_organizations",
    "start",
    "end",
    "location",
    "room",
    "is_public",
    # Termin einer nichtöffentlichen Sitzung veröffentlichen (Issue #757)
    "date_public",
    # Sitzungsformat (Issue #138)
    "format",
    "format_reason",
    "public_access_url",
    "public_access_note",
    "invitation_text",
]

#: Felder, von denen die Zulässigkeit des Sitzungsformats abhängt
#: Seit Issue #757 auch die Öffentlichkeit (Zuschaltung nur in öffentlichen Sitzungen); ein anderer Sitzungstag
#: (Fassung des Landesprofils, Notlagenbeschluss) prüft ``MeetingForm.clean`` gesondert
FORMAT_RELEVANT_FIELDS = frozenset({"format", "format_reason", "organization", "joint_organizations", "is_public"})


def _format_warnings(request, form) -> None:
    """Hinweise der Formatprüfung (z. B. nicht eingeordneter Ausschuss) nach dem Speichern anzeigen."""
    for warning in getattr(form.format_check, "warnings", None) or []:
        messages.warning(request, warning)


class MeetingForm(forms.ModelForm):
    """
    Sitzungsformular mit Sitzungsformat (Issue #138).

    Das Format wird gegen das Landesprofil des Mandanten geprüft (meeting_format_service.check), bei
    gemeinsamen Sitzungen für alle beteiligten Gremien. Beim Bearbeiten nur, wenn sich Format, Begründung
    oder Gremien ändern: Eine gespeicherte Sitzung bleibt so absag- und bearbeitbar, auch wenn sich das
    Landesprofil inzwischen geändert hat. Der Zugangsweg für Zugeschaltete
    wird nur verschlüsselt gespeichert und bei Präsenzsitzungen verworfen.
    """

    remote_access = forms.CharField(
        label="Zugangsweg für zugeschaltete Mitglieder",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    class Meta:
        model = SessionMeeting
        fields = MEETING_FORM_FIELDS

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        self.format_check: meeting_format_service.FormatCheck | None = None
        self.fields["organization"].queryset = SessionOrganization.objects.filter(
            tenant=tenant, is_active=True
        ).exclude(organization_type="department")
        # Mit Körperschaft (Issue #756): Ab der zweiten steht sie neben dem Gremium – ohne Abfrage je Zeile
        self.fields["joint_organizations"].queryset = joint_meeting_service.selectable_organizations(
            tenant
        ).select_related("body")
        # Ohne Angabe bleibt es bei der Präsenzsitzung (Importe, ältere Formulare)
        self.fields["format"].required = False
        if self.instance.pk:
            self.fields["remote_access"].initial = self.instance.get_remote_access_decrypted()

    def clean_format(self) -> str:
        return str(self.cleaned_data.get("format") or SessionMeeting.FORMAT_PRESENCE)

    def clean(self):
        cleaned = super().clean()
        # Ende nach Beginn prüft das Modell (SessionMeeting.clean) – für dieses Formular wie für den Admin
        lead = cleaned.get("organization")
        if lead is not None and cleaned.get("is_public"):
            # Stets nichtöffentliche Gremien (Issue #757, z. B. Hauptausschuss nach § 78 Abs. 2 NKomVG)
            day = state_law_service.local_day(cleaned.get("start"))
            for org in [lead, *(cleaned.get("joint_organizations") or [])]:
                publicity = state_law_service.publicity(org, day)
                if publicity.locked:
                    self.add_error("is_public", publicity.reason)
                    break
        if cleaned.get("is_public"):
            cleaned["date_public"] = False
        relevant = self.instance._state.adding or bool(FORMAT_RELEVANT_FIELDS.intersection(self.changed_data))
        if not relevant and "start" in self.changed_data and cleaned.get("start") is not None:
            # Verschoben auf einen anderen Tag: Fassung des Landesprofils und Notlagenbeschluss neu prüfen
            relevant = state_law_service.local_day(cleaned["start"]) != state_law_service.local_day(self.instance.start)
        if lead is not None and relevant:
            organizations = [lead, *(org for org in cleaned.get("joint_organizations") or [] if org != lead)]
            self.format_check = meeting_format_service.check(
                self.tenant,
                organizations,
                cleaned.get("format") or SessionMeeting.FORMAT_PRESENCE,
                cleaned.get("format_reason") or "",
                day=state_law_service.local_day(cleaned.get("start")),
                is_public=bool(cleaned.get("is_public")),
            )
            for message in self.format_check.errors:
                self.add_error("format", message)
        return cleaned

    def save(self, commit=True):
        meeting = super().save(commit=False)
        remote_access = self.cleaned_data.get("remote_access", "").strip()
        if meeting.format == SessionMeeting.FORMAT_PRESENCE:
            remote_access = ""
        meeting.set_remote_access_encrypted(remote_access)
        if commit:
            meeting.save()
            self.save_m2m()
        return meeting


class MeetingUpdateForm(MeetingForm):
    """
    Bearbeiten: zusätzlich Status und Absage.

    Status „Abgesagt“ und das Häkchen „Sitzung absagen“ beschreiben dasselbe: Wer nur eines von beiden ändert,
    ändert das andere mit (``SessionMeeting.align_cancellation_input``, wie im Admin).
    """

    class Meta(MeetingForm.Meta):
        fields = [*MEETING_FORM_FIELDS, "meeting_state", "cancelled", "cancellation_reason"]

    def clean(self):
        cleaned = super().clean()
        self.instance.align_cancellation_input(cleaned, self.changed_data)
        return cleaned


class MeetingFormMixin:
    """Formular mit Mandant und Kontext für Sitzungsformat und gemeinsame Sitzung."""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["tenant"] = self.session_tenant
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["joint_selected"] = _joint_selected(context["form"])
        # Körperschaft (Issue #756): Gremien ab der zweiten Körperschaft nach Körperschaft gruppiert – die
        # Sitzung gehört über ihr Gremium zur Körperschaft
        context["body_choice"] = body_service.choice(self.session_tenant)
        context["organization_groups"] = body_service.group_organizations(
            context["form"].fields["organization"].queryset, context["body_choice"]
        )
        context["state_profile"] = self.session_tenant.state_profile
        context["can_manage_settings"] = self.has_permission("manage_settings")
        # Öffentlichkeit je Gremium (Issue #757): Vorgabe und Sperre an den Optionen, das Formular folgt der Auswahl
        tenant = self.session_tenant
        law = state_law_service.effective(tenant.state_profile) if tenant.state_profile is not None else None
        bodies = {body.pk: body for body in body_service.bodies(tenant, include_inactive=True)}
        default_body = next((body for body in bodies.values() if body.is_default), None)
        organizations = list(context["form"].fields["organization"].queryset)
        for org in [*organizations, *(org for _label, orgs in context["organization_groups"] or [] for org in orgs)]:
            publicity = state_law_service.publicity(org, law=law, body=bodies.get(org.body_id) or default_body)
            org.public_code, org.public_locked = ("1" if publicity.public else "0"), publicity.locked
        context["organization_choices"] = organizations
        lead = context["form"].instance.organization if context["form"].instance.organization_id else None
        context["publicity_locked"] = bool(lead is not None and state_law_service.publicity(lead).locked)
        return context


class MeetingListView(BodyFilterMixin, SessionViewMixin, ListView):
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

        # Körperschaft (Issue #756) über das federführende Gremium; Filter und Anzeige erst ab der zweiten
        if self.body_choice.active:
            qs = self.filter_body(qs, "organization__").select_related("organization__body")

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
            from ..models import SessionLegislativeTerm
            from .terms import meeting_term_filter

            # Ungültige oder fremde Kennung: kein Treffer statt Serverfehler
            term_uuid = uuid_param(term_id)
            term = (
                SessionLegislativeTerm.objects.filter(tenant=self.session_tenant, pk=term_uuid).first()
                if term_uuid
                else None
            )
            # Sitzungen ohne zugeordnete Wahlperiode zählen über ihr Datum (wie im Archiv)
            qs = qs.filter(meeting_term_filter(term)) if term else qs.none()

        # Filter by date range (ungültige Datumsangaben werden ignoriert)
        date_from = date_param(self.request.GET.get("from"))
        date_to = date_param(self.request.GET.get("to"))
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
        context["organization_groups"] = body_service.group_organizations(context["organizations"], self.body_choice)
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
        # Gemeinsame Sitzung (Issue #317): weitere Gremien nur laden, wenn es welche gibt. Mandant und Niederschrift
        # gleich mit: Teilnahmeart und Landesprofil lesen den Mandanten, Sperre und Seitenleiste die Niederschrift
        return SessionMeeting.with_joint_flag(
            qs.select_related("organization", "tenant", "protocol", "created_by__user")
        )

    def get_context_data(self, **kwargs):
        from ..services import agenda_service

        context = super().get_context_data(**kwargs)
        meeting = self.object

        # Agenda items — Ö/NÖ-gruppiert, NÖ-Teil nur für Berechtigte
        can_view_np = self.has_permission("view_non_public_meetings")
        agenda = agenda_service.grouped_agenda(meeting, include_non_public=can_view_np)
        context["agenda_public"] = agenda["public"]
        context["agenda_non_public"] = agenda["non_public"]
        # Genehmigte Niederschrift: Tagesordnung und Anwesenheit sind gesperrt – keine Bearbeitungsknöpfe
        protocol = getattr(meeting, "protocol", None)
        context["protocol_locked"] = protocol is not None and protocol.is_locked
        context["agenda_can_edit"] = self.has_permission("edit_meetings") and not context["protocol_locked"]
        # Tagesordnungsvorlagen des Landesprofils, z. B. konstituierende Sitzung (Issue #757) – bis zur Ladung
        planning = self.object.meeting_state in ("draft", "scheduled") and not self.object.invitation_sent_at
        context["agenda_templates"] = (
            agenda_template_service.available(self.session_tenant) if context["agenda_can_edit"] and planning else []
        )
        context["agenda_can_retract"] = self.has_permission("edit_meetings") and context["protocol_locked"]

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

        # Teilnahmeart, Störungen und Hinweise des Landesprofils (Issue #139)
        context.update(attendance_service.attendance_panel(meeting))
        context["attendance_can_manage"] = self.has_permission("manage_attendance") and not context["protocol_locked"]
        context["disruption_causes"] = SessionAttendanceDisruption.CAUSE_CHOICES
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

        # Sitzungsformat (Issue #138): nur für hybride und digitale Sitzungen oder mit Übertragung
        if meeting.format != SessionMeeting.FORMAT_PRESENCE or meeting.public_access_url or meeting.public_access_note:
            context["format_info"] = meeting_format_service.describe(
                meeting, for_members=self.has_permission("edit_meetings")
            )

        # Protocol
        context["protocol"] = protocol

        # Sitzungsmappe (Issue #218): abrufbare Fassungen – reine Rechteprüfung, Stand lädt per HTMX nach
        from ..services.meeting_package_plan import variants_for

        context["package_variants"] = variants_for(context["permission_checker"].permissions, meeting)

        return context


class MeetingCreateView(MeetingFormMixin, SessionViewMixin, CreateView):
    """Create a new meeting."""

    model = SessionMeeting
    template_name = "session/meetings/form.html"
    form_class = MeetingForm
    permission_required = "create_meetings"

    def get_initial(self):
        """Aus einem Gremium heraus angelegt (``?organization=``): Gremium und dessen Öffentlichkeit vorbelegen."""
        initial = super().get_initial()
        from apps.common.params import uuid_param

        org_id = uuid_param(self.request.GET.get("organization", ""))
        org = (
            SessionOrganization.objects.filter(tenant=self.session_tenant, is_active=True, pk=org_id).first()
            if org_id
            else None
        )
        if org is not None:
            publicity = state_law_service.publicity(org)
            initial.update(
                organization=org.pk, is_public=publicity.public, date_public=not publicity.public and org.publish_dates
            )
        return initial

    def form_valid(self, form):
        if not _joint_valid(form):
            return self.form_invalid(form)
        form.instance.tenant = self.session_tenant
        form.instance.created_by = self.session_user
        # Die Wahlperiode leitet SessionMeeting.save() aus dem Sitzungsdatum ab (Issue #39) – für jeden Anlageweg

        messages.success(self.request, "Sitzung wurde erstellt.")
        _format_warnings(self.request, form)
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


class MeetingUpdateView(MeetingFormMixin, SessionViewMixin, UpdateView):
    """Update a meeting."""

    model = SessionMeeting
    template_name = "session/meetings/form.html"
    form_class = MeetingUpdateForm
    pk_url_kwarg = "meeting_id"
    permission_required = "edit_meetings"

    def get_queryset(self):
        qs = super().get_queryset()
        # Ö/NÖ: Nichtöffentliche Sitzungen nur für Berechtigte bearbeitbar
        if not self.has_permission("view_non_public_meetings"):
            qs = qs.filter(is_public=True)
        return qs

    def form_valid(self, form):
        if not _joint_valid(form):
            return self.form_invalid(form)
        # Verschoben: Wahlperiode nachführen, wenn das neue Datum außerhalb der bisherigen liegt (Issue #39)
        if "start" in form.changed_data:
            form.instance.assign_legislative_term()
        messages.success(self.request, "Sitzung wurde aktualisiert.")
        _format_warnings(self.request, form)
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "session:meeting_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "meeting_id": self.object.id,
            },
        )
