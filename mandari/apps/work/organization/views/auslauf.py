# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auslauf-Seite einer Mitgliedschaft „Nur Lesen, Löschung nach 30 Tagen“ (Issue #1237).

Die einzige Seite, die eine Mitgliedschaft im Auslauf in ihrer Organisation erreicht (``expiry_allowed``): Hinweis,
Löschdatum, der DSGVO-Export der eigenen Daten (Starten und Herunterladen) und die Anhänge, die am Löschtag mit den
privaten Dokumenten und persönlichen Aufgaben entfallen (nur genau diese, ``auslauf.deleted_attachments``). Keine
Inhalte der Organisation, keine Änderungen. Aktive Mitglieder kommen zum Start, alle anderen bleiben ausgesperrt wie
bisher.
"""

from __future__ import annotations

from typing import Any, cast

from django.contrib import messages
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
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

    def handle_expiry_restriction(self, request: HttpRequest) -> HttpResponse | None:
        """Aktive Mitglieder brauchen die Auslauf-Seite nicht: zum Start."""
        if self.membership is not None and self.membership.is_active:
            return redirect("work:dashboard", org_slug=self.org.slug)
        return None


class MembershipExpiryView(_AuslaufMixin, TemplateView):
    """Hinweis mit Löschdatum und Export der eigenen Daten."""

    template_name = "work/organization/auslauf.html"

    def get_template_names(self) -> list[str]:
        if self.request.GET.getlist("teil") == ["exporte"]:
            return [f"{self.template_name}#exporte"]
        return [self.template_name]

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        context["deletion_date"] = auslauf.deletion_date_text(self.member)
        context["exports"] = selectors.recent_exports(self.org, self.member)
        context["has_active_export"] = selectors.has_active_export(self.org, self.member)
        context["attachments"] = auslauf.deleted_attachments(self.member)
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Einzige Aktion der Seite: Export der eigenen Daten starten (Format JSON oder PDF)."""
        export_format = "pdf" if request.POST.getlist("format") == ["pdf"] else "json"
        try:
            services.start_data_export(self.org, self.member, export_format)
            messages.success(request, "Ihr Datenexport wird erstellt. Sie können ihn gleich hier herunterladen.")
        except ServiceError as exc:
            flash_error(request, exc)
        return redirect("work:membership_expiry", org_slug=self.org.slug)


class MembershipExpiryExportDownloadView(_AuslaufMixin, View):
    """Fertigen Export der eigenen Mitgliedschaft herunterladen (auch im Auslauf)."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return export_file_response(self.org, self.member, kwargs["export_id"])


class MembershipExpiryAttachmentView(_AuslaufMixin, View):
    """
    Anhang eines privaten Dokuments oder einer persönlichen Aufgabe herunterladen, der am Löschtag entfällt.

    Erreichbar ist nur genau diese Auswahl der eigenen Mitgliedschaft (``auslauf.deleted_attachment``); alles andere
    (geteilte oder fremde Anhänge, unbekannte Art): 404. Immer als Download, nie eingebettet.
    """

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> FileResponse:
        attachment = auslauf.deleted_attachment(self.member, kwargs["kind"], kwargs["attachment_id"])
        if attachment is None or not attachment.file:
            raise Http404("Anhang nicht gefunden.")
        try:
            handle = attachment.file.open("rb")
        except (FileNotFoundError, ValueError, OSError):
            raise Http404("Datei nicht gefunden.") from None
        response = FileResponse(
            handle, as_attachment=True, filename=attachment.filename, content_type="application/octet-stream"
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response
