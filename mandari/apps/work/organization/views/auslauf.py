# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auslauf-Seite einer Mitgliedschaft „Nur Lesen, Löschung nach 30 Tagen“ (Issue #1237).

Die einzige Seite, die eine Mitgliedschaft im Auslauf in ihrer Organisation erreicht (``expiry_allowed``): Hinweis,
Löschdatum und der DSGVO-Export der eigenen Daten (Starten und Herunterladen). Keine Inhalte der Organisation,
keine Änderungen. Aktive Mitglieder kommen zum Start, alle anderen bleiben ausgesperrt wie bisher.
"""

from __future__ import annotations

from typing import Any, cast

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.tenants.models import Membership, Organization

from .. import auslauf, selectors, services
from ..services import ServiceError
from ._helpers import flash_error
from .privacy import export_file_response


class _AuslaufMixin(WorkViewMixin):
    """Erreichbar auch im Auslauf; ohne Rechteprüfung, nur Daten der eigenen Mitgliedschaft."""

    expiry_allowed = True
    permission_required = None

    @property
    def org(self) -> Organization:
        return cast(Organization, self.organization)

    @property
    def member(self) -> Membership:
        return cast(Membership, self.membership)


class MembershipExpiryView(_AuslaufMixin, TemplateView):
    """Hinweis mit Löschdatum und Export der eigenen Daten."""

    template_name = "work/organization/auslauf.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if self.member.is_active:
            return redirect("work:dashboard", org_slug=self.org.slug)
        return super().get(request, *args, **kwargs)

    def get_template_names(self) -> list[str]:
        if self.request.GET.get("teil") == "exporte":
            return [f"{self.template_name}#exporte"]
        return [self.template_name]

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        context["deletion_date"] = auslauf.deletion_date_text(self.member)
        context["exports"] = selectors.recent_exports(self.org, self.member)
        context["has_active_export"] = selectors.has_active_export(self.org, self.member)
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if self.member.is_active:
            return redirect("work:dashboard", org_slug=self.org.slug)
        if request.POST.get("action") == "export_data":
            try:
                services.start_data_export(self.org, self.member, request.POST.get("format", "json"))
                messages.success(request, "Dein Datenexport wird erstellt. Du kannst ihn gleich hier herunterladen.")
            except ServiceError as exc:
                flash_error(request, exc)
        return redirect("work:membership_expiry", org_slug=self.org.slug)


class MembershipExpiryExportDownloadView(_AuslaufMixin, View):
    """Fertigen Export der eigenen Mitgliedschaft herunterladen (auch im Auslauf)."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return export_file_response(self.org, self.member, kwargs["export_id"])
