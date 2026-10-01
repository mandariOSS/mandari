# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsgeld-Abrechnung — UI (Issue #38).

Alle Views erfordern die Berechtigung ``manage_allowances`` (Bankdaten!).
Ablauf: Sätze pflegen -> Abrechnungslauf für einen Zeitraum -> Genehmigung
(Vier-Augen-Prinzip) -> Export (CSV/SEPA) -> Jahresübersicht.
Jeder Export wird auditiert.
"""

import logging
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.common.params import date_param, uuid_param

from .. import audit
from ..models import (
    SessionAllowance,
    SessionAllowanceRate,
    SessionOrganization,
    SessionPerson,
)
from ..permissions import SessionViewMixin
from ..services import allowance_service, four_eyes_service

logger = logging.getLogger(__name__)

# UTF-8-BOM für Excel-kompatible CSV-Dateien
_BOM = "﻿"


def _allowance_queryset(view, period_start, period_end, organization_id="", status=""):
    """Positionen des Mandanten im Zeitraum (tenant-sicher, mit Filtern; ungültige Gremiumskennung: keine)."""
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
        organization_pk = uuid_param(organization_id)
        if organization_pk is None:
            return qs.none()
        qs = qs.filter(attendance__meeting__organization_id=organization_pk)
    if status:
        qs = qs.filter(status=status)
    return qs


_INVALID_ORGANIZATION = "Unbekanntes Gremium – bitte ein Gremium aus der Liste wählen."


def _organization_param(view, raw):
    """
    Gremium aus einem Formularwert: ``(gültig, Gremium)``. Leer heißt alle Gremien, also ``(True, None)``.

    Eine Kennung, die keine UUID ist, ist ungültig (``(False, None)``); die UUID eines fremden oder
    unbekannten Gremiums ergibt wie bisher 404.
    """
    if not raw:
        return True, None
    organization_pk = uuid_param(raw)
    if organization_pk is None:
        return False, None
    return True, get_object_or_404(SessionOrganization, pk=organization_pk, tenant=view.session_tenant)


def _list_url(view, period_start, period_end, organization=None) -> str:
    """Rücksprung auf die Übersicht mit Zeitraum (und Gremium)."""
    query = {"from": period_start.isoformat(), "to": period_end.isoformat()}
    if organization is not None:
        query["organization"] = str(organization.pk)
    return f"{reverse('session:allowances', kwargs={'tenant_slug': view.session_tenant.slug})}?{urlencode(query)}"


def _report_corrections(request, cancelled: int, reactivated: int = 0) -> None:
    """Hinweis auf Positionen, die der Abgleich mit Anwesenheit und Absage storniert oder wieder aufgenommen hat."""
    if cancelled:
        messages.warning(
            request,
            f"{cancelled} Position(en) storniert: Die Sitzung wurde abgesagt oder die Anwesenheit ist nicht mehr "
            "anrechenbar.",
        )
    if reactivated:
        messages.info(
            request,
            f"{reactivated} stornierte Position(en) wieder aufgenommen (Anwesenheit wieder anrechenbar) – "
            "bitte erneut genehmigen.",
        )


def _debtor_settings(tenant) -> dict:
    """SEPA-Auftraggeberkonto der Kommune aus den Mandanten-Einstellungen."""
    return (tenant.settings or {}).get("allowances", {})


def debtor_problem(debtor: dict) -> str:
    """Warum mit diesem Auftraggeberkonto keine SEPA-Datei entstehen kann – leer, wenn es passt."""
    if not debtor.get("debtor_iban"):
        return (
            "SEPA-Export nicht möglich — bitte zuerst das Auftraggeberkonto der Kommune bei den Sitzungsgeldern "
            "hinterlegen."
        )
    if not allowance_service.valid_iban(debtor["debtor_iban"]) or (
        debtor.get("debtor_bic") and not allowance_service.valid_bic(debtor["debtor_bic"])
    ):
        return (
            "SEPA-Export nicht möglich — IBAN oder BIC des Auftraggeberkontos sind ungültig. "
            "Bitte das Auftraggeberkonto bei den Sitzungsgeldern prüfen."
        )
    return ""


def _debtor_from_post(data) -> tuple[dict, str]:
    """Auftraggeberkonto aus dem Formular: ``(Werte, Fehlermeldung)``; IBAN mit Prüfziffer, BIC nach ISO 9362."""
    debtor = {
        "debtor_name": (data.get("debtor_name") or "").strip()[:70],
        "debtor_iban": allowance_service.normalize_account_code(data.get("debtor_iban"))[:34],
        "debtor_bic": allowance_service.normalize_account_code(data.get("debtor_bic"))[:11],
    }
    if debtor["debtor_iban"] and not allowance_service.valid_iban(debtor["debtor_iban"]):
        return debtor, "Die IBAN des Auftraggeberkontos ist ungültig (Format oder Prüfziffer) – nichts gespeichert."
    if debtor["debtor_bic"] and not allowance_service.valid_bic(debtor["debtor_bic"]):
        return debtor, "Die BIC des Auftraggeberkontos ist ungültig (8 oder 11 Zeichen) – nichts gespeichert."
    return debtor, ""


def warn_nothing_to_export(request, selection, text: str) -> None:
    """
    Hinweis, wenn der SEPA-Export nichts Neues findet. Wurde die Auswahl gerade exportiert (etwa bei
    einem Doppelklick), nennt er die Referenz der erzeugten Datei (Issue #428).
    """
    reference = allowance_service.last_export_reference(selection)
    if reference:
        text += f" Zuletzt exportiert mit Referenz {reference}."
    messages.warning(request, text)


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
        if organization_id and uuid_param(organization_id) is None:
            messages.error(self.request, f"{_INVALID_ORGANIZATION} Es werden alle Gremien angezeigt.")
            organization_id = ""
        status = self.request.GET.get("status", "")
        allowances = list(_allowance_queryset(self, period_start, period_end, organization_id, status))
        for allowance in allowances:
            # Grundlage entfallen (Sitzung abgesagt, Anwesenheit korrigiert): Genehmigung und Export stornieren
            allowance.basis_missing = allowance.status in allowance_service.OPEN_STATUSES and not (
                allowance_service.has_basis(allowance)
            )

        context["period_start"] = period_start
        context["period_end"] = period_end
        context["selected_organization"] = organization_id
        context["selected_status"] = status
        context["allowances"] = allowances
        # Stornierte Positionen zählen nicht zur Summe
        context["total_amount"] = sum((a.amount for a in allowances if a.status != "cancelled"), Decimal("0.00"))
        context["pending_count"] = sum(1 for a in allowances if a.status == "pending")
        context["approved_count"] = sum(1 for a in allowances if a.status == "approved")
        context["paid_count"] = sum(1 for a in allowances if a.status == "paid")

        context["organizations"] = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        ).order_by("name")
        context["rates"] = SessionAllowanceRate.objects.filter(organization__tenant=self.session_tenant).select_related(
            "organization"
        )
        context["rate_roles"] = SessionAllowanceRate._meta.get_field("role").choices
        context["allowance_statuses"] = SessionAllowance._meta.get_field("status").choices
        context["debtor"] = _debtor_settings(self.session_tenant)
        return context


class AllowanceRateSaveView(SessionViewMixin, View):
    """Entschädigungssatz je Gremium/Funktion speichern."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        valid, organization = _organization_param(self, request.POST.get("organization"))
        if organization is None:
            messages.error(request, _INVALID_ORGANIZATION if not valid else "Bitte ein Gremium wählen.")
            return redirect("session:allowances", tenant_slug=tenant_slug)
        role = request.POST.get("role", "member")
        valid_roles = {c[0] for c in SessionAllowanceRate._meta.get_field("role").choices}
        if role not in valid_roles:
            role = "member"
        amount = allowance_service.parse_amount(request.POST.get("amount", ""))
        if amount is None:
            messages.error(
                request,
                "Ungültiger Betrag für den Entschädigungssatz: bitte einen Betrag von 0,00 bis 999.999,99 € "
                "mit höchstens zwei Nachkommastellen angeben.",
            )
            return redirect("session:allowances", tenant_slug=tenant_slug)

        rate, _created = SessionAllowanceRate.objects.update_or_create(
            organization=organization, role=role, defaults={"amount": amount}
        )
        messages.success(
            request,
            f"Satz für {organization.name} / {rate.get_role_display()}: "
            f"{allowance_service.csv_amount(amount)} EUR gespeichert.",
        )
        return redirect("session:allowances", tenant_slug=tenant_slug)


class AllowanceRateDeleteView(SessionViewMixin, View):
    """Entschädigungssatz löschen (Fallback: Gremiums-Standardsatz)."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, rate_id):
        rate = get_object_or_404(SessionAllowanceRate, pk=rate_id, organization__tenant=self.session_tenant)
        rate.delete()
        messages.success(request, "Entschädigungssatz entfernt.")
        return redirect("session:allowances", tenant_slug=tenant_slug)


#: Felder des Auftraggeberkontos und ihre Namen im Protokoll
_DEBTOR_AUDIT_FIELDS = {
    "debtor_name": "auftraggeberkonto_inhaber",
    "debtor_iban": "auftraggeberkonto_iban",
    "debtor_bic": "auftraggeberkonto_bic",
}


def _masked_iban(value: str) -> str:
    """IBAN im Protokoll gekürzt: Länderkennung mit Prüfziffern und die letzten vier Stellen."""
    if not value:
        return ""
    return f"{value[:4]} … {value[-4:]}" if len(value) > 8 else "…"


def _log_debtor_change(view, request, before: dict, after: dict) -> None:
    """Änderung des Auftraggeberkontos im Protokoll des Mandanten vermerken (nur geänderte Felder)."""
    changes = {}
    for key, label in _DEBTOR_AUDIT_FIELDS.items():
        old, new = before.get(key) or "", after.get(key) or ""
        if old == new:
            continue
        if key == "debtor_iban":
            old, new = _masked_iban(old), _masked_iban(new)
            if old == new:
                # Geändert hat sich nur der gekürzte Mittelteil: im Protokoll trotzdem erkennbar machen
                new = f"{new} (geändert)"
        changes[label] = {"alt": old, "neu": new}
    if changes:
        audit.log_event(
            "update",
            view.session_tenant,
            tenant=view.session_tenant,
            user=view.session_user,
            request=request,
            changes=changes,
        )


class AllowanceDebtorSaveView(SessionViewMixin, View):
    """SEPA-Auftraggeberkonto der Kommune speichern (Mandanten-Einstellungen)."""

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        tenant = self.session_tenant
        debtor, problem = _debtor_from_post(request.POST)
        if problem:
            messages.error(request, problem)
            return redirect("session:allowances", tenant_slug=tenant_slug)
        settings = tenant.settings or {}
        before = dict(settings.get("allowances", {}))
        settings.setdefault("allowances", {})
        settings["allowances"].update(debtor)
        tenant.settings = settings
        tenant.save(update_fields=["settings", "updated_at"])
        _log_debtor_change(self, request, before, settings["allowances"])
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

        valid, organization = _organization_param(self, request.POST.get("organization"))
        if not valid:
            messages.error(request, _INVALID_ORGANIZATION)
            return redirect("session:allowances", tenant_slug=tenant_slug)

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
                    "wieder_aufgenommen": stats["reactivated"],
                    "storniert": stats["cancelled"],
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
        _report_corrections(request, stats["cancelled"], stats["reactivated"])
        return redirect(_list_url(self, period_start, period_end, organization))


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

        valid, organization = _organization_param(self, request.POST.get("organization"))
        if not valid:
            messages.error(request, _INVALID_ORGANIZATION)
            return redirect("session:allowances", tenant_slug=tenant_slug)
        # Korrekturen seit dem Lauf zuerst nachziehen: Ohne Grundlage wird nichts genehmigt
        _report_corrections(
            request,
            allowance_service.cancel_obsolete_allowances(self.session_tenant, period_start, period_end, organization),
        )
        allowances = _allowance_queryset(
            self, period_start, period_end, str(organization.pk) if organization else "", status="pending"
        )
        stats = allowance_service.approve_allowances(
            allowances,
            self.session_user,
            four_eyes=four_eyes_service.required(self.session_tenant, four_eyes_service.PROCESS_ALLOWANCE),
        )

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
        return redirect(_list_url(self, period_start, period_end, organization))


class AllowanceCancelView(SessionViewMixin, View):
    """
    Position von Hand stornieren, z. B. bei Verzicht oder Doppelerfassung.

    Nur offene (ausstehend/genehmigt) und noch nicht exportierte Positionen; ausgezahlte sind überwiesen und
    werden außerhalb von mandari korrigiert. Eine von Hand stornierte Position lebt im nächsten Abrechnungslauf
    nicht wieder auf. Protokoll: „Entschädigung storniert“ über das Audit-Signal.
    """

    permission_required = "manage_allowances"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, allowance_id):
        allowance = get_object_or_404(
            SessionAllowance.objects.select_related("attendance__person", "attendance__meeting"),
            pk=allowance_id,
            attendance__meeting__tenant=self.session_tenant,
        )
        if allowance_service.cancel_allowances([allowance], note=allowance_service.MANUAL_CANCEL_NOTE):
            messages.success(
                request,
                f"Position für {allowance.attendance.person.display_name} "
                f"({timezone.localtime(allowance.attendance.meeting.start):%d.%m.%Y}) storniert.",
            )
        else:
            messages.error(
                request,
                "Nur ausstehende oder genehmigte Positionen, die noch nicht exportiert sind, lassen sich stornieren.",
            )
        period_start = date_param(request.POST.get("from"))
        period_end = date_param(request.POST.get("to"))
        if period_start and period_end:
            return redirect(_list_url(self, period_start, period_end))
        return redirect("session:allowances", tenant_slug=tenant_slug)


class AllowanceCsvExportView(SessionViewMixin, View):
    """Generischer CSV-Export fürs Finanzverfahren (auditiert)."""

    permission_required = "manage_allowances"

    def get(self, request, tenant_slug):
        period_start, period_end = allowance_service.parse_period(
            request.GET.get("from", ""), request.GET.get("to", "")
        )
        if period_start is None:
            return HttpResponse(status=400)

        status = request.GET.get("status", "")
        selection = _allowance_queryset(self, period_start, period_end, request.GET.get("organization", ""), status)
        if not status:
            # Stornierte Positionen gehören nicht in die Datei fürs Finanzverfahren (nur auf ausdrücklichen Filter)
            selection = selection.exclude(status="cancelled")
        # Offene Positionen ohne Grundlage (Sitzung abgesagt, Anwesenheit korrigiert) nicht ausgeben: Wer über das
        # Finanzverfahren statt per SEPA auszahlt, überwiese sie sonst. Gleiche Regel wie Lauf, Genehmigung und
        # SEPA-Export; storniert wird auf GET nicht, das erledigt der nächste Lauf bzw. die Genehmigung.
        allowances = [
            allowance
            for allowance in selection
            if allowance.status not in allowance_service.OPEN_STATUSES or allowance_service.has_basis(allowance)
        ]
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
    fortlaufende Export-Referenz fürs Finanzverfahren. Atomar und unter Sperre
    (``allowance_service.export_sepa``): Ein doppelt ausgelöster Export gibt
    keine Position zweimal aus (Issue #428).
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

        valid, organization = _organization_param(self, request.POST.get("organization"))
        if not valid:
            messages.error(request, _INVALID_ORGANIZATION)
            return redirect("session:allowances", tenant_slug=tenant_slug)
        back = redirect(_list_url(self, period_start, period_end, organization))

        debtor = _debtor_settings(self.session_tenant)
        problem = debtor_problem(debtor)
        if problem:
            messages.error(request, problem)
            return back

        # Korrekturen seit der Genehmigung nachziehen: Ohne Grundlage keine Überweisung
        _report_corrections(
            request,
            allowance_service.cancel_obsolete_allowances(self.session_tenant, period_start, period_end, organization),
        )
        selection = _allowance_queryset(self, period_start, period_end, str(organization.pk) if organization else "")
        # Nur Positionen von Personen MIT IBAN werden als exportiert/ausgezahlt markiert
        result = allowance_service.export_sepa(
            self.session_tenant,
            selection,
            kind=allowance_service.KIND_SESSION,
            debtor_name=debtor.get("debtor_name") or self.session_tenant.name,
            debtor_iban=debtor["debtor_iban"],
            debtor_bic=debtor.get("debtor_bic", ""),
        )
        if not result.reference and not result.skipped:
            warn_nothing_to_export(
                request, selection, "Keine genehmigten Positionen im Zeitraum — nichts zu exportieren."
            )
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

        response = HttpResponse(result.xml, content_type="application/xml")
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
        allowances = list(_allowance_queryset(self, period_start, period_end).filter(attendance__person=person))
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
        current_year = timezone.localdate().year
        raw_year = request.GET.get("year")
        year = allowance_service.parse_year(raw_year) if raw_year else current_year
        if year is None:
            if request.GET.get("format") == "csv":
                return HttpResponse(status=400)
            messages.error(
                request,
                f"Ungültiges Jahr – bitte ein Jahr von {allowance_service.YEAR_MIN} bis "
                f"{allowance_service.YEAR_MAX} angeben. Angezeigt wird {current_year}.",
            )
            year = current_year
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
        context["year_min"] = allowance_service.YEAR_MIN
        context["year_max"] = allowance_service.YEAR_MAX
        # Personenseite verlangt das Sitzungs-Sichtrecht – ohne es kein Link (sonst 403)
        context["can_view_persons"] = self.has_permission("view_meetings")
        context["rows"] = allowance_service.year_summary(self.session_tenant, self.year)
        context["totals"] = allowance_service.year_summary_totals(context["rows"])
        return context
