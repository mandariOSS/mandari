# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.

Enthält auch den Vorlagen-Freigabelauf (Issue #33): Entwurf ->
Mitzeichnung/Prüfung -> Freigabe bzw. Zurückweisung mit Kommentar,
Arbeitsvorrat „Meine zu prüfenden Vorlagen" und E-Mail-Benachrichtigungen.
Vertreterregelung bewusst einfach: Jede/r mit der Berechtigung
approve_papers kann freigeben — feste Zuordnungen/Mehrstufigkeit folgen
als Ausbaustufe.
"""

import logging

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    UpdateView,
)

from .. import audit
from ..models import (
    SessionConsultation,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionUser,
)
from ..permissions import SessionViewMixin

logger = logging.getLogger(__name__)

# =============================================================================
# PAPERS
# =============================================================================


def _active_text_blocks(tenant, categories=("resolution", "general")):
    """Aktive Textbausteine für die Editor-Auswahl (Issue #85)."""
    from ..models import SessionTextBlock

    return SessionTextBlock.objects.filter(tenant=tenant, is_active=True, category__in=categories)


class PaperListView(SessionViewMixin, ListView):
    """List of papers."""

    model = SessionPaper
    template_name = "session/papers/list.html"
    context_object_name = "papers"
    paginate_by = 20
    permission_required = "view_papers"

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.select_related("main_organization", "originator_organization", "originator_person").order_by(
            "-date", "-created_at"
        )

        # Ö/NÖ: Nichtöffentliche Vorlagen nur für Berechtigte
        if not self.has_permission("view_non_public_papers"):
            qs = qs.filter(is_public=True)

        # Filter by type
        paper_type = self.request.GET.get("type")
        if paper_type:
            qs = qs.filter(paper_type=paper_type)

        # Filter by status
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)

        # Filter by organization
        org_id = self.request.GET.get("organization")
        if org_id:
            qs = qs.filter(Q(main_organization_id=org_id) | Q(originator_organization_id=org_id))

        # Perioden-Filter (Issue #39): Vorlagen über den Zeitraum der Periode
        term_id = self.request.GET.get("term")
        if term_id:
            from ..models import SessionLegislativeTerm
            from .terms import term_date_filter

            term = SessionLegislativeTerm.objects.filter(tenant=self.session_tenant, pk=term_id).first()
            if term is not None:
                qs = qs.filter(term_date_filter(term))

        # Search
        search = self.request.GET.get("q")
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(reference__icontains=search) | Q(main_text__icontains=search))

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["organizations"] = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        ).order_by("name")
        context["paper_types"] = SessionPaper._meta.get_field("paper_type").choices
        context["paper_statuses"] = SessionPaper._meta.get_field("status").choices

        # Perioden-Filter (Issue #39)
        from ..models import SessionLegislativeTerm

        context["legislative_terms"] = SessionLegislativeTerm.objects.filter(tenant=self.session_tenant)
        context["selected_term"] = self.request.GET.get("term", "")
        return context


class PaperDetailView(SessionViewMixin, DetailView):
    """Paper detail view."""

    model = SessionPaper
    template_name = "session/papers/detail.html"
    context_object_name = "paper"
    pk_url_kwarg = "paper_id"
    permission_required = "view_papers"

    def get_queryset(self):
        qs = super().get_queryset()
        # Ö/NÖ: Nichtöffentliche Vorlagen nur für Berechtigte
        if not self.has_permission("view_non_public_papers"):
            qs = qs.filter(is_public=True)
        return qs.select_related(
            "main_organization",
            "originator_organization",
            "originator_person",
            "created_by__user",
            "approved_by__user",
            "source_application",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paper = self.object

        # Files — NÖ-Anlagen nur für Berechtigte sichtbar
        files = paper.files.order_by("name")
        if not self.has_permission("view_non_public_papers"):
            files = files.filter(is_public=True)
        context["files"] = list(files)
        context["file_can_edit"] = self.has_permission("edit_papers")

        # Agenda items (where this paper was discussed)
        context["agenda_items"] = paper.agenda_items.select_related("meeting__organization").order_by("-meeting__start")

        # Beratungsfolge (Issue #34): Stationen + Formulardaten
        context["consultations"] = list(
            paper.consultations.select_related("organization", "meeting", "agenda_item__meeting").order_by(
                "order", "created_at"
            )
        )
        context["consultation_can_edit"] = self.has_permission("edit_papers")
        context["consultation_can_schedule"] = self.has_permission("edit_meetings")
        context["consultation_roles"] = SessionConsultation.ROLE_CHOICES
        context["consultation_results"] = SessionConsultation.RESULT_CHOICES
        if context["consultation_can_edit"] or context["consultation_can_schedule"]:
            context["consultation_organizations"] = SessionOrganization.objects.filter(
                tenant=self.session_tenant, is_active=True
            ).order_by("name")
            # Zielsitzungen: kommende (und kürzlich vergangene) Sitzungen;
            # Ö/NÖ: NÖ-Sitzungen nur für Berechtigte wählbar/sichtbar
            from datetime import timedelta

            meetings = SessionMeeting.objects.filter(
                tenant=self.session_tenant,
                cancelled=False,
                start__gte=timezone.now() - timedelta(days=14),
            )
            if not self.has_permission("view_non_public_meetings"):
                meetings = meetings.filter(is_public=True)
            context["consultation_meetings"] = meetings.select_related("organization").order_by("start")[:200]

        return context


class PaperCreateView(SessionViewMixin, CreateView):
    """Create a new paper."""

    model = SessionPaper
    template_name = "session/papers/form.html"
    fields = [
        "reference",
        "name",
        "paper_type",
        "main_text",
        "resolution_text",
        "is_public",
        "date",
        "deadline",
        "main_organization",
        "originator_organization",
        "originator_person",
    ]
    permission_required = "create_papers"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["text_blocks"] = _active_text_blocks(self.session_tenant)
        return context

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["main_organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        form.fields["originator_organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        form.fields["originator_person"].queryset = SessionPerson.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        return form

    def form_valid(self, form):
        form.instance.tenant = self.session_tenant
        form.instance.created_by = self.session_user
        messages.success(self.request, "Vorlage wurde erstellt.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "session:paper_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "paper_id": self.object.id,
            },
        )


class PaperReviewListView(SessionViewMixin, ListView):
    """Arbeitsvorrat „Meine zu prüfenden Vorlagen" (Status: In Prüfung)."""

    model = SessionPaper
    template_name = "session/papers/review_list.html"
    context_object_name = "papers"
    paginate_by = 50
    permission_required = "approve_papers"

    def get_queryset(self):
        qs = super().get_queryset().filter(status="review")
        if not self.has_permission("view_non_public_papers"):
            qs = qs.filter(is_public=True)
        return qs.select_related("main_organization", "created_by__user").order_by("created_at")


class PaperWorkflowView(SessionViewMixin, View):
    """
    Freigabelauf-Aktionen (Issue #33):

    - submit:  Entwurf -> In Prüfung (Vorlage zur Freigabe vorlegen),
               benachrichtigt alle Freigabeberechtigten per E-Mail
    - approve: In Prüfung -> Freigegeben (setzt approved_by/approved_at)
    - reject:  In Prüfung -> Entwurf (Zurückweisung mit Kommentar),
               benachrichtigt die/den Erstellenden

    Jede Aktion wird im Audit-Log nachvollziehbar protokolliert.
    """

    http_method_names = ["post"]

    TRANSITIONS = {
        "submit": ("draft", "review"),
        "approve": ("review", "approved"),
        "reject": ("review", "draft"),
    }
    ACTION_PERMS = {
        "submit": "edit_papers",
        "approve": "approve_papers",
        "reject": "approve_papers",
    }

    def check_view_permissions(self):
        from django.core.exceptions import PermissionDenied

        action = self.kwargs.get("action")
        permission = self.ACTION_PERMS.get(action)
        if permission is None:
            raise PermissionDenied("Unbekannte Aktion")
        self.permission_required = permission
        self.check_permissions()

    def post(self, request, tenant_slug, paper_id, action):
        qs = SessionPaper.objects.filter(tenant=self.session_tenant)
        if not self.has_permission("view_non_public_papers"):
            qs = qs.filter(is_public=True)
        paper = get_object_or_404(qs, pk=paper_id)

        old_status, new_status = self.TRANSITIONS[action]
        if paper.status != old_status:
            messages.error(
                request,
                f"Aktion nicht möglich: Vorlage ist im Status „{paper.get_status_display()}“.",
            )
            return self._redirect(paper)

        paper.status = new_status

        if action == "submit":
            paper.save()  # Audit: update über Signal
            self._notify_approvers(paper)
            messages.success(request, f"Vorlage {paper.reference} wurde zur Freigabe vorgelegt.")

        elif action == "approve":
            paper.approved_by = self.session_user
            paper.approved_at = timezone.now()
            paper.save()  # Audit: approve-Aktion über Signal
            messages.success(request, f"Vorlage {paper.reference} wurde freigegeben.")

        elif action == "reject":
            comment = request.POST.get("comment", "").strip()
            paper.approved_by = None
            paper.approved_at = None
            paper.save()
            # Audit: Zurückweisung mit Kommentar nachvollziehbar machen
            audit.log_event(
                "update",
                paper,
                user=self.session_user,
                request=request,
                changes={
                    "status": {"alt": old_status, "neu": new_status},
                    "zurueckweisungs_kommentar": comment[:300],
                },
            )
            self._notify_creator(paper, comment)
            messages.success(request, f"Vorlage {paper.reference} wurde mit Anmerkungen zurückgewiesen.")

        return self._redirect(paper)

    def _redirect(self, paper):
        next_url = self.request.POST.get("next", "")
        if next_url.startswith(f"/session/{self.session_tenant.slug}/"):
            return redirect(next_url)
        return redirect(
            "session:paper_detail",
            tenant_slug=self.session_tenant.slug,
            paper_id=paper.id,
        )

    def _approver_emails(self, paper):
        """E-Mails aller Freigabeberechtigten des Mandanten (einfache Vertreterregelung)."""
        approvers = (
            SessionUser.objects.filter(tenant=self.session_tenant, is_active=True)
            .filter(Q(roles__is_admin=True) | Q(roles__can_approve_papers=True))
            .select_related("user")
            .distinct()
        )
        return sorted({su.user.email for su in approvers if su.user.email} - {self.session_user.user.email})

    def _notify_approvers(self, paper):
        from apps.common.email import send_email

        recipients = self._approver_emails(paper)
        if not recipients:
            return
        detail_path = reverse(
            "session:paper_detail",
            kwargs={"tenant_slug": self.session_tenant.slug, "paper_id": paper.id},
        )
        body = (
            f"Guten Tag,\n\n"
            f"die Vorlage {paper.reference} „{paper.name}“ wurde zur Freigabe vorgelegt.\n\n"
            f"Zur Vorlage: {self._absolute_url(detail_path)}\n\n"
            f"Mit freundlichen Grüßen\n{self.session_tenant.name}"
        )
        try:
            send_email(
                subject=f"Vorlage zur Freigabe: {paper.reference}",
                body=body,
                to=recipients,
                fail_silently=False,
            )
        except Exception:
            logger.exception("Freigabe-Benachrichtigung für %s konnte nicht versendet werden.", paper.reference)

    def _notify_creator(self, paper, comment):
        from apps.common.email import send_email

        creator_email = paper.created_by.user.email if paper.created_by else ""
        if not creator_email:
            return
        detail_path = reverse(
            "session:paper_detail",
            kwargs={"tenant_slug": self.session_tenant.slug, "paper_id": paper.id},
        )
        body = (
            f"Guten Tag,\n\n"
            f"die Vorlage {paper.reference} „{paper.name}“ wurde in der Prüfung zurückgewiesen.\n\n"
            + (f"Anmerkung: {comment}\n\n" if comment else "")
            + f"Zur Vorlage: {self._absolute_url(detail_path)}\n\n"
            f"Mit freundlichen Grüßen\n{self.session_tenant.name}"
        )
        try:
            send_email(
                subject=f"Vorlage zurückgewiesen: {paper.reference}",
                body=body,
                to=[creator_email],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Zurückweisungs-Benachrichtigung für %s konnte nicht versendet werden.", paper.reference)

    @staticmethod
    def _absolute_url(path):
        from django.conf import settings as django_settings

        base_url = getattr(django_settings, "SITE_URL", "https://mandari.de").rstrip("/")
        return f"{base_url}{path}"


class PaperUpdateView(SessionViewMixin, UpdateView):
    """Update a paper."""

    model = SessionPaper
    template_name = "session/papers/form.html"
    fields = [
        "reference",
        "name",
        "paper_type",
        "main_text",
        "resolution_text",
        "is_public",
        "status",
        "date",
        "deadline",
        "main_organization",
        "originator_organization",
        "originator_person",
    ]
    pk_url_kwarg = "paper_id"
    permission_required = "edit_papers"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["text_blocks"] = _active_text_blocks(self.session_tenant)
        return context

    def get_queryset(self):
        qs = super().get_queryset()
        # Ö/NÖ: Nichtöffentliche Vorlagen nur für Berechtigte bearbeitbar
        if not self.has_permission("view_non_public_papers"):
            qs = qs.filter(is_public=True)
        return qs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["main_organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        form.fields["originator_organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        form.fields["originator_person"].queryset = SessionPerson.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        return form

    def form_valid(self, form):
        messages.success(self.request, "Vorlage wurde aktualisiert.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "session:paper_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "paper_id": self.object.id,
            },
        )
