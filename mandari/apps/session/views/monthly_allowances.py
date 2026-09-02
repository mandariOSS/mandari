# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Monatliche Pauschalen nach EntschVO NRW — UI.

Aufwandsentschädigung (Voll-/Teilpauschale) und Funktionszulagen
(z. B. Fraktionsvorsitz) als monatliche Posten: Katalog pflegen ->
Personen zuordnen -> Monatslauf -> Genehmigung -> Export (CSV/SEPA).
Alle Views erfordern ``manage_allowances`` (Bankdaten!).
"""

import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import ProtectedError
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import (
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionPerson,
    SessionPersonMonthlyRate,
)
from ..permissions import SessionViewMixin
from ..services import allowance_service

logger = logging.getLogger(__name__)

_BOM = "﻿"


def _parse_period(request):
    """Jahr/Monat aus GET/POST lesen (Default: aktueller Monat)."""
    today = timezone.localdate()
    data = request.POST if request.method == "POST" else request.GET
    try:
        year = int(data.get("year", today.year))
        month = int(data.get("month", today.month))
        return date(year, month, 1)
    except (TypeError, ValueError):
        return date(today.year, today.month, 1)


def _period_allowances(view, period):
    return (
        SessionMonthlyAllowance.objects.filter(tenant=view.session_tenant, period=period)
        .select_related("person", "rate", "approved_by__user")
        .order_by("person__family_name", "rate__name")
    )


class MonthlyAllowanceView(SessionViewMixin, TemplateView):
    """Übersicht: Pauschalen-Katalog, Zuordnungen und Monatsabrechnung."""

    template_name = "session/allowances/monthly.html"
    permission_required = "manage_allowances"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        period = _parse_period(self.request)
        allowances = list(_period_allowances(self, period))

        assignments = (
            SessionPersonMonthlyRate.objects.filter(person__tenant=self.session_tenant)
            .select_related("person", "rate")
            .order_by("person__family_name", "rate__name")
        )
        context.update(
            {
                "rates": SessionMonthlyRate.objects.filter(tenant=self.session_tenant),
                "assignments": assignments,
                "period": period,
                "allowances": allowances,
                "sum_pending": sum(a.amount for a in allowances if a.status == "pending"),
                "sum_approved": sum(a.amount for a in allowances if a.status == "approved"),
                "sum_paid": sum(a.amount for a in allowances if a.status == "paid"),
                "persons": SessionPerson.objects.filter(tenant=self.session_tenant, is_active=True).order_by(
                    "family_name", "given_name"
                ),
                "months": range(1, 13),
                "years": range(timezone.localdate().year - 2, timezone.localdate().year + 2),
            }
        )
        return context


class MonthlyRateSaveView(SessionViewMixin, View):
    """Pauschale im Katalog anlegen oder ändern."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        name = request.POST.get("name", "").strip()[:200]
        try:
            amount = Decimal(request.POST.get("amount", "").replace(",", "."))
            assert Decimal("0") <= amount <= Decimal("99999")
        except (InvalidOperation, AssertionError, AttributeError):
            amount = None
        if not name or amount is None:
            messages.error(request, "Bitte Bezeichnung und gültigen Betrag angeben.")
            return redirect("session:allowances_monthly", tenant_slug=tenant_slug)

        rate = None
        rate_id = request.POST.get("rate_id", "").strip()
        if rate_id:
            try:
                rate = SessionMonthlyRate.objects.filter(tenant=self.session_tenant, pk=rate_id).first()
            except (ValueError, DjangoValidationError):
                rate = None

        values = {
            "name": name,
            "amount": amount,
            "legal_basis": request.POST.get("legal_basis", "").strip()[:200],
            "is_active": request.POST.get("is_active", "1") == "1",
        }
        if rate is None:
            rate = SessionMonthlyRate.objects.create(tenant=self.session_tenant, **values)
            action = "create"
        else:
            for key, value in values.items():
                setattr(rate, key, value)
            rate.save()
            action = "update"

        audit.log_event(
            action,
            rate,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"pauschale": name, "betrag": str(amount)},
        )
        messages.success(request, f"Pauschale „{name}“ gespeichert.")
        return redirect("session:allowances_monthly", tenant_slug=tenant_slug)


class MonthlyRateDeleteView(SessionViewMixin, View):
    """Pauschale löschen (blockiert, wenn bereits abgerechnet wurde)."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        try:
            rate = SessionMonthlyRate.objects.filter(tenant=self.session_tenant, pk=request.POST.get("rate_id")).first()
        except (ValueError, DjangoValidationError):
            rate = None
        if rate is None:
            messages.error(request, "Pauschale nicht gefunden.")
            return redirect("session:allowances_monthly", tenant_slug=tenant_slug)
        try:
            audit.log_event(
                "delete",
                rate,
                tenant=self.session_tenant,
                user=self.session_user,
                request=request,
            )
            rate.delete()
            messages.success(request, "Pauschale gelöscht.")
        except ProtectedError:
            messages.error(
                request,
                "Diese Pauschale wurde bereits abgerechnet und kann nicht gelöscht "
                "werden — bitte stattdessen deaktivieren.",
            )
        return redirect("session:allowances_monthly", tenant_slug=tenant_slug)


class MonthlyAssignmentSaveView(SessionViewMixin, View):
    """Pauschale einer Person zuordnen (mit optionalem Zeitraum)."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        person = rate = None
        try:
            person = SessionPerson.objects.filter(tenant=self.session_tenant, pk=request.POST.get("person")).first()
            rate = SessionMonthlyRate.objects.filter(tenant=self.session_tenant, pk=request.POST.get("rate")).first()
        except (ValueError, DjangoValidationError):
            pass
        if person is None or rate is None:
            messages.error(request, "Bitte Person und Pauschale auswählen.")
            return redirect("session:allowances_monthly", tenant_slug=tenant_slug)

        def _parse_date(raw):
            try:
                return date.fromisoformat(raw)
            except (TypeError, ValueError):
                return None

        assignment, created = SessionPersonMonthlyRate.objects.update_or_create(
            person=person,
            rate=rate,
            defaults={
                "start_date": _parse_date(request.POST.get("start_date", "")),
                "end_date": _parse_date(request.POST.get("end_date", "")),
            },
        )
        audit.log_event(
            "create" if created else "update",
            assignment,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"person": person.display_name, "pauschale": rate.name},
        )
        messages.success(request, f"{person.display_name} erhält „{rate.name}“.")
        return redirect("session:allowances_monthly", tenant_slug=tenant_slug)


class MonthlyAssignmentDeleteView(SessionViewMixin, View):
    """Pauschalen-Zuordnung beenden/entfernen."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        try:
            assignment = (
                SessionPersonMonthlyRate.objects.filter(
                    person__tenant=self.session_tenant, pk=request.POST.get("assignment_id")
                )
                .select_related("person", "rate")
                .first()
            )
        except (ValueError, DjangoValidationError):
            assignment = None
        if assignment is None:
            messages.error(request, "Zuordnung nicht gefunden.")
        else:
            audit.log_event(
                "delete",
                assignment,
                tenant=self.session_tenant,
                user=self.session_user,
                request=request,
                changes={"person": assignment.person.display_name, "pauschale": assignment.rate.name},
            )
            assignment.delete()
            messages.success(request, "Zuordnung entfernt (bereits abgerechnete Monate bleiben bestehen).")
        return redirect("session:allowances_monthly", tenant_slug=tenant_slug)


class MonthlyGenerateView(SessionViewMixin, View):
    """Monatslauf: Posten für alle aktiven Zuordnungen erzeugen."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        period = _parse_period(request)
        result = allowance_service.generate_monthly_allowances(
            self.session_tenant, period.year, period.month, created_by=self.session_user
        )
        audit.log_event(
            "create",
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"monatslauf": period.strftime("%m/%Y"), "neu": result["created"]},
        )
        messages.success(
            request,
            f"Monatslauf {period:%m/%Y}: {result['created']} Posten erzeugt, {result['skipped']} bereits vorhanden.",
        )
        return redirect(f"/session/{tenant_slug}/allowances/monthly/?year={period.year}&month={period.month}")


class MonthlyApproveView(SessionViewMixin, View):
    """Alle ausstehenden Posten des Monats genehmigen (Vier-Augen-Prinzip)."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        period = _parse_period(request)
        pending = _period_allowances(self, period).filter(status="pending")
        result = allowance_service.approve_monthly_allowances(pending, self.session_user)
        audit.log_event(
            "approve",
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"monat": period.strftime("%m/%Y"), "genehmigt": result["approved"]},
        )
        messages.success(request, f"{result['approved']} Posten für {period:%m/%Y} genehmigt.")
        return redirect(f"/session/{tenant_slug}/allowances/monthly/?year={period.year}&month={period.month}")


class MonthlyCsvExportView(SessionViewMixin, View):
    """CSV-Export der Monats-Pauschalen (auditiert, enthält Bankdaten)."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        period = _parse_period(request)
        allowances = list(_period_allowances(self, period).exclude(status="cancelled"))
        if not allowances:
            messages.warning(request, "Keine Posten im Monat — nichts zu exportieren.")
            return redirect("session:allowances_monthly", tenant_slug=tenant_slug)

        csv_text = allowance_service.build_monthly_export_csv(allowances)
        audit.log_event(
            "download",
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"export": "monatspauschalen_csv", "monat": period.strftime("%m/%Y"), "anzahl": len(allowances)},
        )
        response = HttpResponse(_BOM + csv_text, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="pauschalen-{period:%Y-%m}.csv"'
        return response


class MonthlySepaExportView(SessionViewMixin, View):
    """SEPA-Export der GENEHMIGTEN Monats-Pauschalen; markiert als ausgezahlt."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        period = _parse_period(request)
        debtor = (self.session_tenant.settings or {}).get("allowances", {})
        if not debtor.get("debtor_iban"):
            messages.error(
                request,
                "SEPA-Export nicht möglich — bitte zuerst das Auftraggeberkonto der "
                "Kommune bei den Sitzungsgeldern hinterlegen.",
            )
            return redirect("session:allowances_monthly", tenant_slug=tenant_slug)

        allowances = list(_period_allowances(self, period).filter(status="approved"))
        if not allowances:
            messages.warning(request, "Keine genehmigten Posten im Monat — nichts zu exportieren.")
            return redirect("session:allowances_monthly", tenant_slug=tenant_slug)

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
                "SEPA-Export nicht möglich — für keine der Personen ist eine IBAN hinterlegt: " + ", ".join(skipped),
            )
            return redirect("session:allowances_monthly", tenant_slug=tenant_slug)

        exportable = [a for a in allowances if a.person.get_bank_iban_decrypted()]
        allowance_service.mark_monthly_exported(exportable, reference)
        audit.log_event(
            "download",
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "export": "monatspauschalen_sepa",
                "monat": period.strftime("%m/%Y"),
                "referenz": reference,
                "transaktionen": txn_count,
                "summe": str(total),
                "uebersprungen": skipped,
            },
        )
        if skipped:
            messages.warning(request, "Ohne IBAN übersprungen: " + ", ".join(skipped))
        messages.success(
            request,
            f"SEPA-Datei {reference} erstellt ({txn_count} Überweisungen) — Posten als ausgezahlt markiert.",
        )
        response = HttpResponse(xml_bytes, content_type="application/xml")
        response["Content-Disposition"] = f'attachment; filename="pauschalen-{period:%Y-%m}-{reference}.xml"'
        return response
