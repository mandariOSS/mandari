# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.

Enthält auch den Vorlagen-Freigabelauf (Issue #33): Entwurf ->
Mitzeichnung/Prüfung -> Freigabe bzw. Zurückweisung mit Kommentar,
Arbeitsvorrat „Meine zu prüfenden Vorlagen" und E-Mail-Benachrichtigungen.
Jede/r mit der Berechtigung approve_papers kann freigeben; seit Issue #222
gelten zusätzlich das Vier-Augen-Prinzip (je Mandant schaltbar) und
nutzerbezogene Vertretungen (services/four_eyes_service.py,
services/delegation_service.py).
"""

import logging
from typing import Any

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
from ..permissions import SessionViewMixin, role_permissions
from ..services import delegation_service, four_eyes_service
from .nexturl import safe_next_url

logger = logging.getLogger(__name__)


def _deputy_emails(people, paper):
    """
    Vertretungen mit Umfang „Benachrichtigungen“ erhalten Mails in Kopie (Issue #222).

    Nichtöffentliche Vorlagen nur an Vertretungen, die sie selbst sehen dürfen.
    """
    emails = set()
    for deputy, _principal in delegation_service.notification_deputies(people):
        if deputy.user.email and (paper.is_public or "view_non_public_papers" in role_permissions(deputy)):
            emails.add(deputy.user.email)
    return emails


# =============================================================================
# PAPERS
# =============================================================================


class PaperNumberingFormMixin:
    """
    Vorlagennummer im Formular (Issue #150): Die Nummer vergibt der Nummernkreis. Nach der
    Vergabe ist sie unveränderlich; vorher dürfen nur Einstellungsberechtigte eine Nummer
    von Hand setzen (Altbestand aus einem Vorsystem). Der Status wechselt über den Freigabelauf,
    nicht über das Bearbeiten-Formular.
    """

    #: Status, die im Bearbeiten-Formular zusätzlich zum aktuellen wählbar sind
    MANUELLE_STATUS = ("withdrawn", "completed")

    def _prepare_numbering(self, form: Any) -> Any:
        instance = form.instance
        if "reference" in form.fields:
            if instance.reference or not self.has_permission("manage_settings"):
                del form.fields["reference"]
            else:
                feld = form.fields["reference"]
                feld.required = False
                feld.label = self.session_tenant.reference_label
                feld.help_text = "Leer lassen für die automatische Vergabe – nur für Altbestände ausfüllen."
        if "status" in form.fields:
            erlaubt = {instance.status, *self.MANUELLE_STATUS}
            if instance.status == "withdrawn":
                erlaubt.add("draft")
            feld = form.fields["status"]
            feld.choices = [(wert, text) for wert, text in feld.choices if wert in erlaubt]
        return form

    def _numbering_context(self, context: dict[str, Any]) -> dict[str, Any]:
        from ..services import numbering_service

        paper = getattr(self, "object", None)
        typ = paper.paper_type if paper else "proposal"
        rng = numbering_service.range_for(self.session_tenant, typ)
        context["reference_label"] = self.session_tenant.reference_label
        if rng is None:
            context["numbering_hint"] = "Für diese Vorlagenart ist kein Nummernkreis eingerichtet."
        elif rng.assign_on == "release":
            context["numbering_hint"] = (
                f"Die Nummer wird bei der Freigabe vergeben (Nummernkreis „{rng.name}“, "
                f"nächste {numbering_service.preview(rng, paper)})."
            )
        else:
            context["numbering_hint"] = (
                f"Die Nummer wird beim Speichern vergeben (Nummernkreis „{rng.name}“, "
                f"nächste {numbering_service.preview(rng, paper)})."
            )
        return context

    def _save_with_numbering(self, form: Any, erfolg: str) -> Any:
        from ..services.numbering_service import NumberingError

        ref = (form.cleaned_data.get("reference") or "").strip()
        if (
            ref
            and SessionPaper.objects.filter(tenant=self.session_tenant, reference=ref)
            .exclude(pk=form.instance.pk)
            .exists()
        ):
            form.add_error("reference", f"{self.session_tenant.reference_label} {ref} ist bereits vergeben.")
            return self.form_invalid(form)
        try:
            response = super().form_valid(form)
        except NumberingError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(
            self.request, f"{erfolg} {self.session_tenant.reference_label}: {self.object.display_reference}."
        )
        return response


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
            "approved_on_behalf_of__user",
            "content_edited_by__user",
            "source_application",
            "parent_paper",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paper = self.object

        # Lesezugriff auf eine nichtöffentliche Vorlage protokollieren (Issue #221): nur Objekt, nie Inhalt
        if not paper.is_public:
            from .. import audit

            audit.log_read(
                self.request,
                paper,
                tenant=self.session_tenant,
                user=self.session_user,
                changes={"umfang": "nichtöffentliche Vorlage"},
            )

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

        # Mitzeichnungslauf (Issue #81)
        from ..services import cosign_service

        cosignatures = list(paper.cosignatures.select_related("department", "decided_by__user"))
        for cosignature in cosignatures:
            cosignature.actionable = (
                paper.status == "review"
                and cosign_service.is_actionable(cosignature)
                and cosign_service.can_decide(self.session_user, cosignature)
            )
        context["cosignatures"] = cosignatures
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

        # Bezüge (Issue #150): Unternummern wie Ergänzung, Neufassung, Antwort
        children = paper.child_papers.order_by("sub_number", "created_at")
        if not self.has_permission("view_non_public_papers"):
            children = children.filter(is_public=True)
        context["child_papers"] = list(children)
        context["relation_choices"] = SessionPaper.RELATION_CHOICES
        context["can_create_papers"] = self.has_permission("create_papers")
        # Vier-Augen-Prinzip und Vertretung (Issue #222): Hinweis statt wirkungslosem Knopf
        if paper.status == "review" and self.has_permission("approve_papers"):
            context["freigabe"] = four_eyes_service.evaluate(four_eyes_service.PROCESS_PAPER, paper, self.session_user)

        # Fassungen (Issue #226): neueste und beschlossene Fassung für die Seitenleiste
        from ..services import paper_version_service

        context.update(paper_version_service.detail_context(paper, context["permission_checker"].permissions))
        return context


class PaperChildCreateView(SessionViewMixin, View):
    """
    Unternummer anlegen (Issue #150): Ergänzung, Neufassung, Änderungsantrag, Antwort oder
    Beschlussempfehlung zu einer Vorlage. Die neue Vorlage bekommt sofort die Unternummer der
    Bezugsvorlage (z. B. 22-0593.1) und öffnet sich zur Bearbeitung.
    """

    http_method_names = ["post"]
    permission_required = "create_papers"

    def post(self, request, tenant_slug, paper_id):
        from ..services.numbering_service import ART_JE_BEZUG, NumberingError

        qs = SessionPaper.objects.filter(tenant=self.session_tenant)
        if not self.has_permission("view_non_public_papers"):
            qs = qs.filter(is_public=True)
        parent = get_object_or_404(qs, pk=paper_id)
        relation = request.POST.get("relation_type", "")
        labels = dict(SessionPaper.RELATION_CHOICES)
        if relation not in labels:
            messages.error(request, "Bitte die Art des Bezugs wählen.")
            return redirect("session:paper_detail", tenant_slug=tenant_slug, paper_id=parent.id)
        if not parent.reference:
            messages.error(
                request,
                f"Die Bezugsvorlage hat noch keine {self.session_tenant.reference_label} – "
                "Unternummern entstehen erst danach.",
            )
            return redirect("session:paper_detail", tenant_slug=tenant_slug, paper_id=parent.id)
        try:
            child = SessionPaper.objects.create(
                tenant=self.session_tenant,
                parent_paper=parent,
                relation_type=relation,
                name=f"{labels[relation]} zu {parent.reference}: {parent.name}"[:500],
                paper_type=ART_JE_BEZUG.get(relation, parent.paper_type),
                is_public=parent.is_public,
                main_organization=parent.main_organization,
                lead_department=parent.lead_department,
                date=timezone.localdate(),
                created_by=self.session_user,
            )
        except NumberingError as exc:
            messages.error(request, str(exc))
            return redirect("session:paper_detail", tenant_slug=tenant_slug, paper_id=parent.id)
        messages.success(
            request, f"{labels[relation]} angelegt – {self.session_tenant.reference_label} {child.display_reference}."
        )
        return redirect("session:paper_edit", tenant_slug=tenant_slug, paper_id=child.id)


class PaperCreateView(PaperNumberingFormMixin, SessionViewMixin, CreateView):
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
        "lead_department",
        "has_financial_impact",
        "financial_impact_note",
        "originator_organization",
        "originator_person",
    ]
    permission_required = "create_papers"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["text_blocks"] = _active_text_blocks(self.session_tenant)
        return self._numbering_context(context)

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
        form.fields["lead_department"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True, organization_type="department"
        )
        return self._prepare_numbering(form)

    def form_valid(self, form):
        form.instance.tenant = self.session_tenant
        form.instance.created_by = self.session_user
        return self._save_with_numbering(form, "Vorlage wurde erstellt.")

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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Vier-Augen-Prinzip und Vertretung (Issue #222) je Vorlage
        for paper in context["papers"]:
            paper.freigabe = four_eyes_service.evaluate(four_eyes_service.PROCESS_PAPER, paper, self.session_user)
        context["my_delegations"] = delegation_service.incoming(self.session_user)
        return context


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

        from ..services import cosign_service

        # Pflichtangabe „Finanzielle Auswirkungen" vor dem Freigabelauf (Issue #81)
        if action == "submit" and paper.has_financial_impact is None:
            messages.error(
                request,
                "Bitte zuerst angeben, ob die Vorlage finanzielle Auswirkungen hat (Vorlage bearbeiten).",
            )
            return self._redirect(paper)

        # Freigabe erst nach vollständiger Mitzeichnung (Issue #81)
        if action == "approve":
            blockers = list(cosign_service.pending_blockers(paper).select_related("department")[:5])
            if blockers:
                names = ", ".join(b.department.name for b in blockers)
                messages.error(
                    request,
                    f"Freigabe nicht möglich — Mitzeichnung noch offen: {names}.",
                )
                return self._redirect(paper)

        # Vier-Augen-Prinzip und Vertretung (Issue #222): Prüfung im Service, vor jeder Änderung
        vertreten = None
        if action in ("approve", "reject"):
            try:
                vertreten = four_eyes_service.authorize(
                    four_eyes_service.PROCESS_PAPER, paper, self.session_user, four_eyes=action == "approve"
                )
            except four_eyes_service.ApprovalError as exc:
                messages.error(request, str(exc))
                return self._redirect(paper)
        vermerk = f" (in Vertretung für {vertreten.user.email})" if vertreten else ""

        paper.status = new_status

        if action == "submit":
            paper.save()  # Audit: update über Signal
            # Mitzeichnungskette aus den Regeln aufbauen (Issue #81)
            chain_count = cosign_service.build_chain(paper)
            self._notify_approvers(paper)
            if chain_count:
                messages.success(
                    request,
                    f"Vorlage {paper.display_reference} wurde zur Freigabe vorgelegt "
                    f"({chain_count} Mitzeichnung(en) erforderlich).",
                )
            else:
                messages.success(request, f"Vorlage {paper.display_reference} wurde zur Freigabe vorgelegt.")

        elif action == "approve":
            from ..services.numbering_service import NumberingError

            paper.approved_by = self.session_user
            paper.approved_on_behalf_of = vertreten
            paper.approved_at = timezone.now()
            try:
                with audit.in_vertretung(vertreten):
                    paper.save()  # Audit: approve-Aktion über Signal; vergibt ggf. die Nummer (Issue #150)
            except NumberingError as exc:
                messages.error(request, f"Freigabe nicht möglich: {exc}")
                return self._redirect(paper)
            messages.success(
                request,
                f"Vorlage wurde freigegeben{vermerk} – {self.session_tenant.reference_label} {paper.display_reference}.",
            )

        elif action == "reject":
            comment = request.POST.get("comment", "").strip()
            paper.approved_by = None
            paper.approved_at = None
            paper.approved_on_behalf_of = None
            with audit.in_vertretung(vertreten):
                paper.save()
            # Audit: Zurückweisung mit Kommentar nachvollziehbar machen
            audit.log_event(
                "update",
                paper,
                user=self.session_user,
                request=request,
                on_behalf_of=vertreten,
                changes={
                    "status": {"alt": old_status, "neu": new_status},
                    "zurueckweisungs_kommentar": comment[:300],
                },
            )
            self._notify_creator(paper, comment)
            messages.success(
                request, f"Vorlage {paper.display_reference} wurde mit Anmerkungen zurückgewiesen{vermerk}."
            )

        return self._redirect(paper)

    def _redirect(self, paper):
        next_url = safe_next_url(self.request, self.session_tenant.slug)
        if next_url:
            return redirect(next_url)
        return redirect(
            "session:paper_detail",
            tenant_slug=self.session_tenant.slug,
            paper_id=paper.id,
        )

    def _approver_emails(self, paper):
        """E-Mails aller Freigabeberechtigten des Mandanten und ihrer Vertretungen (Issue #222)."""
        approvers = list(
            SessionUser.objects.filter(tenant=self.session_tenant, is_active=True)
            .filter(Q(roles__is_admin=True) | Q(roles__can_approve_papers=True))
            .select_related("user")
            .distinct()
        )
        emails = {su.user.email for su in approvers if su.user.email} | _deputy_emails(approvers, paper)
        return sorted(emails - {self.session_user.user.email})

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
            f"die Vorlage {paper.display_reference} „{paper.name}“ wurde zur Freigabe vorgelegt.\n\n"
            f"Zur Vorlage: {self._absolute_url(detail_path)}\n\n"
            f"Mit freundlichen Grüßen\n{self.session_tenant.name}"
        )
        try:
            send_email(
                subject=f"Vorlage zur Freigabe: {paper.display_reference}",
                body=body,
                to=recipients,
                fail_silently=False,
            )
        except Exception:
            logger.exception("Freigabe-Benachrichtigung für %s konnte nicht versendet werden.", paper.pk)

    def _notify_creator(self, paper, comment):
        from apps.common.email import send_email

        creator = paper.created_by
        # Vertretung der erstellenden Person erhält die Zurückweisung in Kopie (Issue #222)
        recipients = sorted(
            ({creator.user.email} if creator and creator.user.email else set())
            | (_deputy_emails([creator], paper) if creator else set())
        )
        if not recipients:
            return
        detail_path = reverse(
            "session:paper_detail",
            kwargs={"tenant_slug": self.session_tenant.slug, "paper_id": paper.id},
        )
        body = (
            f"Guten Tag,\n\n"
            f"die Vorlage {paper.display_reference} „{paper.name}“ wurde in der Prüfung zurückgewiesen.\n\n"
            + (f"Anmerkung: {comment}\n\n" if comment else "")
            + f"Zur Vorlage: {self._absolute_url(detail_path)}\n\n"
            f"Mit freundlichen Grüßen\n{self.session_tenant.name}"
        )
        try:
            send_email(
                subject=f"Vorlage zurückgewiesen: {paper.display_reference}",
                body=body,
                to=recipients,
                fail_silently=False,
            )
        except Exception:
            logger.exception("Zurückweisungs-Benachrichtigung für %s konnte nicht versendet werden.", paper.pk)

    @staticmethod
    def _absolute_url(path):
        from django.conf import settings as django_settings

        base_url = getattr(django_settings, "SITE_URL", "https://mandari.de").rstrip("/")
        return f"{base_url}{path}"


class PaperUpdateView(PaperNumberingFormMixin, SessionViewMixin, UpdateView):
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
        "lead_department",
        "has_financial_impact",
        "financial_impact_note",
        "originator_organization",
        "originator_person",
    ]
    pk_url_kwarg = "paper_id"
    permission_required = "edit_papers"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["text_blocks"] = _active_text_blocks(self.session_tenant)
        return self._numbering_context(context)

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
        form.fields["lead_department"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True, organization_type="department"
        )
        return self._prepare_numbering(form)

    def form_valid(self, form):
        # Ö→NÖ: Steht die Vorlage noch auf einem öffentlichen TOP, erschiene ihr Betreff dort weiter
        if "is_public" in form.changed_data and not form.instance.is_public:
            offen = list(
                form.instance.agenda_items.filter(is_public=True)
                .select_related("meeting")
                .order_by("meeting__start")[:3]
            )
            if offen:
                tops = ", ".join(f"TOP {i.number} ({i.meeting.name})" for i in offen)
                form.add_error(
                    "is_public",
                    f"Die Vorlage steht noch auf öffentlichen Tagesordnungspunkten: {tops}. "
                    "Bitte zuerst diese TOPs auf nicht-öffentlich stellen.",
                )
                return self.form_invalid(form)
        return self._save_with_numbering(form, "Vorlage wurde aktualisiert.")

    def get_success_url(self):
        return reverse(
            "session:paper_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "paper_id": self.object.id,
            },
        )
