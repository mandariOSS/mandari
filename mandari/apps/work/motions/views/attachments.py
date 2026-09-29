# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anhänge im Dokument-Editor: hochladen, umbenennen, entfernen (Issue #584).

Rechte nach der Dokumentregel: ``motions.edit`` und ``Motion.can_edit`` (Schreibstufe ohne
Status-Sperre – die Sperre betrifft nur den Inhalt). Gäste bearbeiten keine Anhänge. Antwort
ist das Fragment ``partials/_attachments.html`` (HTMX) bzw. eine Weiterleitung in den Editor.
Der Download läuft über ``MotionDocumentDownloadView`` (``can_access``).
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.common.mixins import WorkViewMixin

from .. import attachments
from ..models import Motion, MotionDocument


class _AttachmentViewMixin(WorkViewMixin):
    permission_required = "motions.edit"

    def _motion(self, motion_id: Any) -> Motion:
        motion = get_object_or_404(Motion, id=motion_id, organization=self.organization)
        if not motion.can_edit(self.membership):
            raise PermissionDenied("Kein Zugriff auf dieses Dokument.")
        return motion

    def _respond(self, request: HttpRequest, motion: Motion, errors: list[str], notice: str = "") -> HttpResponse:
        if self.is_htmx or request.headers.get("x-requested-with") == "XMLHttpRequest":
            return render(
                request,
                "work/motions/partials/_attachments.html",
                {
                    "organization": self.organization,
                    "motion": motion,
                    "documents": motion.documents.all(),
                    "can_edit_attachments": True,
                    "attachment_errors": errors,
                    "attachment_notice": notice,
                    "attachment_accept": attachments.ACCEPT,
                    "attachment_max_mb": attachments.ATTACHMENT_MAX_BYTES // (1024 * 1024),
                },
            )
        for error in errors:
            messages.error(request, error)
        if notice:
            messages.success(request, notice)
        return redirect("work:document_editor", org_slug=motion.organization.slug, motion_id=motion.id)


class MotionDocumentUploadView(_AttachmentViewMixin, View):
    """Eine oder mehrere Dateien an ein Dokument hängen."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        motion = self._motion(kwargs.get("motion_id"))
        files = request.FILES.getlist("file")
        if not files:
            return self._respond(request, motion, ["Bitte eine Datei auswählen."])
        membership = self.membership
        assert membership is not None  # WorkViewMixin: nur mit Mitgliedschaft
        created, errors = attachments.add_attachments(motion, membership, files)
        notice = ""
        if created:
            notice = "Anhang hinzugefügt." if len(created) == 1 else f"{len(created)} Anhänge hinzugefügt."
        return self._respond(request, motion, errors, notice)


class MotionDocumentRenameView(_AttachmentViewMixin, View):
    """Anzeigenamen eines Anhangs ändern (die Endung bleibt)."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        motion = self._motion(kwargs.get("motion_id"))
        document = get_object_or_404(MotionDocument, id=kwargs.get("document_id"), motion=motion)
        error = attachments.rename_attachment(document, request.POST.get("filename", ""))
        if error:
            return self._respond(request, motion, [error])
        return self._respond(request, motion, [], "Anhang umbenannt.")


class MotionDocumentDeleteView(_AttachmentViewMixin, View):
    """Anhang entfernen (Datei im Speicher nach der Transaktion)."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        motion = self._motion(kwargs.get("motion_id"))
        document = get_object_or_404(MotionDocument, id=kwargs.get("document_id"), motion=motion)
        attachments.delete_attachment(document)
        return self._respond(request, motion, [], "Anhang entfernt.")
