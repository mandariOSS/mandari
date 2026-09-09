# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben-Export und Datei-Import für das Work-Modul (Issue #7).

Die Formate, Validierung und Duplikat-Regel liegen in
``apps.work.tasks.export_service`` und ``apps.work.tasks.import_service``;
die Views reichen Upload bzw. Format durch und formen die Antwort.
"""

import logging

from django.http import HttpResponse, JsonResponse
from django.views.generic import View

from apps.common.mixins import WorkViewMixin

from .. import export_service, import_service

logger = logging.getLogger(__name__)


class TaskExportView(WorkViewMixin, View):
    """Export der sichtbaren Aufgaben als CSV, JSON oder XML (Download)."""

    permission_required = "tasks.view"

    def get(self, request, *args, **kwargs):
        export_format = (request.GET.get("format") or "csv").lower()
        try:
            content, content_type, filename = export_service.export_tasks(
                self.organization, self.membership, export_format
            )
        except export_service.UnknownExportFormatError:
            return JsonResponse({"error": "Unbekanntes Format. Erlaubt: csv, json, xml."}, status=400)

        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class TaskFileImportView(WorkViewMixin, View):
    """
    Datei-Import von Aufgaben (CSV, JSON, XML).

    POST mit multipart "file". Mit dry_run=1 wird nur validiert und ein
    Vorschau-Bericht zurückgegeben, ohne Daten zu schreiben.
    """

    permission_required = "tasks.create"

    def post(self, request, *args, **kwargs):
        upload = request.FILES.get("file")
        if not upload:
            return JsonResponse({"error": "Keine Datei übermittelt."}, status=400)
        if upload.size > import_service.MAX_IMPORT_FILE_SIZE:
            return JsonResponse({"error": "Datei zu groß (max. 5 MB)."}, status=400)

        try:
            file_format, rows = import_service.parse_upload(upload.name or "", upload.read())
        except import_service.TaskImportError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        dry_run = request.POST.get("dry_run") in ("1", "true")
        report = import_service.classify_rows(self.organization, self.membership, rows)
        if not dry_run:
            import_service.apply_import(self.organization, self.membership, report)

        return JsonResponse({"success": True, "dry_run": dry_run, "format": file_format, "report": report.summary()})
