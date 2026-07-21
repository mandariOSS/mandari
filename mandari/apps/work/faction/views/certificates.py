# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teilnahmenachweis + Sammel-Export (Issue #68).

- FactionCertificateDownloadView: Mitglieder laden ihre EIGENEN, vom
  Vorstand bestätigten Teilnahmen als PDF-Nachweis (Zeitraum wählbar,
  Prüfcode + QR).
- FactionAttendanceExportView: Sammel-Export für den Vorstand — alle
  bestätigten Teilnahmen je Person/Zeitraum als PDF/CSV (Sitzungsgeld).
- CertificateVerifyView: öffentliche Verifikations-Seite (ohne Login);
  zeigt AUSSCHLIESSLICH Gültigkeit, Ausstellungsdatum, Organisation,
  Anzahl und Zeitraum — niemals personenbezogene Daten.
"""

import logging
from datetime import date, datetime

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from ..models import FactionAttendanceCertificate

logger = logging.getLogger(__name__)


def _parse_period(request):
    """Zeitraum aus ?from=YYYY-MM-DD&to=YYYY-MM-DD lesen (Default: laufendes Jahr)."""
    today = timezone.localdate()
    period_start = date(today.year, 1, 1)
    period_end = today

    raw_from = (request.GET.get("from") or "").strip()
    raw_to = (request.GET.get("to") or "").strip()
    try:
        if raw_from:
            period_start = datetime.strptime(raw_from, "%Y-%m-%d").date()
        if raw_to:
            period_end = datetime.strptime(raw_to, "%Y-%m-%d").date()
    except ValueError:
        return None, None
    if period_start > period_end:
        return None, None
    return period_start, period_end


class FactionCertificateDownloadView(WorkViewMixin, View):
    """Eigenen Teilnahmenachweis als PDF herunterladen (nur bestätigte Teilnahmen)."""

    permission_required = "faction.view_public"

    def get(self, request, *args, **kwargs):
        period_start, period_end = _parse_period(request)
        if period_start is None:
            messages.error(request, "Ungültiger Zeitraum für den Teilnahmenachweis.")
            return redirect("work:faction", org_slug=self.organization.slug)

        from ..certificates import build_certificate_pdf, issue_certificate

        certificate, attendances = issue_certificate(
            self.membership, period_start, period_end, issued_by=self.membership
        )
        if certificate is None:
            messages.warning(
                request,
                "Im gewählten Zeitraum gibt es keine vom Vorstand bestätigten Teilnahmen — "
                "es wurde kein Nachweis ausgestellt.",
            )
            return redirect("work:faction", org_slug=self.organization.slug)

        pdf_bytes = build_certificate_pdf(certificate, attendances)
        filename = f"teilnahmenachweis-{period_start.isoformat()}-{period_end.isoformat()}.pdf"
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class FactionAttendanceExportView(WorkViewMixin, View):
    """
    Sammel-Export für den Vorstand (Issue #68).

    Alle bestätigten Teilnahmen je Person/Zeitraum als PDF oder CSV zum
    Einreichen bei der Verwaltung (Sitzungsgeld sachkundiger Bürger:innen).
    Berechtigt sind — wie bei der Teilnahme-Bestätigung (Issue #67) —
    Vorstand/Vorsitz (inkl. stellv. Vorsitz); Fallback ohne besetzten
    Vorstand: faction.manage.
    """

    permission_required = "faction.view_public"

    def get(self, request, *args, **kwargs):
        from ..invitations import can_confirm_attendance

        if not can_confirm_attendance(self.membership):
            return HttpResponse(status=403)

        period_start, period_end = _parse_period(request)
        if period_start is None:
            messages.error(request, "Ungültiger Zeitraum für den Sammel-Export.")
            return redirect("work:faction", org_slug=self.organization.slug)

        export_format = (request.GET.get("format") or "pdf").lower()
        if export_format not in ("pdf", "csv"):
            return HttpResponse(status=400)

        from ..audit import log_event
        from ..certificates import build_bulk_export_csv, build_bulk_export_pdf, bulk_confirmed_attendances

        attendances = list(bulk_confirmed_attendances(self.organization, period_start, period_end))

        log_event(
            "attendance_exported",
            self.organization,
            organization=self.organization,
            membership=self.membership,
            is_internal=False,
            changes={
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "format": export_format,
                "rows": len(attendances),
            },
        )

        stem = f"teilnahmen-{period_start.isoformat()}-{period_end.isoformat()}"
        if export_format == "csv":
            csv_text = build_bulk_export_csv(self.organization, period_start, period_end, attendances)
            # BOM für Excel-kompatible UTF-8-Erkennung
            response = HttpResponse("﻿" + csv_text, content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="{stem}.csv"'
            return response

        pdf_bytes = build_bulk_export_pdf(self.organization, period_start, period_end, attendances)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{stem}.pdf"'
        return response


class CertificateVerifyView(TemplateView):
    """
    Öffentliche Verifikations-Seite für Teilnahmenachweise (ohne Login).

    Datenschutz (Akzeptanzkriterium Issue #68): Die Seite zeigt bei einem
    gültigen Token AUSSCHLIESSLICH "Nachweis gültig, ausgestellt am X,
    Organisation Y, N bestätigte Teilnahmen im Zeitraum Z" sowie die
    Prüfsumme — niemals Namen oder andere personenbezogene Daten.
    Unbekannte Token werden mit 404 sauber abgewiesen.
    """

    template_name = "pages/public/certificate_verify.html"

    def get(self, request, *args, **kwargs):
        token = kwargs.get("token", "")
        certificate = FactionAttendanceCertificate.objects.select_related("organization").filter(token=token).first()
        context = self.get_context_data(**kwargs)
        context["certificate"] = certificate
        context["valid"] = certificate is not None
        return self.render_to_response(context, status=200 if certificate else 404)
