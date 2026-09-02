# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Problem melden"-Formular (Fehlerseiten -> Ticket im Admin-Dashboard).

Öffentlich erreichbar (Fehler treffen auch nicht angemeldete Personen).
Angemeldete Nutzer werden automatisch verknüpft und erhalten Rückmeldungen
an ihre Konto-Adresse; alternativ kann eine E-Mail-Adresse angegeben werden.
Einfaches Rate-Limit pro IP gegen Missbrauch.
"""

import logging

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from .models import ProblemReport

logger = logging.getLogger(__name__)

# Rate-Limit: höchstens N Meldungen pro IP und Stunde
MAX_REPORTS_PER_HOUR = 5


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class ProblemReportView(View):
    """Formular anzeigen (GET) und Meldung entgegennehmen (POST)."""

    template_name = "feedback/report.html"

    def get(self, request):
        context = {
            "error_id": request.GET.get("error_id", "")[:64],
            "page_url": request.GET.get("url", "")[:1000],
        }
        return render(request, self.template_name, context)

    def post(self, request):
        message = request.POST.get("message", "").strip()
        error_id = request.POST.get("error_id", "").strip()[:64]
        page_url = request.POST.get("url", "").strip()[:1000]
        browser_info = request.POST.get("browser_info", "").strip()[:2000]
        email = request.POST.get("email", "").strip()[:254]

        context = {
            "error_id": error_id,
            "page_url": page_url,
            "message_value": message,
            "email_value": email,
        }

        if len(message) < 10:
            messages.error(request, "Bitte beschreibe das Problem in mindestens 10 Zeichen.")
            return render(request, self.template_name, context, status=400)

        ip_address = _client_ip(request)
        one_hour_ago = timezone.now() - timezone.timedelta(hours=1)
        if (
            ip_address
            and ProblemReport.objects.filter(ip_address=ip_address, created_at__gte=one_hour_ago).count()
            >= MAX_REPORTS_PER_HOUR
        ):
            messages.error(
                request,
                "Zu viele Meldungen in kurzer Zeit — bitte versuche es später erneut.",
            )
            return render(request, self.template_name, context, status=429)

        report = ProblemReport.objects.create(
            error_id=error_id,
            url=page_url,
            message=message,
            browser_info=browser_info,
            user=request.user if request.user.is_authenticated else None,
            email=email,
            ip_address=ip_address,
        )
        logger.warning(
            "Neue Fehlermeldung %s (Fehler-ID %s, %s)",
            report.reference,
            error_id or "-",
            page_url or "-",
        )
        return redirect("problem_report_done", reference=report.reference)


class ProblemReportDoneView(View):
    """Bestätigungsseite mit Ticket-Nummer."""

    def get(self, request, reference):
        report = ProblemReport.objects.filter(reference=reference).first()
        return render(
            request,
            "feedback/done.html",
            {
                "reference": reference,
                "has_contact": bool(report and report.reporter_email),
            },
        )
