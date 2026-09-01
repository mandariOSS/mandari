# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mitzeichnung von Vorlagen (Issue #81).

- Mitzeichnen/Zurückweisen einer Station (nur zugeordnete Ämter oder Admin,
  Stationen der Reihe nach)
- Arbeitsvorrat „Meine Mitzeichnungen"
- Verwaltung der Mitzeichnungsregeln und Amts-Zuordnungen (Einstellungen)
"""

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import (
    SessionCosignature,
    SessionCosignatureRule,
    SessionOrganization,
    SessionPaper,
    SessionUser,
)
from ..permissions import SessionViewMixin
from ..services import cosign_service


class CosignatureActionView(SessionViewMixin, View):
    """Eine Mitzeichnungsstation mitzeichnen oder zurückweisen."""

    permission_required = "view_papers"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, cosign_id, action):
        if action not in ("sign", "reject"):
            messages.error(request, "Unbekannte Aktion.")
            return redirect("session:my_cosignatures", tenant_slug=tenant_slug)

        cosignature = get_object_or_404(
            SessionCosignature.objects.select_related("paper", "department"),
            pk=cosign_id,
            paper__tenant=self.session_tenant,
        )
        paper = cosignature.paper

        if not cosign_service.can_decide(self.session_user, cosignature):
            messages.error(
                request,
                f"Sie sind dem Amt „{cosignature.department.name}“ nicht zugeordnet.",
            )
            return self._redirect(request, paper)
        if paper.status != "review":
            messages.error(request, "Die Vorlage ist nicht (mehr) in Prüfung.")
            return self._redirect(request, paper)
        if not cosign_service.is_actionable(cosignature):
            messages.error(
                request,
                "Diese Station ist noch nicht an der Reihe oder bereits entschieden.",
            )
            return self._redirect(request, paper)

        comment = request.POST.get("comment", "").strip()
        cosignature.comment = comment
        cosignature.decided_by = self.session_user
        cosignature.decided_at = timezone.now()

        if action == "sign":
            cosignature.status = "signed"
            cosignature.save()
            messages.success(
                request,
                f"Mitzeichnung {cosignature.department.name} für {paper.reference} erteilt.",
            )
        else:
            if not comment:
                messages.error(request, "Bitte einen Kommentar zur Zurückweisung angeben.")
                return self._redirect(request, paper)
            cosignature.status = "rejected"
            cosignature.save()
            # Zurückweisung wirft die Vorlage zurück an die Sachbearbeitung
            paper.status = "draft"
            paper.approved_by = None
            paper.approved_at = None
            paper.save()
            messages.success(
                request,
                f"Mitzeichnung {cosignature.department.name} zurückgewiesen — {paper.reference} ist wieder im Entwurf.",
            )

        audit.log_event(
            "update",
            paper,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "mitzeichnung": cosignature.department.name,
                "entscheidung": cosignature.get_status_display(),
                "kommentar": comment[:300],
            },
        )
        return self._redirect(request, paper)

    def _redirect(self, request, paper):
        next_url = request.POST.get("next", "")
        if next_url.startswith(f"/session/{self.session_tenant.slug}/"):
            return redirect(next_url)
        return redirect("session:paper_detail", tenant_slug=self.session_tenant.slug, paper_id=paper.id)


class MyCosignaturesView(SessionViewMixin, TemplateView):
    """Arbeitsvorrat: offene Mitzeichnungen der eigenen Ämter."""

    template_name = "session/papers/cosign_list.html"
    permission_required = "view_papers"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cosignatures = list(cosign_service.my_pending_cosignatures(self.session_user))
        for cosignature in cosignatures:
            cosignature.actionable = cosign_service.is_actionable(cosignature)
        context.update(
            {
                "cosignatures": cosignatures,
                "my_departments": list(self.session_user.departments.all()),
            }
        )
        return context


class CosignSettingsView(SessionViewMixin, TemplateView):
    """Einstellungen: Mitzeichnungsregeln und Amts-Zuordnungen."""

    template_name = "session/settings/cosign.html"
    permission_required = "manage_settings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        departments = SessionOrganization.objects.filter(
            tenant=self.session_tenant, organization_type="department", is_active=True
        ).order_by("name")
        context.update(
            {
                "rules": SessionCosignatureRule.objects.filter(tenant=self.session_tenant).select_related("department"),
                "departments": departments,
                "paper_type_choices": SessionPaper._meta.get_field("paper_type").choices,
                "session_users": SessionUser.objects.filter(tenant=self.session_tenant, is_active=True)
                .select_related("user")
                .prefetch_related("departments")
                .order_by("user__email"),
            }
        )
        return context


class CosignRuleManageView(SessionViewMixin, View):
    """Mitzeichnungsregel anlegen oder löschen."""

    permission_required = "manage_settings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        if request.POST.get("action") == "delete":
            rule = self._get_rule(request.POST.get("rule_id"))
            if rule is None:
                messages.error(request, "Regel nicht gefunden.")
            else:
                audit.log_event(
                    "delete",
                    rule,
                    tenant=self.session_tenant,
                    user=self.session_user,
                    request=request,
                )
                rule.delete()
                messages.success(request, "Mitzeichnungsregel gelöscht.")
            return self._redirect()

        department = None
        dep_id = request.POST.get("department", "").strip()
        if dep_id:
            try:
                department = SessionOrganization.objects.filter(
                    tenant=self.session_tenant, organization_type="department", pk=dep_id
                ).first()
            except (ValueError, DjangoValidationError):
                department = None
        if department is None:
            messages.error(request, "Bitte ein Amt/Fachbereich auswählen.")
            return self._redirect()

        paper_type = request.POST.get("paper_type", "")
        valid_types = {value for value, _ in SessionPaper._meta.get_field("paper_type").choices}
        if paper_type not in valid_types:
            paper_type = ""

        try:
            order = max(0, min(999, int(request.POST.get("order", "0"))))
        except (TypeError, ValueError):
            order = 0

        rule = SessionCosignatureRule.objects.create(
            tenant=self.session_tenant,
            paper_type=paper_type,
            department=department,
            order=order,
            only_financial=request.POST.get("only_financial") == "1",
        )
        audit.log_event(
            "create",
            rule,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"amt": department.name, "vorlagenart": rule.get_paper_type_label()},
        )
        messages.success(request, f"Mitzeichnungsregel für {department.name} angelegt.")
        return self._redirect()

    def _get_rule(self, pk):
        if not pk:
            return None
        try:
            return SessionCosignatureRule.objects.filter(tenant=self.session_tenant, pk=pk).first()
        except (ValueError, DjangoValidationError):
            return None

    def _redirect(self):
        return redirect("session:settings_cosign", tenant_slug=self.session_tenant.slug)


class DepartmentAssignmentView(SessionViewMixin, View):
    """Benutzer einem Amt zuordnen bzw. Zuordnung entfernen."""

    permission_required = "manage_users"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        try:
            session_user = SessionUser.objects.filter(
                tenant=self.session_tenant, pk=request.POST.get("session_user")
            ).first()
            department = SessionOrganization.objects.filter(
                tenant=self.session_tenant,
                organization_type="department",
                pk=request.POST.get("department"),
            ).first()
        except (ValueError, DjangoValidationError):
            session_user = department = None

        if session_user is None or department is None:
            messages.error(request, "Bitte Benutzer und Amt auswählen.")
            return redirect("session:settings_cosign", tenant_slug=tenant_slug)

        if request.POST.get("action") == "remove":
            session_user.departments.remove(department)
            action_label = "entfernt"
        else:
            session_user.departments.add(department)
            action_label = "zugeordnet"

        audit.log_event(
            "update",
            session_user,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"amt": department.name, "zuordnung": action_label},
        )
        messages.success(
            request,
            f"{session_user.user.email} wurde dem Amt „{department.name}“ {action_label}.",
        )
        return redirect("session:settings_cosign", tenant_slug=tenant_slug)
