# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Statistiken und Berichte (Issue #84).
"""

import csv

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionOrganization
from ..permissions import SessionViewMixin
from ..services import report_service


def _parse_year(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
        assert 2000 <= year <= 2100
        return year
    except (TypeError, ValueError, AssertionError):
        return today.year


def _parse_organization(view, request):
    org_id = request.GET.get("organization")
    if not org_id:
        return None
    try:
        return SessionOrganization.objects.filter(tenant=view.session_tenant, pk=org_id).first()
    except (ValueError, DjangoValidationError):
        return None


class ReportsView(SessionViewMixin, TemplateView):
    """Berichtsseite: Anwesenheit, Sitzungen, Sitzungsgeld, Durchlaufzeiten."""

    template_name = "session/reports/index.html"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tenant = self.session_tenant
        year = _parse_year(self.request)
        organization = _parse_organization(self, self.request)
        include_np = self.has_permission("view_non_public_meetings")

        context.update(
            {
                "year": year,
                "years": report_service.available_years(tenant),
                "organization": organization,
                "organizations": SessionOrganization.objects.filter(tenant=tenant, is_active=True).order_by("name"),
                "attendance": report_service.attendance_stats(
                    tenant, year, organization, include_non_public=include_np
                ),
                "meeting_stats": report_service.meeting_stats(tenant, year, include_non_public=include_np),
                "paper_throughput": report_service.paper_throughput(tenant, year),
                "can_view_allowances": self.has_permission("manage_allowances"),
            }
        )
        if context["can_view_allowances"]:
            allowances, totals = report_service.allowance_stats(tenant, year)
            context["allowances"] = allowances
            context["allowance_totals"] = totals
        return context


class ReportCsvExportView(SessionViewMixin, View):
    """CSV-Export der Berichte (?type=attendance|allowances)."""

    permission_required = "view_meetings"
    http_method_names = ["get"]

    def get(self, request, tenant_slug):
        tenant = self.session_tenant
        year = _parse_year(request)
        export_type = request.GET.get("type", "attendance")
        include_np = self.has_permission("view_non_public_meetings")

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response.write("﻿")  # BOM für Excel
        writer = csv.writer(response, delimiter=";")

        if export_type == "allowances":
            if not self.has_permission("manage_allowances"):
                response.status_code = 403
                return response
            response["Content-Disposition"] = f'attachment; filename="sitzungsgeld-{year}.csv"'
            writer.writerow(["Person", "Sitzungsgelder (Anzahl)", "Pauschalen", "Summe", "Davon ausgezahlt"])
            rows, totals = report_service.allowance_stats(tenant, year)
            for row in rows:
                writer.writerow([row["name"], row["count"], row["monthly"], row["amount"], row["paid"]])
            writer.writerow(["Gesamt", totals["count"], totals["monthly"], totals["amount"], totals["paid"]])
        else:
            export_type = "attendance"
            organization = _parse_organization(self, request)
            response["Content-Disposition"] = f'attachment; filename="anwesenheit-{year}.csv"'
            writer.writerow(["Person", "Eingeladen", "Anwesend", "Entschuldigt", "Abwesend", "Quote %"])
            for row in report_service.attendance_stats(tenant, year, organization, include_non_public=include_np):
                writer.writerow(
                    [row["name"], row["invited"], row["present"], row["excused"], row["absent"], row["rate"]]
                )

        audit.log_event(
            "download",
            tenant,
            tenant=tenant,
            user=self.session_user,
            request=request,
            changes={"export": f"bericht_{export_type}_csv", "jahr": year},
        )
        return response
