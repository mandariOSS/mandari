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

import csv
from datetime import date

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionResolutionForwarding
from ..permissions import SessionViewMixin
from ..services import resolution_service


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


class ResolutionRegisterView(SessionViewMixin, TemplateView):
    """Beschlussregister: alle gefassten Beschlüsse mit Nummer und Filtern."""

    template_name = "session/resolutions/register.html"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        include_np = self.has_permission("view_non_public_meetings")
        qs = resolution_service.decided_items(self.session_tenant, include_non_public=include_np)

        org_id = self.request.GET.get("organization")
        if org_id:
            qs = qs.filter(meeting__organization_id=org_id)
        year = self.request.GET.get("year")
        if year and year.isdigit():
            qs = qs.filter(meeting__start__year=int(year))
        result = self.request.GET.get("result")
        if result in resolution_service.DECIDED_RESULTS:
            qs = qs.filter(vote_result=result)

        today = timezone.localdate()
        overdue_q = Q(
            vote_result="approved",
            implementation_deadline__lt=today,
        ) & ~Q(implementation_status="done")

        # Beschlusskontrolle: Ampel-Zahlen über den Gremium-/Jahresfilter
        # hinweg (nur angenommene Beschlüsse haben einen Umsetzungsstand).
        approved = qs.filter(vote_result="approved")
        tracking_stats = {
            "open": approved.filter(implementation_status="open").count(),
            "in_progress": approved.filter(implementation_status="in_progress").count(),
            "done": approved.filter(implementation_status="done").count(),
            "deferred": approved.filter(implementation_status="deferred").count(),
            "overdue": qs.filter(overdue_q).count(),
        }

        valid_statuses = {value for value, _ in SessionAgendaItem.IMPLEMENTATION_CHOICES}
        impl_status = self.request.GET.get("status")
        if impl_status in valid_statuses:
            qs = qs.filter(vote_result="approved", implementation_status=impl_status)
        overdue = self.request.GET.get("overdue") == "1"
        if overdue:
            qs = qs.filter(overdue_q)

        items = list(qs.prefetch_related("forwardings")[:300])

        years = sorted(
            {ms.year for ms in qs.values_list("meeting__start", flat=True) if ms},
            reverse=True,
        )
        context.update(
            {
                "items": items,
                "organizations": SessionOrganization.objects.filter(
                    tenant=self.session_tenant, is_active=True
                ).order_by("name"),
                "years": years,
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
        next_url = request.POST.get("next", "")
        if next_url.startswith(f"/session/{self.session_tenant.slug}/"):
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

        pdf_bytes = resolution_service.build_extract_pdf(items, internal=include_np)
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
        pdf_bytes = resolution_service.build_extract_pdf([item], internal=include_np)
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
        next_url = request.POST.get("next", "")
        if next_url.startswith(f"/session/{self.session_tenant.slug}/"):
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
        item.implementation_updated_at = timezone.now()
        item.implementation_updated_by = self.session_user
        item.save(
            update_fields=[
                "implementation_status",
                "implementation_recipient",
                "implementation_deadline",
                "implementation_note",
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
        next_url = request.POST.get("next", "")
        if next_url.startswith(f"/session/{self.session_tenant.slug}/"):
            return redirect(next_url)
        return redirect("session:resolutions", tenant_slug=self.session_tenant.slug)


class ResolutionCsvExportView(SessionViewMixin, View):
    """CSV-Export des Beschlussregisters inkl. Umsetzungsstand (Issue #37)."""

    permission_required = "view_meetings"
    http_method_names = ["get"]

    def get(self, request, tenant_slug):
        include_np = self.has_permission("view_non_public_meetings")
        qs = resolution_service.decided_items(self.session_tenant, include_non_public=include_np)

        org_id = request.GET.get("organization")
        if org_id:
            qs = qs.filter(meeting__organization_id=org_id)
        year = request.GET.get("year")
        if year and year.isdigit():
            qs = qs.filter(meeting__start__year=int(year))
        result = request.GET.get("result")
        if result in resolution_service.DECIDED_RESULTS:
            qs = qs.filter(vote_result=result)

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="beschlussregister.csv"'
        response.write("﻿")  # BOM für Excel

        writer = csv.writer(response, delimiter=";")
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
                    item.meeting.start.strftime("%d.%m.%Y") if item.meeting.start else "",
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
