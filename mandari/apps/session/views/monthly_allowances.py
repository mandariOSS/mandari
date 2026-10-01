# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Monatliche Pauschalen nach EntschVO NRW — UI.

Aufwandsentschädigung (Voll-/Teilpauschale) und Funktionszulagen
(z. B. Fraktionsvorsitz) als monatliche Posten: Katalog pflegen ->
Personen zuordnen -> Monatslauf -> Genehmigung -> Export (CSV/SEPA).
Alle Views erfordern ``manage_allowances`` (Bankdaten!).

Der Abrechnungsmonat kommt aus ``year``/``month``. Ungültige Angaben führen nie still zu einem anderen
Monat: Die Übersicht meldet sie und zeigt den laufenden Monat, schreibende Aktionen laufen gar nicht.
"""

import logging
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import ProtectedError
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.common.formatting import parse_iso_date

from .. import audit
from ..models import (
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionPerson,
    SessionPersonMonthlyRate,
)
from ..permissions import SessionViewMixin
from ..services import allowance_service, four_eyes_service
from .allowances import _debtor_settings, debtor_problem, warn_nothing_to_export

logger = logging.getLogger(__name__)

_BOM = "﻿"

#: Höchstbetrag einer Monatspauschale
MAX_MONTHLY_AMOUNT = Decimal("99999")

_INVALID_PERIOD = (
    f"Ungültiger Abrechnungsmonat – bitte Monat 1 bis 12 und ein Jahr von {allowance_service.YEAR_MIN} "
    f"bis {allowance_service.YEAR_MAX} angeben."
)


def _parse_period(request) -> date | None:
    """
    Abrechnungsmonat (Monatserster) aus GET/POST; ohne Angabe der laufende Monat.

    Ungültige Angaben (Text, Monat außerhalb 1–12, Jahr außerhalb des Abrechnungsbereichs) ergeben ``None``.
    """
    today = timezone.localdate()
    data = request.POST if request.method == "POST" else request.GET
    raw_year, raw_month = data.get("year"), data.get("month")
    year = allowance_service.parse_year(raw_year) if raw_year else today.year
    try:
        month = int(str(raw_month).strip()) if raw_month else today.month
    except ValueError:
        return None
    if year is None or not 1 <= month <= 12:
        return None
    return date(year, month, 1)


def _period_url(view, period: date | None = None) -> str:
    """Monatsseite, auf Wunsch mit dem gewählten Abrechnungsmonat."""
    url = reverse("session:allowances_monthly", kwargs={"tenant_slug": view.session_tenant.slug})
    return f"{url}?year={period.year}&month={period.month}" if period else url


def _invalid_period(view, request):
    messages.error(request, f"{_INVALID_PERIOD} Es wurde nichts ausgeführt.")
    return redirect(_period_url(view))


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
        if period is None:
            messages.error(self.request, f"{_INVALID_PERIOD} Angezeigt wird der laufende Monat.")
            period = timezone.localdate().replace(day=1)
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
                "sum_pending": sum((a.amount for a in allowances if a.status == "pending"), Decimal("0.00")),
                "sum_approved": sum((a.amount for a in allowances if a.status == "approved"), Decimal("0.00")),
                "sum_paid": sum((a.amount for a in allowances if a.status == "paid"), Decimal("0.00")),
                "open_statuses": allowance_service.OPEN_STATUSES,
                "persons": SessionPerson.objects.filter(tenant=self.session_tenant, is_active=True).order_by(
                    "family_name", "given_name"
                ),
                "months": range(1, 13),
                "years": range(timezone.localdate().year - 2, timezone.localdate().year + 2),
            }
        )
        return context


class MonthlyRateSaveView(SessionViewMixin, View):
    """
    Pauschale im Katalog anlegen oder ändern (``rate_id``).

    Beim Ändern ist ``is_active`` ein Ankreuzfeld: fehlt es, wird die Pauschale deaktiviert und nicht mehr
    abgerechnet. Eine zweite aktive Pauschale mit derselben Bezeichnung entsteht nicht – die jährliche
    Anpassung ändert den Betrag der vorhandenen; bereits erzeugte Posten behalten ihren Betrag.
    """

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        name = request.POST.get("name", "").strip()[:200]
        amount = allowance_service.parse_amount(request.POST.get("amount", ""), maximum=MAX_MONTHLY_AMOUNT)
        if not name or amount is None:
            messages.error(
                request,
                "Bitte Bezeichnung und gültigen Betrag angeben (0,00 bis 99.999,00 € mit höchstens zwei "
                "Nachkommastellen).",
            )
            return redirect("session:allowances_monthly", tenant_slug=tenant_slug)

        rate = None
        rate_id = request.POST.get("rate_id", "").strip()
        if rate_id:
            try:
                rate = SessionMonthlyRate.objects.filter(tenant=self.session_tenant, pk=rate_id).first()
            except (ValueError, DjangoValidationError):
                rate = None
            if rate is None:
                messages.error(request, "Pauschale nicht gefunden.")
                return redirect("session:allowances_monthly", tenant_slug=tenant_slug)

        values = {
            "name": name,
            "amount": amount,
            "legal_basis": request.POST.get("legal_basis", "").strip()[:200],
            # Neu angelegt ist eine Pauschale aktiv; beim Ändern entscheidet das Ankreuzfeld
            "is_active": True if rate is None else request.POST.get("is_active") == "1",
        }
        duplicates = SessionMonthlyRate.objects.filter(tenant=self.session_tenant, name__iexact=name, is_active=True)
        if rate is not None:
            duplicates = duplicates.exclude(pk=rate.pk)
        if values["is_active"] and duplicates.exists():
            messages.error(
                request,
                f"Eine aktive Pauschale „{name}“ gibt es bereits – bitte dort den Betrag ändern oder sie "
                "vorher deaktivieren.",
            )
            return redirect("session:allowances_monthly", tenant_slug=tenant_slug)

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
            changes={"pauschale": name, "betrag": str(amount), "aktiv": values["is_active"]},
        )
        state = "" if values["is_active"] else " (deaktiviert – wird nicht mehr abgerechnet)"
        messages.success(request, f"Pauschale „{name}“ gespeichert{state}.")
        return redirect("session:allowances_monthly", tenant_slug=tenant_slug)


class MonthlyRateDeleteView(SessionViewMixin, View):
    """Pauschale löschen (blockiert, wenn bereits abgerechnet wurde – dann deaktivieren)."""

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
        in_use = (
            "Diese Pauschale wurde bereits abgerechnet und kann nicht gelöscht werden — bitte stattdessen "
            "deaktivieren (Bearbeiten, Haken bei „Aktiv“ entfernen)."
        )
        if rate.allowances.exists():
            messages.error(request, in_use)
            return redirect("session:allowances_monthly", tenant_slug=tenant_slug)
        try:
            # Protokolleintrag und Löschen gemeinsam: Scheitert das Löschen, bleibt auch kein Eintrag „gelöscht“
            with transaction.atomic():
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
            messages.error(request, in_use)
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

        assignment, created = SessionPersonMonthlyRate.objects.update_or_create(
            person=person,
            rate=rate,
            defaults={
                "start_date": parse_iso_date(request.POST.get("start_date", "")),
                "end_date": parse_iso_date(request.POST.get("end_date", "")),
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
        if period is None:
            return _invalid_period(self, request)
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
        return redirect(_period_url(self, period))


class MonthlyApproveView(SessionViewMixin, View):
    """Alle ausstehenden Posten des Monats genehmigen (Vier-Augen-Prinzip)."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        period = _parse_period(request)
        if period is None:
            return _invalid_period(self, request)
        pending = _period_allowances(self, period).filter(status="pending")
        result = allowance_service.approve_monthly_allowances(
            pending,
            self.session_user,
            four_eyes=four_eyes_service.required(self.session_tenant, four_eyes_service.PROCESS_ALLOWANCE),
        )
        if result.get("blocked_four_eyes"):
            messages.warning(
                request,
                f"{result['blocked_four_eyes']} Posten nicht genehmigt: Wer den Monatslauf erzeugt hat, "
                "darf ihn nicht selbst genehmigen (Vier-Augen-Prinzip).",
            )
        audit.log_event(
            "approve",
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"monat": period.strftime("%m/%Y"), "genehmigt": result["approved"]},
        )
        messages.success(request, f"{result['approved']} Posten für {period:%m/%Y} genehmigt.")
        return redirect(_period_url(self, period))


class MonthlyCancelView(SessionViewMixin, View):
    """
    Einzelnen Posten stornieren, z. B. bei Verzicht oder nachträglich beendeter Funktion.

    Nur offene (ausstehend/genehmigt) und noch nicht exportierte Posten; ausgezahlte sind überwiesen. Ein
    stornierter Posten bleibt bestehen (Protokoll „Entschädigung storniert“), der Monatslauf legt ihn nicht neu an.
    """

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        period = _parse_period(request)
        allowance = None
        try:
            allowance = (
                SessionMonthlyAllowance.objects.filter(tenant=self.session_tenant, pk=request.POST.get("allowance_id"))
                .select_related("person", "rate")
                .first()
            )
        except (ValueError, DjangoValidationError):
            allowance = None
        if allowance is None:
            messages.error(request, "Posten nicht gefunden.")
        elif allowance_service.cancel_allowances([allowance], note=allowance_service.MANUAL_CANCEL_NOTE):
            messages.success(
                request,
                f"Posten „{allowance.rate.name}“ für {allowance.person.display_name} ({allowance.period:%m/%Y}) "
                "storniert.",
            )
        else:
            messages.error(
                request,
                "Nur ausstehende oder genehmigte Posten, die noch nicht exportiert sind, lassen sich stornieren.",
            )
        return redirect(_period_url(self, period))


class MonthlyCsvExportView(SessionViewMixin, View):
    """CSV-Export der Monats-Pauschalen (auditiert, enthält Bankdaten)."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        period = _parse_period(request)
        if period is None:
            return _invalid_period(self, request)
        allowances = list(_period_allowances(self, period).exclude(status="cancelled"))
        if not allowances:
            messages.warning(request, "Keine Posten im Monat — nichts zu exportieren.")
            return redirect(_period_url(self, period))

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
    """
    SEPA-Export der GENEHMIGTEN Monats-Pauschalen; markiert als ausgezahlt.

    Atomar und unter Sperre, Referenz aus dem gemeinsamen Zähler mit dem Sitzungsgeld, Verwendungszweck
    „Monatspauschale“ (``allowance_service.export_sepa``, Issue #428).
    """

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        period = _parse_period(request)
        if period is None:
            return _invalid_period(self, request)
        back = redirect(_period_url(self, period))
        debtor = _debtor_settings(self.session_tenant)
        problem = debtor_problem(debtor)
        if problem:
            messages.error(request, problem)
            return back

        selection = _period_allowances(self, period)
        result = allowance_service.export_sepa(
            self.session_tenant,
            selection,
            kind=allowance_service.KIND_MONTHLY,
            debtor_name=debtor.get("debtor_name") or self.session_tenant.name,
            debtor_iban=debtor["debtor_iban"],
            debtor_bic=debtor.get("debtor_bic", ""),
        )
        if not result.reference and not result.skipped:
            warn_nothing_to_export(request, selection, "Keine genehmigten Posten im Monat — nichts zu exportieren.")
            return back
        if not result.reference:
            messages.error(
                request,
                "SEPA-Export nicht möglich — für keine der Personen ist eine IBAN hinterlegt: "
                + ", ".join(result.skipped),
            )
            return back
        reference, txn_count, total, skipped = result.reference, result.transaction_count, result.total, result.skipped

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
        response = HttpResponse(result.xml, content_type="application/xml")
        response["Content-Disposition"] = f'attachment; filename="pauschalen-{period:%Y-%m}-{reference}.xml"'
        return response
