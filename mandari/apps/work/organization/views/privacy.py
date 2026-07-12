# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import logging

from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


# =============================================================================
# COUNCIL PARTY MANAGEMENT
# =============================================================================


# =============================================================================
# PROFILE: DATA & PRIVACY (DSGVO)
# =============================================================================


class ProfileDataPrivacyView(WorkViewMixin, TemplateView):
    """DSGVO data export, activity log, and account deletion."""

    template_name = "work/profile/data_privacy.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "data"

        user = self.request.user

        # Security events (last logins, password changes)
        from apps.accounts.models import UserSession

        context["recent_sessions"] = UserSession.objects.filter(user=user).order_by("-created_at")[:10]

        context["is_owner"] = self.organization.owner == user

        # Export history
        from ..models import DataExport

        context["exports"] = DataExport.objects.filter(
            membership=self.membership,
            organization=self.organization,
        ).order_by("-created_at")[:10]

        context["has_active_export"] = DataExport.objects.filter(
            membership=self.membership,
            organization=self.organization,
            status__in=["pending", "processing"],
        ).exists()

        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")

        if action == "export_data":
            return self._export_data(request)
        elif action == "request_deletion":
            return self._request_deletion(request)

        return redirect("work:profile_data", org_slug=self.organization.slug)

    def _export_data(self, request):
        """Start async DSGVO data export."""
        from apps.work.background_tasks import generate_dsgvo_export_task

        from ..models import DataExport

        # Prevent duplicate exports
        if DataExport.objects.filter(
            membership=self.membership,
            organization=self.organization,
            status__in=["pending", "processing"],
        ).exists():
            messages.info(request, "Es läuft bereits ein Export. Bitte warten Sie, bis dieser abgeschlossen ist.")
            return redirect("work:profile_data", org_slug=self.organization.slug)

        export_format = request.POST.get("format", "json")
        if export_format not in ("json", "pdf"):
            export_format = "json"

        export = DataExport.objects.create(
            organization=self.organization,
            membership=self.membership,
            export_format=export_format,
        )

        generate_dsgvo_export_task.enqueue(str(export.id))

        messages.success(request, "Ihr Datenexport wird erstellt. Sie können die Datei in Kürze herunterladen.")
        return redirect("work:profile_data", org_slug=self.organization.slug)

    def _request_deletion(self, request):
        """Handle account deletion request."""
        user = request.user

        if self.organization.owner == user:
            messages.error(
                request,
                "Als Eigentümer müssen Sie zuerst die Eigentümerschaft übertragen, bevor Sie Ihr Konto löschen können.",
            )
            return redirect("work:profile_data", org_slug=self.organization.slug)

        password = request.POST.get("password", "")
        if not user.check_password(password):
            messages.error(request, "Falsches Passwort.")
            return redirect("work:profile_data", org_slug=self.organization.slug)

        # Deactivate membership (soft delete)
        self.membership.is_active = False
        self.membership.save()

        messages.success(
            request,
            "Ihre Mitgliedschaft wurde deaktiviert. Kontaktieren Sie den Support für eine vollständige Kontolöschung.",
        )
        return redirect("work:dashboard", org_slug=self.organization.slug)


class DataExportStatusView(WorkViewMixin, View):
    """JSON API for polling export status."""

    permission_required = "dashboard.view"

    def get(self, request, *args, **kwargs):
        from ..models import DataExport

        export = get_object_or_404(
            DataExport,
            id=kwargs["export_id"],
            membership=self.membership,
            organization=self.organization,
        )

        download_url = (
            reverse(
                "work:export_download",
                kwargs={"org_slug": self.organization.slug, "export_id": export.id},
            )
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
        from ..models import DataExport

        export = get_object_or_404(
            DataExport,
            id=kwargs["export_id"],
            membership=self.membership,
            organization=self.organization,
            status="completed",
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
        from ..models import DataExport

        export = get_object_or_404(
            DataExport,
            id=kwargs["export_id"],
            membership=self.membership,
            organization=self.organization,
        )

        export.delete_file()
        export.delete()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": True})

        messages.success(request, "Export wurde gelöscht.")
        return redirect("work:profile_data", org_slug=self.organization.slug)
