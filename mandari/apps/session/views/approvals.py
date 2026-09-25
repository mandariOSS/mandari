# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen → Vier-Augen-Prinzip und Vertretungen (Issue #222).

- Vier-Augen-Prinzip je Vorgangsart schalten (``manage_settings``)
- Vertretungen eintragen und aufheben, Übersicht aktiver, geplanter und beendeter
  Vertretungen (``manage_users``)

Die Regeln selbst liegen in ``services/four_eyes_service.py`` und
``services/delegation_service.py``; jede Änderung steht im Audit-Log.
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionDelegation, SessionTenant, SessionUser
from ..permissions import SessionViewMixin
from ..services import delegation_service, four_eyes_service

_log_event = cast(Any, audit).log_event


def _date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _describe(delegation: SessionDelegation) -> str:
    return (
        f"{delegation.deputy.user.email} vertritt {delegation.principal.user.email} vom "
        f"{delegation.start_date:%d.%m.%Y} bis {delegation.end_date:%d.%m.%Y}"
    )


class FourEyesSettingsView(SessionViewMixin, TemplateView):
    """Vier-Augen-Prinzip je Vorgangsart anzeigen und speichern."""

    template_name = "session/settings/four_eyes.html"
    permission_required = "manage_settings"

    @property
    def tenant(self) -> SessionTenant:
        return cast(SessionTenant, self.session_tenant)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        context["processes"] = [
            {
                "key": key,
                "label": label,
                "field": four_eyes_service.TENANT_FIELDS[key],
                "value": getattr(self.tenant, four_eyes_service.TENANT_FIELDS[key]),
                "self_definition": four_eyes_service.SELF_DEFINITION[key],
            }
            for key, label in four_eyes_service.PROCESSES.items()
        ]
        context["paper_choices"] = SessionTenant.FOUR_EYES_PAPER_CHOICES
        context["can_manage_users"] = "manage_users" in context["permission_checker"].permissions
        return context

    def post(self, request: HttpRequest, tenant_slug: str) -> HttpResponse:
        tenant = self.tenant
        fields = list(four_eyes_service.TENANT_FIELDS.values())
        old = {name: getattr(tenant, name) for name in fields}

        mode = request.POST.get("four_eyes_papers", "")
        if mode in dict(SessionTenant.FOUR_EYES_PAPER_CHOICES):
            tenant.four_eyes_papers = mode
        tenant.four_eyes_protocols = request.POST.get("four_eyes_protocols") == "on"
        tenant.four_eyes_allowances = request.POST.get("four_eyes_allowances") == "on"
        tenant.four_eyes_forwardings = request.POST.get("four_eyes_forwardings") == "on"

        changes = {
            name: {"alt": old[name], "neu": getattr(tenant, name)}
            for name in fields
            if old[name] != getattr(tenant, name)
        }
        if changes:
            cast(Any, tenant).save(update_fields=[*fields, "updated_at"])
            _log_event("update", tenant, tenant=tenant, user=self.session_user, request=request, changes=changes)
        messages.success(request, "Einstellungen zum Vier-Augen-Prinzip gespeichert.")
        if old["four_eyes_allowances"] and not tenant.four_eyes_allowances:
            messages.warning(
                request,
                "Sitzungsgeld und Pauschalen darf jetzt auch genehmigen, wer den Lauf erzeugt hat. "
                "Für Auszahlungen empfehlen wir das Vier-Augen-Prinzip.",
            )
        return redirect("session:settings_four_eyes", tenant_slug=tenant_slug)


class DelegationListView(SessionViewMixin, TemplateView):
    """Übersicht aktiver, geplanter und zuletzt beendeter Vertretungen mit Formular."""

    template_name = "session/settings/delegations.html"
    permission_required = "manage_users"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        context["overview"] = delegation_service.overview(cast(SessionTenant, self.session_tenant))
        context["tenant_users"] = (
            SessionUser.objects.filter(tenant=self.session_tenant, is_active=True)
            .select_related("user")
            .order_by("user__email")
        )
        context["scopes"] = list(delegation_service.SCOPES.items())
        return context


class DelegationCreateView(SessionViewMixin, View):
    """Vertretung eintragen."""

    http_method_names = ["post"]
    permission_required = "manage_users"

    def _tenant_user(self, raw: str | None) -> SessionUser | None:
        if not raw:
            return None
        try:
            return SessionUser.objects.select_related("user").get(pk=raw, tenant=self.session_tenant)
        except (SessionUser.DoesNotExist, ValidationError, ValueError, TypeError):
            return None

    def post(self, request: HttpRequest, tenant_slug: str) -> HttpResponse:
        target = redirect("session:settings_delegations", tenant_slug=tenant_slug)
        principal = self._tenant_user(request.POST.get("principal"))
        deputy = self._tenant_user(request.POST.get("deputy"))
        start, end = _date(request.POST.get("start_date")), _date(request.POST.get("end_date"))
        if principal is None or deputy is None:
            messages.error(request, "Bitte vertretene Person und Vertretung aus diesem Mandanten wählen.")
            return target
        if start is None or end is None:
            messages.error(request, "Bitte Beginn und Ende der Vertretung angeben.")
            return target
        try:
            delegation = delegation_service.create(
                cast(SessionTenant, self.session_tenant),
                principal=principal,
                deputy=deputy,
                start_date=start,
                end_date=end,
                scopes=request.POST.getlist("scopes"),
                created_by=self.session_user,
            )
        except delegation_service.DelegationError as exc:
            messages.error(request, str(exc))
            return target

        _log_event(
            "create",
            delegation,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "vertretung": {"alt": None, "neu": _describe(delegation)},
                "umfang": {"alt": None, "neu": ", ".join(delegation.scope_labels)},
            },
        )
        messages.success(request, f"Vertretung eingetragen: {_describe(delegation)}.")
        # Keine Kettenvertretung: Ist die Vertretung selbst abwesend, bleibt die Person dann unvertreten
        absences = delegation_service.overlapping_absences(deputy, start, end)
        if absences:
            first = absences[0]
            messages.warning(
                request,
                f"Hinweis: {deputy.user.email} ist vom {first.start_date:%d.%m.%Y} bis {first.end_date:%d.%m.%Y} "
                "selbst vertreten. Vertretungen werden nicht weitergereicht – in dieser Zeit bleibt "
                f"{principal.user.email} unvertreten.",
            )
        return target


class DelegationRevokeView(SessionViewMixin, View):
    """Vertretung aufheben (bleibt als Nachweis in der Übersicht)."""

    http_method_names = ["post"]
    permission_required = "manage_users"

    def post(self, request: HttpRequest, tenant_slug: str, delegation_id: Any) -> HttpResponse:
        delegation = get_object_or_404(
            SessionDelegation.objects.select_related("principal__user", "deputy__user"),
            pk=delegation_id,
            tenant=self.session_tenant,
        )
        if delegation_service.revoke(delegation, by=self.session_user):
            _log_event(
                "update",
                delegation,
                tenant=self.session_tenant,
                user=self.session_user,
                request=request,
                changes={"vertretung_aufgehoben": {"alt": _describe(delegation), "neu": None}},
            )
            messages.success(request, f"Vertretung aufgehoben: {_describe(delegation)}.")
        else:
            messages.info(request, "Diese Vertretung war bereits aufgehoben.")
        return redirect("session:settings_delegations", tenant_slug=tenant_slug)
