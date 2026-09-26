# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fassungen einer Vorlage (Issue #226): Übersicht, Ansicht, Vergleich, Sichern, Wiederherstellen.

Sicherheit:
- Jede Ansicht lädt die Vorlage mandantengefiltert und nach der Ö/NÖ-Regel der Detailseite;
  Fassungen und ihre Anlagen zeigt sie nur, soweit ``paper_version_service.version_visible`` /
  ``entry_visible`` es erlauben (nie weiter als heute und nie weiter als damals).
- Sichern und Wiederherstellen brauchen das Bearbeitungsrecht; Wiederherstellen zusätzlich den
  Workflow-Zustand „Entwurf“ (``paper_version_service.RESTORE_STATUSES``).
- Downloads laufen über die zugriffsgeprüfte View, immer als Download mit Typ aus der Endung,
  ``Cache-Control: private, no-store``, ``X-Content-Type-Options: nosniff`` und Sandbox-CSP, und
  stehen im Audit-Log.
"""

from __future__ import annotations

from typing import Any, cast

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic.base import ContextMixin

from .. import audit
from ..models import SessionPaper, SessionPaperVersion, SessionPaperVersionFile, SessionTenant, SessionUser
from ..permissions import SessionPermissionChecker, SessionViewMixin
from ..services import file_service, file_version_service, paper_version_diff, paper_version_service

_log_event = cast(Any, audit).log_event


class _PaperVersionView(SessionViewMixin, ContextMixin, View):
    """Gemeinsame Grundlage: Vorlage laden (Mandant, Ö/NÖ) und Fassungen prüfen."""

    permission_required = "view_papers"

    @property
    def tenant(self) -> SessionTenant:
        return cast(SessionTenant, self.session_tenant)

    @property
    def user(self) -> SessionUser:
        return cast(SessionUser, self.session_user)

    def permissions(self) -> set[str]:
        return cast(set[str], cast(Any, SessionPermissionChecker)(self.user).permissions)

    def get_paper(self) -> SessionPaper:
        qs = SessionPaper.objects.filter(tenant=self.tenant)
        if "view_non_public_papers" not in self.permissions():
            qs = qs.filter(is_public=True)
        return get_object_or_404(qs, pk=self.kwargs["paper_id"])

    def get_version(self, paper: SessionPaper, number: Any) -> SessionPaperVersion:
        version = (
            SessionPaperVersion.objects.filter(paper=paper, tenant=self.tenant, number=number)
            .select_related("created_by__user", "restored_from", "agenda_item__meeting")
            .first()
        )
        if version is None or not paper_version_service.version_visible(self.permissions(), paper, version):
            raise Http404("Fassung nicht gefunden")
        return version

    def base_context(self, paper: SessionPaper, **extra: Any) -> dict[str, Any]:
        """Seitenkontext inkl. Navigation (SessionMixin) plus die Rechte für die Aktionen."""
        permissions = self.permissions()
        return cast(
            dict[str, Any],
            cast(Any, self).get_context_data(
                paper=paper,
                can_edit="edit_papers" in permissions,
                restore_blocker=paper_version_service.restore_blocker(paper),
                **extra,
            ),
        )


class PaperVersionListView(_PaperVersionView):
    """Alle Fassungen einer Vorlage mit Auswahl für den Vergleich."""

    http_method_names = ["get"]

    def get(self, request: HttpRequest, tenant_slug: str, paper_id: Any) -> HttpResponse:
        paper = self.get_paper()
        versions = paper_version_service.visible_versions(self.permissions(), paper)
        rows = []
        for index, version in enumerate(versions):
            older = versions[index + 1] if index + 1 < len(versions) else None
            rows.append(
                {
                    "version": version,
                    "previous": older,
                    "unchanged": older is not None and older.fingerprint == version.fingerprint,
                }
            )
        context = self.base_context(
            paper,
            rows=rows,
            default_a=versions[1].number if len(versions) > 1 else None,
            default_b=versions[0].number if versions else None,
        )
        return render(request, "session/papers/versions/list.html", context)


class PaperVersionDetailView(_PaperVersionView):
    """Eine Fassung: Texte, Angaben, Anlagen (mit Download) und Aktionen."""

    http_method_names = ["get"]

    def get(self, request: HttpRequest, tenant_slug: str, paper_id: Any, number: int) -> HttpResponse:
        paper = self.get_paper()
        version = self.get_version(paper, number)
        permissions = self.permissions()
        visible = paper_version_service.visible_entries(permissions, paper, version)
        older = SessionPaperVersion.objects.filter(paper=paper, number__lt=version.number)
        if "view_non_public_papers" not in permissions:
            older = older.filter(is_public=True)
        if not paper.is_public or not version.is_public:
            # Lesezugriff auf eine nichtöffentliche Fassung (Issue #221): nur Objekt, nie Inhalt
            cast(Any, audit).log_read(
                request,
                paper,
                tenant=self.tenant,
                user=self.user,
                changes={"umfang": f"nichtöffentliche Vorlage, Fassung {version.number}"},
                dedup_suffix=f"fassung-{version.number}",
            )
        context = self.base_context(
            paper,
            version=version,
            entries=visible.entries,
            purgeable=(
                file_version_service.purgeable_blob_ids(entry.blob for entry in visible.entries)
                if "manage_settings" in permissions
                else set()
            ),
            previous_number=older.order_by("-number").values_list("number", flat=True).first(),
        )
        return render(request, "session/papers/versions/detail.html", context)


class PaperVersionCompareView(_PaperVersionView):
    """Zwei Fassungen vergleichen: Texte wortgenau, Angaben und Anlagen über Metadaten."""

    http_method_names = ["get"]

    def get(self, request: HttpRequest, tenant_slug: str, paper_id: Any) -> HttpResponseBase:
        paper = self.get_paper()
        try:
            first = int(request.GET.get("a", ""))
            second = int(request.GET.get("b", ""))
        except ValueError:
            messages.info(request, "Bitte zwei Fassungen zum Vergleich auswählen.")
            return redirect("session:paper_versions", tenant_slug=self.tenant.slug, paper_id=paper.pk)
        if first == second:
            messages.info(request, "Bitte zwei verschiedene Fassungen auswählen.")
            return redirect("session:paper_versions", tenant_slug=self.tenant.slug, paper_id=paper.pk)
        older = self.get_version(paper, min(first, second))
        newer = self.get_version(paper, max(first, second))
        permissions = self.permissions()
        comparison = paper_version_diff.compare(
            older,
            newer,
            older_entries=paper_version_service.visible_entries(permissions, paper, older).entries,
            newer_entries=paper_version_service.visible_entries(permissions, paper, newer).entries,
        )
        if not paper.is_public or not older.is_public or not newer.is_public:
            # Vergleich nichtöffentlicher Fassungen ist ein Lesezugriff (Issue #221)
            cast(Any, audit).log_read(
                request,
                paper,
                tenant=self.tenant,
                user=self.user,
                changes={
                    "umfang": f"nichtöffentliche Vorlage, Vergleich der Fassungen {older.number} und {newer.number}"
                },
                dedup_suffix=f"vergleich-{older.number}-{newer.number}",
            )
        return render(request, "session/papers/versions/compare.html", self.base_context(paper, comparison=comparison))


class PaperVersionCreateView(_PaperVersionView):
    """„Fassung sichern“: aktuellen Stand von Hand festhalten."""

    http_method_names = ["post"]
    permission_required = "edit_papers"

    def post(self, request: HttpRequest, tenant_slug: str, paper_id: Any) -> HttpResponse:
        paper = self.get_paper()
        version = paper_version_service.save_manually(paper, user=self.user, note=request.POST.get("note", ""))
        messages.success(request, f"Fassung {version.number} wurde gesichert.")
        return redirect("session:paper_versions", tenant_slug=self.tenant.slug, paper_id=paper.pk)


class PaperVersionRestoreView(_PaperVersionView):
    """Fassung als neue Fassung wiederherstellen (nur im Entwurf)."""

    http_method_names = ["post"]
    permission_required = "edit_papers"

    def post(self, request: HttpRequest, tenant_slug: str, paper_id: Any, number: int) -> HttpResponse:
        paper = self.get_paper()
        version = self.get_version(paper, number)
        try:
            result = paper_version_service.restore(paper, version, user=self.user, permissions=self.permissions())
        except paper_version_service.RestoreRefusedError as exc:
            messages.error(request, str(exc))
            return redirect(
                "session:paper_version_detail", tenant_slug=self.tenant.slug, paper_id=paper.pk, number=number
            )
        messages.success(
            request,
            f"Fassung {version.number} wurde als neue Fassung {result.version.number} wiederhergestellt.",
        )
        for note in result.notes:
            messages.info(request, note)
        return redirect("session:paper_detail", tenant_slug=self.tenant.slug, paper_id=paper.pk)


class PaperVersionFileDownloadView(_PaperVersionView):
    """Anlage so herunterladen, wie sie in der Fassung enthalten ist (protokolliert)."""

    http_method_names = ["get"]

    def get(
        self, request: HttpRequest, tenant_slug: str, paper_id: Any, number: int, entry_id: Any
    ) -> HttpResponseBase:
        paper = self.get_paper()
        version = self.get_version(paper, number)
        entry = get_object_or_404(SessionPaperVersionFile.objects.select_related("blob"), pk=entry_id, version=version)
        attachments = paper_version_service.current_attachments(paper, [entry])
        if not paper_version_service.entry_visible(self.permissions(), paper, version, entry, attachments):
            raise PermissionDenied("Keine Berechtigung für diese Anlage")
        handle = file_version_service.open_blob(entry.blob)
        if handle is None:
            raise Http404("Der Inhalt dieser Anlage ist nicht mehr vorhanden.")
        changes: dict[str, Any] = {"anlage": entry.name, "fassung": version.number}
        if not paper.is_public or not version.is_public or not entry.is_public:
            changes["nichtoeffentlich"] = True  # Lesezugriff auf Nichtöffentliches (Issue #221)
        _log_event("download", version, user=self.user, request=request, changes=changes)
        return protected_download(handle, file_service.blob_download_name(entry.name, entry.blob))


def protected_download(handle: Any, filename: str) -> FileResponse:
    """
    Download mit den Schutz-Headern der Anlagen: immer als Download, Typ aus der Endung,
    kein MIME-Sniffing, Sandbox-CSP, kein Caching (``file_service.file_response``).
    """
    response = file_service.file_response(handle, filename)
    response["Cache-Control"] = "private, no-store"
    return response
