# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Endgeräte für die digitale Ratsarbeit — UI.

- Gerätebestand (iPads etc.): anlegen, ausgeben, zurücknehmen, Defekt,
  Ausmusterung — mit Historie und Übergabeprotokoll-PDF
- Einmalige Endgeräte-Zuschüsse: erfassen, genehmigen, auszahlen, CSV

Alle Views erfordern die Berechtigung ``manage_devices``.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.common.pdf import html_to_pdf

from .. import audit
from ..models import (
    SessionDevice,
    SessionDeviceGrant,
    SessionDeviceLog,
    SessionPerson,
)
from ..permissions import SessionViewMixin

logger = logging.getLogger(__name__)

_BOM = "﻿"


def _get_person(view, raw_id):
    if not raw_id:
        return None
    try:
        return SessionPerson.objects.filter(tenant=view.session_tenant, pk=raw_id).first()
    except (ValueError, DjangoValidationError):
        return None


class DeviceListView(SessionViewMixin, TemplateView):
    """Übersicht: Gerätebestand und Zuschüsse."""

    template_name = "session/devices/index.html"
    permission_required = "manage_devices"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        devices = list(
            SessionDevice.objects.filter(tenant=self.session_tenant)
            .select_related("issued_to")
            .prefetch_related("logs__person", "logs__created_by__user")
        )
        grants = list(
            SessionDeviceGrant.objects.filter(tenant=self.session_tenant).select_related("person", "approved_by__user")
        )
        context.update(
            {
                "devices": devices,
                "devices_issued": sum(1 for d in devices if d.status == "issued"),
                "devices_stock": sum(1 for d in devices if d.status == "in_stock"),
                "grants": grants,
                "grants_sum": sum(g.amount for g in grants if g.status != "cancelled"),
                "persons": SessionPerson.objects.filter(tenant=self.session_tenant, is_active=True).order_by(
                    "family_name", "given_name"
                ),
            }
        )
        return context


class DeviceSaveView(SessionViewMixin, View):
    """Endgerät anlegen."""

    permission_required = "manage_devices"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        label = request.POST.get("label", "").strip()[:200]
        if not label:
            messages.error(request, "Bitte eine Gerätebezeichnung angeben.")
            return redirect("session:devices", tenant_slug=tenant_slug)

        device = SessionDevice.objects.create(
            tenant=self.session_tenant,
            label=label,
            serial_number=request.POST.get("serial_number", "").strip()[:100],
            inventory_number=request.POST.get("inventory_number", "").strip()[:100],
            accessories=request.POST.get("accessories", "").strip()[:300],
        )
        SessionDeviceLog.objects.create(device=device, action="created", created_by=self.session_user)
        audit.log_event(
            "create",
            device,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"geraet": str(device)},
        )
        messages.success(request, f"Endgerät „{device}“ aufgenommen.")
        return redirect("session:devices", tenant_slug=tenant_slug)


class DeviceActionView(SessionViewMixin, View):
    """Statuswechsel eines Geräts: ausgeben, zurücknehmen, Defekt, Ausmusterung."""

    permission_required = "manage_devices"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, device_id, action):
        device = get_object_or_404(SessionDevice.objects.filter(tenant=self.session_tenant), pk=device_id)
        note = request.POST.get("note", "").strip()

        if action == "issue":
            person = _get_person(self, request.POST.get("person"))
            if device.status != "in_stock" or person is None:
                messages.error(request, "Ausgabe nur für Geräte im Bestand und mit gewählter Person.")
                return redirect("session:devices", tenant_slug=tenant_slug)
            device.status = "issued"
            device.issued_to = person
            device.issued_at = timezone.now()
            log_action = "issued"
            messages.success(
                request, f"{device.label} an {person.display_name} ausgegeben — Übergabeprotokoll als PDF verfügbar."
            )
        elif action == "return":
            if device.status != "issued":
                messages.error(request, "Nur ausgegebene Geräte können zurückgenommen werden.")
                return redirect("session:devices", tenant_slug=tenant_slug)
            person = device.issued_to
            device.status = "in_stock"
            device.issued_to = None
            device.issued_at = None
            log_action = "returned"
            messages.success(request, f"{device.label} zurückgenommen.")
        elif action == "defect":
            person = device.issued_to
            device.status = "defect"
            device.issued_to = None
            device.issued_at = None
            log_action = "defect"
            messages.success(request, f"{device.label} als defekt erfasst.")
        elif action == "retire":
            person = device.issued_to
            device.status = "retired"
            device.issued_to = None
            device.issued_at = None
            log_action = "retired"
            messages.success(request, f"{device.label} ausgemustert.")
        else:
            messages.error(request, "Unbekannte Aktion.")
            return redirect("session:devices", tenant_slug=tenant_slug)

        device.save()
        SessionDeviceLog.objects.create(
            device=device, action=log_action, person=person, note=note, created_by=self.session_user
        )
        audit.log_event(
            "update",
            device,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "geraet": str(device),
                "aktion": log_action,
                "person": person.display_name if person else "",
                "vermerk": note[:300],
            },
        )
        return redirect("session:devices", tenant_slug=tenant_slug)


class DeviceHandoverPdfView(SessionViewMixin, View):
    """Übergabe-/Rückgabeprotokoll als PDF (mit Unterschriftenzeilen)."""

    permission_required = "manage_devices"
    http_method_names = ["get"]

    def get(self, request, tenant_slug, device_id):
        device = get_object_or_404(
            SessionDevice.objects.filter(tenant=self.session_tenant).select_related("issued_to"),
            pk=device_id,
        )
        tenant = self.session_tenant
        context = {
            "tenant": tenant,
            "device": device,
            "person": device.issued_to,
            "variant": "Übergabeprotokoll" if device.status == "issued" else "Geräteprotokoll",
            "generated_at": timezone.localtime(),
            "address_lines": [line for line in (tenant.address or "").splitlines() if line.strip()],
        }
        pdf_bytes = html_to_pdf(render_to_string("session/pdf/device_handover.html", context))
        audit.log_event(
            "download",
            device,
            tenant=tenant,
            user=self.session_user,
            request=request,
            changes={"export": "uebergabeprotokoll", "geraet": str(device)},
        )
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = 'attachment; filename="uebergabeprotokoll.pdf"'
        return response


class DeviceGrantSaveView(SessionViewMixin, View):
    """Endgeräte-Zuschuss erfassen."""

    permission_required = "manage_devices"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        person = _get_person(self, request.POST.get("person"))
        try:
            amount = Decimal(request.POST.get("amount", "").replace(",", "."))
            assert Decimal("0") < amount <= Decimal("99999")
        except (InvalidOperation, AssertionError, AttributeError):
            amount = None
        if person is None or amount is None:
            messages.error(request, "Bitte Person und gültigen Betrag angeben.")
            return redirect("session:devices", tenant_slug=tenant_slug)

        if (
            SessionDeviceGrant.objects.filter(tenant=self.session_tenant, person=person)
            .exclude(status="cancelled")
            .exists()
        ):
            messages.warning(
                request,
                f"Hinweis: Für {person.display_name} existiert bereits ein Zuschuss — "
                "der Zuschuss ist üblicherweise einmalig je Wahlperiode.",
            )

        grant = SessionDeviceGrant.objects.create(
            tenant=self.session_tenant,
            person=person,
            amount=amount,
            note=request.POST.get("note", "").strip(),
            created_by=self.session_user,
        )
        audit.log_event(
            "create",
            grant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"person": person.display_name, "betrag": str(amount)},
        )
        messages.success(request, f"Zuschuss über {amount} € für {person.display_name} erfasst.")
        return redirect("session:devices", tenant_slug=tenant_slug)


class DeviceGrantActionView(SessionViewMixin, View):
    """Zuschuss genehmigen, als ausgezahlt markieren oder stornieren."""

    permission_required = "manage_devices"
    http_method_names = ["post"]

    TRANSITIONS = {
        "approve": ("pending", "approved"),
        "pay": ("approved", "paid"),
        "cancel": (None, "cancelled"),
    }

    def post(self, request, tenant_slug, grant_id, action):
        grant = get_object_or_404(
            SessionDeviceGrant.objects.filter(tenant=self.session_tenant).select_related("person"),
            pk=grant_id,
        )
        transition = self.TRANSITIONS.get(action)
        if transition is None:
            messages.error(request, "Unbekannte Aktion.")
            return redirect("session:devices", tenant_slug=tenant_slug)
        required_status, new_status = transition
        if required_status and grant.status != required_status:
            messages.error(request, f"Aktion nicht möglich (Status: {grant.get_status_display()}).")
            return redirect("session:devices", tenant_slug=tenant_slug)
        if grant.status == "paid" and action == "cancel":
            messages.error(request, "Ausgezahlte Zuschüsse können nicht storniert werden.")
            return redirect("session:devices", tenant_slug=tenant_slug)

        grant.status = new_status
        if action == "approve":
            grant.approved_by = self.session_user
            grant.approved_at = timezone.now()
        elif action == "pay":
            grant.paid_at = timezone.now()
        grant.save()
        audit.log_event(
            "approve" if action == "approve" else "update",
            grant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"person": grant.person.display_name, "status": grant.get_status_display()},
        )
        messages.success(request, f"Zuschuss für {grant.person.display_name}: {grant.get_status_display()}.")
        return redirect("session:devices", tenant_slug=tenant_slug)


class DeviceGrantCsvExportView(SessionViewMixin, View):
    """CSV der Zuschüsse (mit Bankdaten) fürs Finanzverfahren."""

    permission_required = "manage_devices"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        import csv
        import io

        grants = list(
            SessionDeviceGrant.objects.filter(tenant=self.session_tenant)
            .exclude(status="cancelled")
            .select_related("person")
            .order_by("person__family_name")
        )
        if not grants:
            messages.warning(request, "Keine Zuschüsse vorhanden.")
            return redirect("session:devices", tenant_slug=tenant_slug)

        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        writer.writerow(["Name", "Betrag", "Status", "Vermerk", "Kontoinhaber", "IBAN", "BIC"])
        for grant in grants:
            person = grant.person
            writer.writerow(
                [
                    person.display_name,
                    f"{grant.amount:.2f}".replace(".", ","),
                    grant.get_status_display(),
                    grant.note[:200],
                    person.get_bank_account_holder_decrypted() or "",
                    person.get_bank_iban_decrypted() or "",
                    person.get_bank_bic_decrypted() or "",
                ]
            )
        audit.log_event(
            "download",
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"export": "endgeraete_zuschuesse_csv", "anzahl": len(grants)},
        )
        response = HttpResponse(_BOM + buffer.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="endgeraete-zuschuesse.csv"'
        return response
