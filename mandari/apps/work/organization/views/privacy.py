# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Profil: Daten & Datenschutz (DSGVO-Export, Sitzungen, Kontolöschung).
"""

from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..services import ServiceError
from ._helpers import flash_error


class ProfileDataPrivacyView(WorkViewMixin, TemplateView):
    """DSGVO data export, activity log, and account deletion."""

    template_name = "work/profile/data_privacy.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "data"
        context["recent_sessions"] = selectors.recent_sessions(self.request.user)
        context["is_owner"] = self.organization.owner == self.request.user
        context["exports"] = selectors.recent_exports(self.organization, self.membership)
        context["has_active_export"] = selectors.has_active_export(self.organization, self.membership)
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        try:
            if action == "export_data":
                services.start_data_export(self.organization, self.membership, request.POST.get("format", "json"))
                messages.success(request, "Ihr Datenexport wird erstellt. Sie können die Datei in Kürze herunterladen.")
            elif action == "request_deletion":
                services.request_account_deletion(self.organization, self.membership, request.POST.get("password", ""))
                messages.success(
                    request,
                    "Ihre Mitgliedschaft wurde deaktiviert. Kontaktieren Sie den Support für eine vollständige "
                    "Kontolöschung.",
                )
                return redirect("work:dashboard", org_slug=self.organization.slug)
        except ServiceError as exc:
            flash_error(request, exc)
        return redirect("work:profile_data", org_slug=self.organization.slug)


class DataExportStatusView(WorkViewMixin, View):
    """JSON API for polling export status."""

    permission_required = "dashboard.view"

    def get(self, request, *args, **kwargs):
        export = selectors.get_export_or_404(self.organization, self.membership, kwargs["export_id"])
        download_url = (
            reverse("work:export_download", kwargs={"org_slug": self.organization.slug, "export_id": export.id})
            if export.is_ready
            else None
        )
        return JsonResponse(
            {
                "id": str(export.id),
                "status": export.status,
                "format": export.export_format,
                "file_size": export.file_size,
                "file_size_human": export.file_size_human,
                "is_ready": export.is_ready,
                "is_in_progress": export.is_in_progress,
                "download_url": download_url,
                "error_message": export.error_message,
                "created_at": export.created_at.isoformat() if export.created_at else None,
                "completed_at": export.completed_at.isoformat() if export.completed_at else None,
            }
        )


class DataExportDownloadView(WorkViewMixin, View):
    """Serve export file for download."""

    permission_required = "dashboard.view"

    def get(self, request, *args, **kwargs):
        export = selectors.get_export_or_404(
            self.organization, self.membership, kwargs["export_id"], status="completed"
        )
        file_path = export.get_absolute_path()
        if not file_path or not file_path.exists():
            raise Http404("Exportdatei nicht gefunden.")

        content_type = "application/pdf" if export.export_format == "pdf" else "application/json; charset=utf-8"
        filename = f"mandari-datenexport-{export.created_at.strftime('%Y%m%d')}.{export.export_format}"
        response = HttpResponse(file_path.read_bytes(), content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class DataExportDeleteView(WorkViewMixin, View):
    """Delete an export and its file."""

    permission_required = "dashboard.view"

    def post(self, request, *args, **kwargs):
        export = selectors.get_export_or_404(self.organization, self.membership, kwargs["export_id"])
        services.delete_export(export)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": True})
        messages.success(request, "Export wurde gelöscht.")
        return redirect("work:profile_data", org_slug=self.organization.slug)
