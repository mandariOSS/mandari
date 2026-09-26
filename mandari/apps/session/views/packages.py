# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsmappe (Issue #218): Status, Anforderung und Download.

- Status: HTMX-Fragment in der Seitenleiste der Sitzung. Je abrufbarer Fassung der Stand,
  fertige Fassungen zum Herunterladen, „wird erstellt“ mit Selbstaktualisierung und ein
  Hinweis, wenn sich die Unterlagen seit der letzten Fassung geändert haben.
- Anforderung (POST): legt nur die Anforderung an; erzeugt wird im Hintergrund
  (``build_meeting_packages``) – der Seitenaufruf blockiert nie.
- Download: nur für Berechtigte der jeweiligen Fassung (dieselben Rechte, die jeder
  enthaltene Bestandteil einzeln verlangt), gesperrte Fassungen nie; jeder Download steht
  im Audit-Log.
"""

from __future__ import annotations

from typing import Any, cast

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from ..models import SessionMeeting, SessionMeetingPackage, SessionTenant, SessionUser
from ..permissions import SessionPermissionChecker, SessionViewMixin
from ..services import meeting_package_service
from ..services.meeting_package_plan import VARIANT_LABELS, variants_for

POLL_SECONDS = 5


class _MeetingPackageView(SessionViewMixin, View):
    """Gemeinsame Grundlage: Sitzung laden (Ö/NÖ wie überall) und abrufbare Fassungen bestimmen."""

    permission_required = "view_meetings"

    @property
    def tenant(self) -> SessionTenant:
        return cast(SessionTenant, self.session_tenant)

    @property
    def user(self) -> SessionUser:
        return cast(SessionUser, self.session_user)

    def permissions(self) -> set[str]:
        return cast(set[str], cast(Any, SessionPermissionChecker)(self.user).permissions)

    def meeting_and_variants(self) -> tuple[SessionMeeting, list[str]]:
        # Ö/NÖ wie in allen Sitzungsansichten: nichtöffentliche Sitzungen nur für Berechtigte
        permissions = self.permissions()
        qs = SessionMeeting.objects.filter(tenant=self.tenant).select_related(
            "tenant", "organization", "legislative_term"
        )
        if "view_non_public_meetings" not in permissions:
            qs = qs.filter(is_public=True)
        meeting = get_object_or_404(qs, pk=self.kwargs["meeting_id"])
        variants = variants_for(permissions, meeting)
        if not variants:
            raise PermissionDenied("Fehlende Berechtigung für die Sitzungsmappe")
        return meeting, variants

    def render_status(self, meeting: SessionMeeting, variants: list[str]) -> HttpResponse:
        entries = meeting_package_service.overview(meeting, variants)
        context: dict[str, Any] = {
            "meeting": meeting,
            "tenant_slug": self.tenant.slug,
            "entries": entries,
            "polling": any(entry.in_progress for entry in entries),
            "poll_seconds": POLL_SECONDS,
        }
        return render(self.request, "session/partials/meeting_package.html", context)


class MeetingPackageStatusView(_MeetingPackageView):
    """Stand der Sitzungsmappe (HTMX-Fragment, wird während der Erstellung abgefragt)."""

    http_method_names = ["get"]

    def get(self, request: HttpRequest, tenant_slug: str, meeting_id: Any) -> HttpResponse:
        meeting, variants = self.meeting_and_variants()
        return self.render_status(meeting, variants)


class MeetingPackageRequestView(_MeetingPackageView):
    """Fassung anfordern – erzeugt wird im Hintergrund."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, tenant_slug: str, meeting_id: Any) -> HttpResponse:
        meeting, variants = self.meeting_and_variants()
        variant = request.POST.get("variant", "")
        if variant not in variants:
            raise PermissionDenied("Diese Fassung ist für Sie nicht abrufbar")
        package, created = meeting_package_service.request_package(meeting, variant, self.user)
        if self.is_htmx:
            return self.render_status(meeting, variants)
        if created:
            messages.success(
                request,
                f"Die Sitzungsmappe ({VARIANT_LABELS[variant]}, Fassung {package.version}) wird im Hintergrund erstellt.",
            )
        else:
            messages.info(request, "Die Sitzungsmappe ist bereits auf dem aktuellen Stand.")
        return redirect("session:meeting_detail", tenant_slug=self.tenant.slug, meeting_id=meeting.id)


class MeetingPackageDownloadView(_MeetingPackageView):
    """Gesamt-PDF oder ZIP-Paket einer fertigen Fassung herunterladen (protokolliert)."""

    http_method_names = ["get"]

    CONTENT_TYPES = {"pdf": "application/pdf", "zip": "application/zip"}

    def get(
        self, request: HttpRequest, tenant_slug: str, meeting_id: Any, package_id: Any, fmt: str
    ) -> HttpResponseBase:
        if fmt not in self.CONTENT_TYPES:
            raise Http404("Unbekanntes Format")
        meeting, variants = self.meeting_and_variants()
        package = get_object_or_404(
            SessionMeetingPackage.objects.select_related("meeting__organization", "tenant"),
            pk=package_id,
            meeting=meeting,
            tenant=self.tenant,
            status=SessionMeetingPackage.STATUS_READY,
        )
        if package.variant not in variants:
            raise PermissionDenied("Diese Fassung ist für Sie nicht abrufbar")
        if package.pk in meeting_package_service.blocked_packages([package]):
            messages.error(
                request,
                "Diese Fassung ist nicht mehr abrufbar: Enthaltene Unterlagen wurden inzwischen gelöscht "
                "oder sind nichtöffentlich. Bitte eine neue Fassung erstellen.",
            )
            return redirect("session:meeting_detail", tenant_slug=self.tenant.slug, meeting_id=meeting.id)

        field = package.pdf_file if fmt == "pdf" else package.zip_file
        if not field:
            raise Http404("Datei nicht gefunden")
        try:
            handle = field.storage.open(str(field.name), "rb")
        except (OSError, ValueError):
            raise Http404("Datei nicht gefunden") from None

        meeting_package_service.log_download(package, fmt, user=self.user, request=request)
        response = FileResponse(
            handle,
            as_attachment=True,
            filename=meeting_package_service.download_filename(package, fmt),
            content_type=self.CONTENT_TYPES[fmt],
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response
