# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsgeld-Abrechnung — UI (Issue #38).

Alle Views erfordern die Berechtigung ``manage_allowances`` (Bankdaten!).
Ablauf: Sätze pflegen -> Abrechnungslauf für einen Zeitraum -> Genehmigung
(Vier-Augen-Prinzip) -> Export (CSV/SEPA) -> Jahresübersicht.
Jeder Export wird auditiert.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import (
    SessionAllowance,
    SessionAllowanceRate,
    SessionOrganization,
    SessionPerson,
)
from ..permissions import SessionViewMixin
from ..services import allowance_service

logger = logging.getLogger(__name__)

# UTF-8-BOM für Excel-kompatible CSV-Dateien
_BOM = "﻿"


def _allowance_queryset(view, period_start, period_end, organization_id="", status=""):
    """Positionen des Mandanten im Zeitraum (tenant-sicher, mit Filtern)."""
    qs = (
        SessionAllowance.objects.filter(
            attendance__meeting__tenant=view.session_tenant,
            attendance__meeting__start__date__gte=period_start,
            attendance__meeting__start__date__lte=period_end,
        )
        .select_related(
            "attendance__person",
            "attendance__meeting__organization",
            "created_by__user",
            "approved_by__user",
        )
        .order_by("attendance__person__family_name", "attendance__meeting__start")
    )
    if organization_id:
        qs = qs.filter(attendance__meeting__organization_id=organization_id)
    if status:
        qs = qs.filter(status=status)
    return qs


def _debtor_settings(tenant) -> dict:
    """SEPA-Auftraggeberkonto der Kommune aus den Mandanten-Einstellungen."""
    return (tenant.settings or {}).get("allowances", {})


class AllowanceListView(SessionViewMixin, TemplateView):
    """Sitzungsgeld-Übersicht: Sätze, Abrechnungslauf, Positionen, Exporte."""

    template_name = "session/allowances/index.html"
    permission_required = "manage_allowances"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        period_start, period_end = allowance_service.parse_period(
            self.request.GET.get("from", ""), self.request.GET.get("to", "")
        )
        if period_start is None:
            messages.error(self.request, "Ungültiger Zeitraum — es wird der laufende Monat angezeigt.")
            period_start, period_end = allowance_service.parse_period("", "")

        organization_id = self.request.GET.get("organization", "")
        status = self.request.GET.get("status", "")
        allowances = list(_allowance_queryset(self, period_start, period_end, organization_id, status))

        context["period_start"] = period_start
        context["period_end"] = period_end
        context["selected_organization"] = organization_id
        context["selected_status"] = status
        context["allowances"] = allowances
        context["total_amount"] = sum((a.amount for a in allowances), Decimal("0.00"))
        context["pending_count"] = sum(1 for a in allowances if a.status == "pending")
        context["approved_count"] = sum(1 for a in allowances if a.status == "approved")
        context["paid_count"] = sum(1 for a in allowances if a.status == "paid")

        context["organizations"] = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        ).order_by("name")
        context["rates"] = SessionAllowanceRate.objects.filter(
            organization__tenant=self.session_tenant
        ).select_related("organization")
        context["rate_roles"] = SessionAllowanceRate._meta.get_field("role").choices
        context["allowance_statuses"] = SessionAllowance._meta.get_field("status").choices
        context["debtor"] = _debtor_settings(self.session_tenant)
        return context


class AllowanceRateSaveView(SessionViewMixin, View):
    """Entschädigungssatz je Gremium/Funktion speichern."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        organization = get_object_or_404(
            SessionOrganization, pk=request.POST.get("organization"), tenant=self.session_tenant
        )
        role = request.POST.get("role", "member")
        valid_roles = {c[0] for c in SessionAllowanceRate._meta.get_field("role").choices}
        if role not in valid_roles:
            role = "member"
        try:
            amount = Decimal(str(request.POST.get("amount", "")).replace(",", "."))
        except (InvalidOperation, TypeError):
            messages.error(request, "Ungültiger Betrag für den Entschädigungssatz.")
            return redirect("session:allowances", tenant_slug=tenant_slug)
        if amount < 0:
            messages.error(request, "Der Entschädigungssatz darf nicht negativ sein.")
            return redirect("session:allowances", tenant_slug=tenant_slug)

        rate, _created = SessionAllowanceRate.objects.update_or_create(
            organization=organization, role=role, defaults={"amount": amount}
        )
        messages.success(
            request,
            f"Satz für {organization.name} / {rate.get_role_display()}: {amount} EUR gespeichert.",
        )
        return redirect("session:allowances", tenant_slug=tenant_slug)


class AllowanceRateDeleteView(SessionViewMixin, View):
    """Entschädigungssatz löschen (Fallback: Gremiums-Standardsatz)."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, rate_id):
        rate = get_object_or_404(
            SessionAllowanceRate, pk=rate_id, organization__tenant=self.session_tenant
        )
        rate.delete()
        messages.success(request, "Entschädigungssatz entfernt.")
        return redirect("session:allowances", tenant_slug=tenant_slug)


class AllowanceDebtorSaveView(SessionViewMixin, View):
    """SEPA-Auftraggeberkonto der Kommune speichern (Mandanten-Einstellungen)."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        tenant = self.session_tenant
        settings = tenant.settings or {}
        settings.setdefault("allowances", {})
        settings["allowances"].update(
            {
                "debtor_name": (request.POST.get("debtor_name") or "").strip()[:70],
                "debtor_iban": (request.POST.get("debtor_iban") or "").replace(" ", "").upper()[:34],
                "debtor_bic": (request.POST.get("debtor_bic") or "").replace(" ", "").upper()[:11],
            }
        )
        tenant.settings = settings
        tenant.save(update_fields=["settings", "updated_at"])
        messages.success(request, "Auftraggeberkonto für den SEPA-Export gespeichert.")
        return redirect("session:allowances", tenant_slug=tenant_slug)


class AllowanceGenerateView(SessionViewMixin, View):
    """Abrechnungslauf: Positionen aus Anwesenheiten erzeugen."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        period_start, period_end = allowance_service.parse_period(
            request.POST.get("from", ""), request.POST.get("to", "")
        )
        if period_start is None:
            messages.error(request, "Ungültiger Zeitraum für den Abrechnungslauf.")
            return redirect("session:allowances", tenant_slug=tenant_slug)

        organization = None
        if request.POST.get("organization"):
            organization = get_object_or_404(
                SessionOrganization, pk=request.POST["organization"], tenant=self.session_tenant
            )

        stats = allowance_service.generate_allowances(
            self.session_tenant,
            period_start,
            period_end,
            organization=organization,
            created_by=self.session_user,
        )

        audit.log_event(
            "create",
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "abrechnungslauf": {
                    "zeitraum": f"{period_start.isoformat()} bis {period_end.isoformat()}",
                    "gremium": organization.name if organization else "alle",
                    "erzeugt": stats["created"],
                    "summe": f"{stats['total']:.2f}",
                    "uebersprungen_vorhanden": stats["skipped_existing"],
                    "uebersprungen_satz_null": stats["skipped_zero"],
                }
            },
        )
        messages.success(
            request,
            f"Abrechnungslauf: {stats['created']} Position(en) über {stats['total']:.2f} EUR erzeugt "
            f"({stats['skipped_existing']} bereits abgerechnet, {stats['skipped_zero']} ohne Satz).",
        )
        return redirect(
            f"/session/{tenant_slug}/allowances/?from={period_start.isoformat()}&to={period_end.isoformat()}"
        )


class AllowanceApproveView(SessionViewMixin, View):
    """Genehmigung der Positionen im Zeitraum (Vier-Augen-Prinzip)."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        period_start, period_end = allowance_service.parse_period(
            request.POST.get("from", ""), request.POST.get("to", "")
        )
        if period_start is None:
            messages.error(request, "Ungültiger Zeitraum für die Genehmigung.")
            return redirect("session:allowances", tenant_slug=tenant_slug)

        allowances = _allowance_queryset(
            self, period_start, period_end, request.POST.get("organization", ""), status="pending"
        )
        stats = allowance_service.approve_allowances(allowances, self.session_user)

        if stats["blocked_four_eyes"]:
            messages.warning(
                request,
                f"{stats['blocked_four_eyes']} Position(en) nicht genehmigt — Vier-Augen-Prinzip: "
                "der Abrechnungslauf wurde von Ihnen selbst erzeugt.",
            )
        if stats["approved"]:
            messages.success(request, f"{stats['approved']} Position(en) genehmigt.")
        elif not stats["blocked_four_eyes"]:
            messages.info(request, "Keine ausstehenden Positionen im Zeitraum.")
        return redirect(
            f"/session/{tenant_slug}/allowances/?from={period_start.isoformat()}&to={period_end.isoformat()}"
        )


class AllowanceCsvExportView(SessionViewMixin, View):
    """Generischer CSV-Export fürs Finanzverfahren (auditiert)."""

    permission_required = "manage_allowances"

    def get(self, request, tenant_slug):
        period_start, period_end = allowance_service.parse_period(
            request.GET.get("from", ""), request.GET.get("to", "")
        )
        if period_start is None:
            return HttpResponse(status=400)

        allowances = list(
            _allowance_queryset(
                self, period_start, period_end, request.GET.get("organization", ""), request.GET.get("status", "")
            )
        )
        csv_text = allowance_service.build_export_csv(allowances)

        audit.log_event(
            "download",
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "sitzungsgeld_export": {
                    "format": "csv",
                    "zeitraum": f"{period_start.isoformat()} bis {period_end.isoformat()}",
                    "positionen": len(allowances),
                }
            },
        )

        response = HttpResponse(_BOM + csv_text, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="sitzungsgeld-{period_start.isoformat()}-{period_end.isoformat()}.csv"'
        )
        return response


class AllowanceSepaExportView(SessionViewMixin, View):
    """
    SEPA-pain.001-Export der GENEHMIGTEN Positionen (auditiert).

    Markiert die exportierten Positionen als ausgezahlt und vergibt eine
    fortlaufende Export-Referenz fürs Finanzverfahren.
    """

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        period_start, period_end = allowance_service.parse_period(
            request.POST.get("from", ""), request.POST.get("to", "")
        )
        if period_start is None:
            messages.error(request, "Ungültiger Zeitraum für den SEPA-Export.")
            return redirect("session:allowances", tenant_slug=tenant_slug)

        debtor = _debtor_settings(self.session_tenant)
        if not debtor.get("debtor_iban"):
            messages.error(
                request,
                "SEPA-Export nicht möglich — bitte zuerst das Auftraggeberkonto der Kommune hinterlegen.",
            )
            return redirect("session:allowances", tenant_slug=tenant_slug)

        allowances = list(
            _allowance_queryset(
                self, period_start, period_end, request.POST.get("organization", ""), status="approved"
            )
        )
        if not allowances:
            messages.warning(request, "Keine genehmigten Positionen im Zeitraum — nichts zu exportieren.")
            return redirect(
                f"/session/{tenant_slug}/allowances/?from={period_start.isoformat()}&to={period_end.isoformat()}"
            )

        reference = allowance_service.next_export_reference(self.session_tenant)
        xml_bytes, txn_count, total, skipped = allowance_service.build_sepa_xml(
            self.session_tenant,
            allowances,
            debtor_name=debtor.get("debtor_name") or self.session_tenant.name,
            debtor_iban=debtor["debtor_iban"],
            debtor_bic=debtor.get("debtor_bic", ""),
            reference=reference,
        )

        if txn_count == 0:
            messages.error(
                request,
                "SEPA-Export nicht möglich — für keine der Personen ist eine IBAN hinterlegt: "
                + ", ".join(skipped),
            )
            return redirect(
                f"/session/{tenant_slug}/allowances/?from={period_start.isoformat()}&to={period_end.isoformat()}"
            )

        # Nur Positionen von Personen MIT IBAN als exportiert/ausgezahlt markieren
        exportable = [
            a for a in allowances if (a.attendance.person.get_bank_iban_decrypted() or "").strip()
        ]
        allowance_service.mark_exported(exportable, reference, mark_paid=True)

        audit.log_event(
            "download",
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "sitzungsgeld_export": {
                    "format": "sepa-pain.001",
                    "referenz": reference,
                    "zeitraum": f"{period_start.isoformat()} bis {period_end.isoformat()}",
                    "transaktionen": txn_count,
                    "summe": f"{total:.2f}",
                    "ohne_iban": skipped,
                }
            },
        )
        if skipped:
            messages.warning(
                request,
                "Ohne IBAN übersprungen (Positionen bleiben genehmigt): " + ", ".join(skipped),
            )

        response = HttpResponse(xml_bytes, content_type="application/xml")
        response["Content-Disposition"] = f'attachment; filename="{reference}-pain001.xml"'
        return response


class AllowanceNoticePdfView(SessionViewMixin, View):
    """Abrechnungsmitteilung als PDF je Empfänger (Issue #38)."""

    permission_required = "manage_allowances"

    def get(self, request, tenant_slug, person_id):
        period_start, period_end = allowance_service.parse_period(
            request.GET.get("from", ""), request.GET.get("to", "")
        )
        if period_start is None:
            return HttpResponse(status=400)

        person = get_object_or_404(SessionPerson, pk=person_id, tenant=self.session_tenant)
        allowances = list(
            _allowance_queryset(self, period_start, period_end).filter(attendance__person=person)
        )
        if not allowances:
            messages.warning(request, f"Keine Sitzungsgeld-Positionen für {person.display_name} im Zeitraum.")
            return redirect("session:allowances", tenant_slug=tenant_slug)

        pdf_bytes = allowance_service.build_notice_pdf(
            self.session_tenant, person, allowances, period_start, period_end
        )
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'attachment; filename="abrechnung-{person.family_name.lower()}-'
            f'{period_start.isoformat()}-{period_end.isoformat()}.pdf"'
        )
        return response


class AllowanceYearView(SessionViewMixin, TemplateView):
    """Jahresübersicht je Person (Grundlage Steuerbescheinigung)."""

    template_name = "session/allowances/year.html"
    permission_required = "manage_allowances"

    def get(self, request, *args, **kwargs):
        from django.utils import timezone as _tz

        try:
            year = int(request.GET.get("year", _tz.localdate().year))
        except (TypeError, ValueError):
            year = _tz.localdate().year
        self.year = year

        if request.GET.get("format") == "csv":
            rows = allowance_service.year_summary(self.session_tenant, year)
            csv_text = allowance_service.year_summary_csv(rows, year)
            audit.log_event(
                "download",
                self.session_tenant,
                tenant=self.session_tenant,
                user=self.session_user,
                request=request,
                changes={"sitzungsgeld_export": {"format": "jahresuebersicht-csv", "jahr": year}},
            )
            response = HttpResponse(_BOM + csv_text, content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="sitzungsgeld-jahr-{year}.csv"'
            return response

        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["year"] = self.year
        context["rows"] = allowance_service.year_summary(self.session_tenant, self.year)
        context["year_total"] = sum((row["total"] for row in context["rows"]), Decimal("0.00"))
        return context
