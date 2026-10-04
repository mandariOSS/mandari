# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussregister, Beschlussauszüge (Issue #32) und Beschlusskontrolle (Issue #37).

Views für:
- Beschlussregister je Mandant (filterbar nach Gremium, Jahr, Ergebnis, Umsetzungsstand)
- Sammel-Ausfertigung: Nummernvergabe + Sammel-PDF je Sitzung
- Beschlussauszug-PDF je TOP
- Versand-/Übergabevermerk mit Audit-Eintrag
- Beschlusskontrolle: Umsetzungsstand, Zuständigkeit, Frist mit Audit-Eintrag
- CSV-Export des Registers inkl. Umsetzungsstand
"""

from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.common import csv_safety
from apps.common.params import uuid_param

from .. import audit, hub_events
from ..models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionResolutionForwarding
from ..permissions import SessionViewMixin
from ..services import four_eyes_service, resolution_service
from .nexturl import safe_next_url


def _get_meeting(view, meeting_id):
    qs = SessionMeeting.objects.filter(tenant=view.session_tenant).select_related("organization", "tenant")
    if not view.has_permission("view_non_public_meetings"):
        qs = qs.filter(is_public=True)
    return get_object_or_404(qs, pk=meeting_id)


def _get_item(view, item_id):
    qs = SessionAgendaItem.objects.filter(meeting__tenant=view.session_tenant).select_related(
        "meeting__organization", "meeting__tenant"
    )
    if not view.has_permission("view_non_public_meetings"):
        qs = qs.filter(is_public=True, meeting__is_public=True)
    return get_object_or_404(qs, pk=item_id)


#: Filter des Beschlussregisters – der CSV-Export übernimmt alle
REGISTER_FILTERS = ("organization", "year", "result", "status", "overdue")
IMPLEMENTATION_VALUES = frozenset(value for value, _ in SessionAgendaItem.IMPLEMENTATION_CHOICES)


def _decided_items(view):
    """Gefasste Beschlüsse, die diese Person sehen darf (ungefiltert)."""
    include_np = view.has_permission("view_non_public_meetings")
    return resolution_service.decided_items(view.session_tenant, include_non_public=include_np)


def _overdue_q():
    return Q(vote_result="approved", implementation_deadline__lt=timezone.localdate()) & ~Q(
        implementation_status="done"
    )


def _jahr(raw):
    """Jahr aus dem Filter (1–9999); sonst None – ``year=0`` oder Riesenzahlen sprengen den Datumsbereich."""
    if raw and raw.isascii() and raw.isdigit() and len(raw) <= 4 and int(raw) >= 1:
        return int(raw)
    return None


def _filter_basis(qs, params):
    """Gremium, Jahr und Ergebnis – darüber zählt auch die Ampel der Beschlusskontrolle."""
    org_id = params.get("organization")
    if org_id:
        org_uuid = uuid_param(org_id)  # ungültig: kein Treffer statt Serverfehler
        qs = qs.filter(meeting__organization_id=org_uuid) if org_uuid else qs.none()
    year = _jahr(params.get("year"))
    if year is not None:
        # Ortszeit: Eine Sitzung am 1. Januar um 0:30 Uhr gehört zum neuen Jahr
        qs = qs.filter(meeting__start__year=year)
    result = params.get("result")
    if result in resolution_service.DECIDED_RESULTS:
        qs = qs.filter(vote_result=result)
    return qs


def _filter_umsetzung(qs, params):
    """Umsetzungsstand und „nur überfällige“ (Beschlusskontrolle, Issue #37)."""
    impl_status = params.get("status")
    if impl_status in IMPLEMENTATION_VALUES:
        qs = qs.filter(vote_result="approved", implementation_status=impl_status)
    if params.get("overdue") == "1":
        qs = qs.filter(_overdue_q())
    return qs


def _register_years(qs):
    """Jahre mit Beschlüssen (Ortszeit, neueste zuerst) – unabhängig von den gesetzten Filtern."""
    return [moment.year for moment in qs.datetimes("meeting__start", "year", order="DESC")]


class ResolutionRegisterView(SessionViewMixin, TemplateView):
    """Beschlussregister: alle gefassten Beschlüsse mit Nummer und Filtern."""

    template_name = "session/resolutions/register.html"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET
        alle = _decided_items(self)
        qs = _filter_basis(alle, params)

        today = timezone.localdate()

        # Beschlusskontrolle: Ampel-Zahlen über den Gremium-/Jahresfilter
        # hinweg (nur angenommene Beschlüsse haben einen Umsetzungsstand).
        approved = qs.filter(vote_result="approved")
        tracking_stats = {
            "open": approved.filter(implementation_status="open").count(),
            "in_progress": approved.filter(implementation_status="in_progress").count(),
            "done": approved.filter(implementation_status="done").count(),
            "deferred": approved.filter(implementation_status="deferred").count(),
            "overdue": qs.filter(_overdue_q()).count(),
        }

        qs = _filter_umsetzung(qs, params)
        impl_status = params.get("status")
        overdue = params.get("overdue") == "1"
        org_id = params.get("organization")
        year = params.get("year")
        result = params.get("result")

        items = list(qs.prefetch_related("forwardings")[:300])

        context.update(
            {
                "items": items,
                "organizations": SessionOrganization.objects.filter(
                    tenant=self.session_tenant, is_active=True
                ).order_by("name"),
                # Alle Jahre, nicht nur die gefilterten – sonst ließe sich das Jahr nicht mehr wechseln
                "years": _register_years(alle),
                # Der Export liefert, was die Seite zeigt
                "export_query": urlencode({key: params[key] for key in REGISTER_FILTERS if params.get(key)}),
                "result_choices": [
                    (value, label)
                    for value, label in SessionAgendaItem._meta.get_field("vote_result").choices
                    if value in resolution_service.DECIDED_RESULTS
                ],
                "can_manage": self.has_permission("edit_meetings"),
                "filter_organization": org_id or "",
                "filter_year": year or "",
                "filter_result": result or "",
                "filter_status": impl_status or "",
                "filter_overdue": overdue,
                "tracking_stats": tracking_stats,
                "implementation_choices": SessionAgendaItem.IMPLEMENTATION_CHOICES,
                "today": today,
            }
        )
        return context


class ResolutionBatchView(SessionViewMixin, View):
    """
    Sammel-Ausfertigung einer Sitzung: vergibt Beschlussnummern für alle
    gefassten Beschlüsse (Audit über Signale) und meldet das Ergebnis.
    """

    permission_required = "edit_meetings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, meeting_id):
        meeting = _get_meeting(self, meeting_id)
        assigned = resolution_service.ensure_numbers_for_meeting(meeting)
        if assigned:
            messages.success(request, f"Beschlussausfertigung: {assigned} Beschlussnummer(n) vergeben.")
        else:
            messages.info(request, "Alle gefassten Beschlüsse dieser Sitzung haben bereits Beschlussnummern.")
        next_url = safe_next_url(request, self.session_tenant.slug)
        if next_url:
            return redirect(next_url)
        return redirect(
            "session:meeting_detail",
            tenant_slug=self.session_tenant.slug,
            meeting_id=meeting.id,
        )


class ResolutionMeetingPdfView(SessionViewMixin, TemplateView):
    """Sammel-PDF: alle Beschlussauszüge einer Sitzung in einem Lauf."""

    permission_required = "view_meetings"

    def get(self, request, *args, **kwargs):
        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        include_np = self.has_permission("view_non_public_meetings")

        items = list(
            meeting.agenda_items.filter(vote_result__in=resolution_service.DECIDED_RESULTS)
            .exclude(is_withdrawn=True)
            .order_by("order", "number")
        )
        if not include_np:
            items = [i for i in items if i.is_public]
        if not items:
            messages.error(request, "Diese Sitzung enthält keine gefassten Beschlüsse.")
            return redirect(
                "session:meeting_detail",
                tenant_slug=self.session_tenant.slug,
                meeting_id=meeting.id,
            )

        pdf_bytes = resolution_service.build_extract_pdf(
            items, internal=include_np, permissions=self.session_permissions
        )
        # Nichtöffentliche Beschlüsse im Dokument: Abruf protokollieren (Issue #221)
        if not meeting.is_public or any(not i.is_public for i in items):
            audit.log_read(
                request,
                meeting,
                tenant=self.session_tenant,
                user=self.session_user,
                action="download",
                changes={"dokument": "Beschlussauszüge mit nichtöffentlichen Beschlüssen (PDF)"},
            )
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = 'attachment; filename="beschlussauszuege.pdf"'
        return response


class ResolutionExtractPdfView(SessionViewMixin, TemplateView):
    """Beschlussauszug-PDF für einen einzelnen TOP."""

    permission_required = "view_meetings"

    def get(self, request, *args, **kwargs):
        item = _get_item(self, self.kwargs["item_id"])
        if item.vote_result not in resolution_service.DECIDED_RESULTS:
            messages.error(request, "Für diesen TOP liegt noch kein Beschluss vor.")
            return redirect(
                "session:meeting_detail",
                tenant_slug=self.session_tenant.slug,
                meeting_id=item.meeting_id,
            )
        include_np = self.has_permission("view_non_public_meetings")
        pdf_bytes = resolution_service.build_extract_pdf(
            [item], internal=include_np, permissions=self.session_permissions
        )
        # Beschlussauszug eines nichtöffentlichen TOP: Abruf protokollieren (Issue #221)
        if not item.is_public or not item.meeting.is_public:
            audit.log_read(
                request,
                item,
                tenant=self.session_tenant,
                user=self.session_user,
                action="download",
                changes={"dokument": "Beschlussauszug, nichtöffentlicher TOP (PDF)"},
            )
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = 'attachment; filename="beschlussauszug.pdf"'
        return response


class ResolutionForwardingCreateView(SessionViewMixin, View):
    """Versand-/Übergabevermerk für einen Beschlussauszug dokumentieren."""

    permission_required = "edit_meetings"
    http_method_names = ["post"]

    VALID_METHODS = {choice[0] for choice in SessionResolutionForwarding._meta.get_field("method").choices}

    def post(self, request, tenant_slug, item_id):
        item = _get_item(self, item_id)
        recipient = request.POST.get("recipient", "").strip()[:255]
        if not recipient:
            messages.error(request, "Bitte die zuständige Stelle angeben.")
            return redirect("session:resolutions", tenant_slug=self.session_tenant.slug)

        method = request.POST.get("method", "internal")
        if method not in self.VALID_METHODS:
            method = "internal"

        # Vier-Augen-Prinzip (Issue #222): nicht, wer die Niederschrift erstellt bzw. zuletzt bearbeitet hat
        try:
            four_eyes_service.authorize(four_eyes_service.PROCESS_FORWARDING, item, self.session_user)
        except four_eyes_service.ApprovalError as exc:
            messages.error(request, str(exc))
            return redirect("session:resolutions", tenant_slug=self.session_tenant.slug)

        forwarding = SessionResolutionForwarding.objects.create(
            agenda_item=item,
            recipient=recipient,
            method=method,
            note=request.POST.get("note", "").strip(),
            sent_by=self.session_user,
        )

        # Audit: Übergabe nachweisbar protokollieren (wer, wann, an wen)
        audit.log_event(
            "create",
            forwarding,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "beschluss": item.resolution_number or f"TOP {item.number}",
                "empfaenger": recipient,
                "uebergabeweg": forwarding.get_method_display(),
            },
        )
        messages.success(request, f"Übergabe an „{recipient}“ wurde dokumentiert.")
        next_url = safe_next_url(request, self.session_tenant.slug)
        if next_url:
            return redirect(next_url)
        return redirect("session:resolutions", tenant_slug=self.session_tenant.slug)


class ResolutionTrackingUpdateView(SessionViewMixin, View):
    """
    Beschlusskontrolle (Issue #37): Umsetzungsstand, Zuständigkeit, Frist und
    Erledigungsvermerk eines angenommenen Beschlusses pflegen.
    """

    permission_required = "edit_meetings"
    http_method_names = ["post"]

    VALID_STATUSES = {value for value, _ in SessionAgendaItem.IMPLEMENTATION_CHOICES}

    def post(self, request, tenant_slug, item_id):
        item = _get_item(self, item_id)
        if item.vote_result != "approved":
            messages.error(request, "Beschlusskontrolle ist nur für angenommene Beschlüsse möglich.")
            return self._redirect(request)

        status = request.POST.get("status", "")
        if status not in self.VALID_STATUSES:
            messages.error(request, "Ungültiger Umsetzungsstand.")
            return self._redirect(request)

        deadline_raw = request.POST.get("deadline", "").strip()
        deadline = None
        if deadline_raw:
            try:
                deadline = date.fromisoformat(deadline_raw)
            except ValueError:
                messages.error(request, "Ungültiges Datum für die Erledigungsfrist.")
                return self._redirect(request)

        old = {
            "status": item.get_implementation_status_display(),
            "stelle": item.implementation_recipient,
            "frist": item.implementation_deadline.isoformat() if item.implementation_deadline else "",
        }

        item.implementation_status = status
        item.implementation_recipient = request.POST.get("recipient", "").strip()[:255]
        item.implementation_deadline = deadline
        item.implementation_note = request.POST.get("note", "").strip()
        item.implementation_public_note = request.POST.get("public_note", "").strip()[:2000]
        item.implementation_public = request.POST.get("public") != "0"
        item.implementation_updated_at = timezone.now()
        item.implementation_updated_by = self.session_user
        # Drehscheibe (Issue #535): Umsetzungsstand (öffentlich nur, wenn zur Veröffentlichung freigegeben)
        with hub_events.track(self.session_tenant) as tracked:
            tracked.agenda(item.meeting)
            item.save(
                update_fields=[
                    "implementation_status",
                    "implementation_recipient",
                    "implementation_deadline",
                    "implementation_note",
                    "implementation_public_note",
                    "implementation_public",
                    "implementation_updated_at",
                    "implementation_updated_by",
                    "updated_at",
                ]
            )

        audit.log_event(
            "update",
            item,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "beschluss": item.resolution_number or f"TOP {item.number}",
                "beschlusskontrolle_vorher": old,
                "umsetzungsstand": item.get_implementation_status_display(),
                "zustaendige_stelle": item.implementation_recipient,
                "frist": deadline.isoformat() if deadline else "",
            },
        )
        messages.success(
            request,
            f"Beschlusskontrolle aktualisiert: {item.get_implementation_status_display()}.",
        )
        return self._redirect(request)

    def _redirect(self, request):
        next_url = safe_next_url(request, self.session_tenant.slug)
        if next_url:
            return redirect(next_url)
        return redirect("session:resolutions", tenant_slug=self.session_tenant.slug)


class ResolutionCsvExportView(SessionViewMixin, View):
    """CSV-Export des Beschlussregisters inkl. Umsetzungsstand (Issue #37)."""

    permission_required = "view_meetings"
    http_method_names = ["get"]

    def get(self, request, tenant_slug):
        # Dieselben Filter wie das Register, einschließlich Umsetzungsstand und „nur überfällige“
        qs = _filter_umsetzung(_filter_basis(_decided_items(self), request.GET), request.GET)

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="beschlussregister.csv"'
        response.write("﻿")  # BOM für Excel

        writer = csv_safety.writer(response, delimiter=";")
        writer.writerow(
            [
                "Beschluss-Nr.",
                "TOP",
                "Betreff",
                "Gremium",
                "Sitzung",
                "Datum",
                "Ergebnis",
                "Ja",
                "Nein",
                "Enthaltung",
                "Öffentlich",
                "Umsetzungsstand",
                "Zuständige Stelle",
                "Erledigungsfrist",
                "Überfällig",
                "Erledigungsvermerk",
            ]
        )
        for item in qs.select_related("meeting__organization"):
            is_approved = item.vote_result == "approved"
            writer.writerow(
                [
                    item.resolution_number,
                    item.number,
                    item.name,
                    item.meeting.organization.name if item.meeting.organization else "",
                    item.meeting.name,
                    timezone.localtime(item.meeting.start).strftime("%d.%m.%Y") if item.meeting.start else "",
                    item.get_vote_result_display(),
                    item.votes_yes,
                    item.votes_no,
                    item.votes_abstain,
                    "ja" if item.is_public else "nein",
                    item.get_implementation_status_display() if is_approved else "",
                    item.implementation_recipient if is_approved else "",
                    item.implementation_deadline.strftime("%d.%m.%Y")
                    if is_approved and item.implementation_deadline
                    else "",
                    "ja" if is_approved and item.implementation_overdue else "",
                    item.implementation_note if is_approved else "",
                ]
            )

        audit.log_event(
            "download",
            self.session_tenant,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"export": "beschlussregister_csv", "anzahl": qs.count()},
        )
        return response
