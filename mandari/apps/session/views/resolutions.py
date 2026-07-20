# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussregister und Beschlussauszüge (Issue #32).

Views für:
- Beschlussregister je Mandant (filterbar nach Gremium, Jahr, Ergebnis)
- Sammel-Ausfertigung: Nummernvergabe + Sammel-PDF je Sitzung
- Beschlussauszug-PDF je TOP
- Versand-/Übergabevermerk mit Audit-Eintrag
"""

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
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
