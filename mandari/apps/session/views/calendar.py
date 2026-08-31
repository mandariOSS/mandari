# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungskalender und Jahresplanung (Issue #82).

Views für:
- Monatskalender über alle Gremien
- Jahresplanung mit Serienterminen (Vorschau -> Anlage als Entwürfe)
- Sitzungsplan-PDF (Jahresübersicht)
- ICS-Abo-Feed je Gremium (nur öffentliche Sitzungen, ohne Anmeldung)
"""

from datetime import date, time

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionMeeting, SessionOrganization
from ..permissions import SessionViewMixin
from ..services import calendar_service

MONTH_NAMES = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


class MeetingCalendarView(SessionViewMixin, TemplateView):
    """Monatskalender über alle Gremien des Mandanten."""

    template_name = "session/calendar/month.html"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        try:
            year = int(self.request.GET.get("year", today.year))
            month = int(self.request.GET.get("month", today.month))
            date(year, month, 1)
        except (TypeError, ValueError):
            year, month = today.year, today.month
        year = max(2000, min(2100, year))

        include_np = self.has_permission("view_non_public_meetings")
        weeks, count = calendar_service.month_grid(
            self.session_tenant, year, month, include_non_public=include_np
        )

        prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

        context.update(
            {
                "weeks": weeks,
                "meetings_count": count,
                "year": year,
                "month": month,
                "month_name": MONTH_NAMES[month],
                "prev_year": prev_year,
                "prev_month": prev_month,
                "next_year": next_year,
                "next_month": next_month,
                "today": today,
                "organizations": SessionOrganization.objects.filter(
                    tenant=self.session_tenant, is_active=True
                ).order_by("name"),
                "can_plan": self.has_permission("edit_meetings"),
                "weekday_labels": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
            }
        )
        return context


class MeetingPlanView(SessionViewMixin, TemplateView):
    """
    Jahresplanung: Serientermine berechnen (Vorschau mit Kollisionsprüfung)
    und als Sitzungs-Entwürfe anlegen.
    """

    template_name = "session/calendar/plan.html"
    permission_required = "edit_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "organizations": SessionOrganization.objects.filter(
                    tenant=self.session_tenant, is_active=True
                ).order_by("name"),
                "rhythm_choices": calendar_service.RHYTHM_CHOICES,
                "weekday_choices": calendar_service.WEEKDAY_CHOICES,
                "next_year": timezone.localdate().year + 1,
            }
        )
        return context

    def _parse_form(self, request):
        """Formular validieren; wirft ValueError mit Nutzermeldung."""
        from django.core.exceptions import ValidationError as DjangoValidationError

        org = None
        org_id = request.POST.get("organization", "").strip()
        if org_id:
            try:
                org = SessionOrganization.objects.filter(
                    tenant=self.session_tenant, is_active=True, pk=org_id
                ).first()
            except (ValueError, DjangoValidationError):
                org = None
        if org is None:
            raise ValueError("Bitte ein Gremium auswählen.")

        rhythm = request.POST.get("rhythm", "")
        if rhythm not in {value for value, _ in calendar_service.RHYTHM_CHOICES}:
            raise ValueError("Bitte einen gültigen Rhythmus auswählen.")

        try:
            weekday = int(request.POST.get("weekday", ""))
            assert 0 <= weekday <= 6
        except (TypeError, ValueError, AssertionError):
            raise ValueError("Bitte einen Wochentag auswählen.")

        try:
            start_time = time.fromisoformat(request.POST.get("time", ""))
        except (TypeError, ValueError):
            raise ValueError("Bitte eine gültige Uhrzeit angeben.")

        try:
            date_from = date.fromisoformat(request.POST.get("date_from", ""))
            date_to = date.fromisoformat(request.POST.get("date_to", ""))
        except (TypeError, ValueError):
            raise ValueError("Bitte einen gültigen Zeitraum angeben.")
        if date_to < date_from:
            raise ValueError("Das Enddatum liegt vor dem Startdatum.")
        if (date_to - date_from).days > 400:
            raise ValueError("Der Planungszeitraum darf höchstens ein Jahr umfassen.")

        return {
            "organization": org,
            "rhythm": rhythm,
            "weekday": weekday,
            "time": start_time,
            "date_from": date_from,
            "date_to": date_to,
            "name": request.POST.get("name", "").strip()[:500] or f"Sitzung: {org.name}",
            "location": request.POST.get("location", "").strip()[:500]
            or org.default_meeting_location,
            "room": request.POST.get("room", "").strip()[:100],
            "is_public": request.POST.get("is_public", "1") == "1",
        }

    def post(self, request, tenant_slug):
        try:
            form = self._parse_form(request)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("session:meeting_plan", tenant_slug=tenant_slug)

        starts = calendar_service.generate_series(
            rhythm=form["rhythm"],
            weekday=form["weekday"],
            start_time=form["time"],
            date_from=form["date_from"],
            date_to=form["date_to"],
        )
        if not starts:
            messages.error(request, "Im gewählten Zeitraum ergibt die Serie keine Termine.")
            return redirect("session:meeting_plan", tenant_slug=tenant_slug)

        entries = [
            {
                "start": start,
                "conflicts": calendar_service.find_conflicts(
                    self.session_tenant, start, room=form["room"]
                ),
            }
            for start in starts
        ]

        if request.POST.get("action") != "create":
            # Vorschau anzeigen
            context = self.get_context_data()
            context.update({"preview": entries, "form": form, "form_post": request.POST})
            return self.render_to_response(context)

        from ..services import textblock_service

        created = 0
        for entry in entries:
            meeting = SessionMeeting.objects.create(
                tenant=self.session_tenant,
                name=form["name"],
                organization=form["organization"],
                start=entry["start"],
                location=form["location"],
                room=form["room"],
                is_public=form["is_public"],
                meeting_state="draft",
            )
            # Standard-TOPs des Gremiums automatisch übernehmen (Issue #85)
            textblock_service.apply_standard_items(meeting)
            created += 1
        audit.log_event(
            "create",
            form["organization"],
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "sitzungsserie": form["name"],
                "gremium": form["organization"].name,
                "anzahl": created,
                "zeitraum": f"{form['date_from'].isoformat()} – {form['date_to'].isoformat()}",
            },
        )
        conflict_count = sum(1 for e in entries if e["conflicts"])
        note = f" ({conflict_count} Termin(e) mit Kollisionshinweis)" if conflict_count else ""
        messages.success(
            request,
            f"{created} Sitzungstermine für {form['organization'].name} als Entwurf angelegt{note}.",
        )
        return redirect("session:meeting_calendar", tenant_slug=tenant_slug)


class YearPlanPdfView(SessionViewMixin, View):
    """Sitzungsplan-PDF: Jahresübersicht, optional je Gremium."""

    permission_required = "view_meetings"
    http_method_names = ["get"]

    def get(self, request, tenant_slug):
        today = timezone.localdate()
        try:
            year = int(request.GET.get("year", today.year))
            assert 2000 <= year <= 2100
        except (TypeError, ValueError, AssertionError):
            year = today.year

        from django.core.exceptions import ValidationError as DjangoValidationError

        organization = None
        org_id = request.GET.get("organization")
        if org_id:
            try:
                organization = SessionOrganization.objects.filter(
                    tenant=self.session_tenant, pk=org_id
                ).first()
            except (ValueError, DjangoValidationError):
                organization = None

        include_np = self.has_permission("view_non_public_meetings")
        pdf_bytes = calendar_service.build_year_plan_pdf(
            self.session_tenant, year, organization=organization, include_non_public=include_np
        )
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="sitzungsplan-{year}.pdf"'
        return response


class OrganizationIcsFeedView(View):
    """
    ICS-Abo-Feed je Gremium — bewusst OHNE Anmeldung abrufbar, damit
    Outlook/Thunderbird ihn abonnieren können. Enthält deshalb
    ausschließlich öffentliche, nicht abgesagte Sitzungen.
    """

    http_method_names = ["get"]

    def get(self, request, tenant_slug, org_id):
        organization = get_object_or_404(
            SessionOrganization.objects.select_related("tenant"),
            pk=org_id,
            tenant__slug=tenant_slug,
            tenant__is_active=True,
            is_active=True,
        )
        ics_bytes = calendar_service.build_organization_feed(organization)
        response = HttpResponse(ics_bytes, content_type="text/calendar; charset=utf-8")
        response["Content-Disposition"] = 'inline; filename="sitzungen.ics"'
        return response
