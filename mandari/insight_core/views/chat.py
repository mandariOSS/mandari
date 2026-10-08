# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

import json
import logging
from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from apps.common.ki_anbieter import KiEndpunkt, KiHinweis, endpunkt_fuer_insight

from ..models import (
    ChatUsage,
)
from ._helpers import ActiveBodyRequiredMixin, get_active_body

#: Sitzungsschlüssel der Einwilligung; der Wert ist die Kennung des Anbieters, für den sie gilt
EINWILLIGUNG = "chat_consent"


def _ki_hinweis() -> KiHinweis | None:
    """Anbieter des KI-Assistenten (Anzeigename, Verarbeitungsort) aus der KI-Konfiguration; ohne Endpunkt ``None``."""
    endpunkt = endpunkt_fuer_insight()
    return endpunkt.hinweis() if endpunkt is not None else None


def _hat_einwilligung(request, hinweis: KiHinweis | None) -> bool:
    """Eine Einwilligung gilt nur für den Anbieter, dem zugestimmt wurde; nach einem Wechsel erneut fragen."""
    return hinweis is not None and request.session.get(EINWILLIGUNG) == hinweis.einwilligungskennung


def _nicht_verfuegbar() -> JsonResponse:
    return JsonResponse(
        {
            "error": "ai_unavailable",
            "message": "Der KI-Assistent ist derzeit nicht verfügbar. Bitte versuchen Sie es später erneut.",
        },
        status=503,
    )


# =============================================================================
# Chat (KI-Assistent)
# =============================================================================


class ChatView(ActiveBodyRequiredMixin, TemplateView):
    """KI-Chat-Interface."""

    template_name = "pages/chat.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Ohne freigegebenen Endpunkt kein Chat; Anbieter und Verarbeitungsort für die Einwilligung (Issue #950)
        hinweis = _ki_hinweis()
        ctx["ki_endpunkt"] = hinweis
        ctx["has_chat_consent"] = _hat_einwilligung(self.request, hinweis)

        from ..seo import get_page_seo

        ctx["seo"] = get_page_seo(
            self.request,
            title="KI-Assistent",
            description="Fragen zur Kommunalpolitik stellen: Der KI-Assistent durchsucht Ratsinformationen und antwortet mit Quellenangaben.",
            body=get_active_body(self.request),
        ).to_dict()
        return ctx


def _get_client_ip(request):
    """Extract client IP from request (respects X-Forwarded-For)."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "127.0.0.1")


def _check_rate_limit(request) -> tuple[bool, dict]:
    """
    Check rate limits for chat usage.

    Tiers:
        Anonymous:    5/day,  10/week  (tracked by IP + session)
        Registered:  25/day, 100/week  (tracked by user_id)
        Staff:       unlimited

    Returns:
        (is_allowed, info_dict) where info_dict has remaining_today, remaining_week, resets_at
    """
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    tomorrow = today_start + timedelta(days=1)

    user = request.user if request.user.is_authenticated else None

    # Staff: unlimited
    if user and user.is_staff:
        return True, {"remaining_today": 999, "remaining_week": 999, "resets_at": None}

    if user:
        # Registered user
        day_limit, week_limit = 25, 100
        day_count = ChatUsage.objects.filter(user=user, created_at__gte=today_start, filter_result="passed").count()
        week_count = ChatUsage.objects.filter(user=user, created_at__gte=week_start, filter_result="passed").count()
    else:
        # Anonymous: check both IP and session, use the stricter count
        day_limit, week_limit = 5, 10
        ip = _get_client_ip(request)
        session_key = request.session.session_key or ""

        ip_day = ChatUsage.objects.filter(ip_address=ip, created_at__gte=today_start, filter_result="passed").count()
        ip_week = ChatUsage.objects.filter(ip_address=ip, created_at__gte=week_start, filter_result="passed").count()

        if session_key:
            sess_day = ChatUsage.objects.filter(
                session_key=session_key, created_at__gte=today_start, filter_result="passed"
            ).count()
            sess_week = ChatUsage.objects.filter(
                session_key=session_key, created_at__gte=week_start, filter_result="passed"
            ).count()
            day_count = max(ip_day, sess_day)
            week_count = max(ip_week, sess_week)
        else:
            day_count = ip_day
            week_count = ip_week

    remaining_today = max(0, day_limit - day_count)
    remaining_week = max(0, week_limit - week_count)
    is_allowed = remaining_today > 0 and remaining_week > 0

    return is_allowed, {
        "remaining_today": remaining_today,
        "remaining_week": remaining_week,
        "resets_at": tomorrow.isoformat(),
    }


@require_POST
def chat_message(request):
    """
    API endpoint for chat messages.

    Pipeline:
    1. Parse JSON body
    2. Handle consent-set request
    3. Check DSGVO consent (session)
    4. Check rate limit
    5. Run content filters (PII, spam, injection)
    6. Build RAG context from Elasticsearch
    7. KI-Aufruf über chat_service (Endpunkt aus der zentralen KI-Konfiguration)
    8. Log ChatUsage
    9. Return response + sources + remaining counts
    """
    # 1. Parse JSON
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    # 2. Handle consent-set request: gilt nur für den aktuell eingerichteten Anbieter. Der Endpunkt wird je
    # Anfrage genau einmal aufgelöst; Einwilligung und KI-Aufruf beziehen sich auf denselben Endpunkt.
    endpunkt: KiEndpunkt | None = endpunkt_fuer_insight()
    hinweis = endpunkt.hinweis() if endpunkt is not None else None
    if data.get("consent") is True:
        if hinweis is None:
            return _nicht_verfuegbar()
        request.session[EINWILLIGUNG] = hinweis.einwilligungskennung
        request.session.modified = True
        return JsonResponse({"status": "consent_granted"})

    message = data.get("message", "").strip()
    history = data.get("history", [])

    if not message:
        return JsonResponse({"error": "Message is required"}, status=400)

    # 3. Check DSGVO consent (für genau diesen Anbieter)
    if endpunkt is None or hinweis is None:
        return _nicht_verfuegbar()
    if not _hat_einwilligung(request, hinweis):
        return JsonResponse(
            {"error": "consent_required", "message": "Bitte stimmen Sie der Datenverarbeitung zu."},
            status=403,
        )

    # Ensure session exists for tracking
    if not request.session.session_key:
        request.session.create()

    ip_address = _get_client_ip(request)
    session_key = request.session.session_key or ""
    user = request.user if request.user.is_authenticated else None

    # 4. Check rate limit
    is_allowed, rate_info = _check_rate_limit(request)
    if not is_allowed:
        # Log the blocked attempt
        ChatUsage.objects.create(
            session_key=session_key,
            ip_address=ip_address,
            user=user,
            message=message[:500],
            filter_result="passed",
            tokens_used=0,
        )
        msg = "Tageslimit erreicht."
        if not user:
            msg += " Erstellen Sie ein kostenloses Konto für mehr Anfragen."
        elif rate_info["remaining_week"] <= 0:
            msg = "Wochenlimit erreicht. Bitte versuchen Sie es nächste Woche erneut."
        return JsonResponse(
            {
                "error": "rate_limited",
                "message": msg,
                "remaining_today": rate_info["remaining_today"],
                "remaining_week": rate_info["remaining_week"],
                "resets_at": rate_info["resets_at"],
            },
            status=429,
        )

    # 5. Run content filters
    from insight_ai.services.chat_filters import check_message

    filter_result = check_message(message, session_key)
    if filter_result.get("blocked"):
        ChatUsage.objects.create(
            session_key=session_key,
            ip_address=ip_address,
            user=user,
            message=message[:500],
            filter_result=filter_result["filter_result"],
            tokens_used=0,
        )
        return JsonResponse(
            {
                "error": "content_blocked",
                "reason": filter_result["reason"],
                "message": filter_result["message"],
                "remaining_today": rate_info["remaining_today"],
                "remaining_week": rate_info["remaining_week"],
            },
            status=422,
        )

    # 6-7. Build RAG context and call AI
    # Außerhalb des try: Eine abgeschaltete Kommune endet mit dem Hinweis statt als interner Fehler
    body = get_active_body(request)
    body_id = str(body.id) if body else None
    try:
        from insight_ai.services.chat_service import process_chat_message

        result = process_chat_message(
            message=message,
            history=history,
            body_id=body_id,
            endpunkt=endpunkt,
        )
    except ValueError as e:
        # Provider not configured
        logger = logging.getLogger(__name__)
        logger.warning(f"Chat AI unavailable: {e}")
        return _nicht_verfuegbar()
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.exception(f"Chat error: {e}")
        return JsonResponse(
            {
                "error": "internal_error",
                "message": "Ein interner Fehler ist aufgetreten. Bitte versuchen Sie es erneut.",
            },
            status=500,
        )

    # 8. Log usage
    ChatUsage.objects.create(
        session_key=session_key,
        ip_address=ip_address,
        user=user,
        message=message[:500],
        filter_result="passed",
        tokens_used=result.get("tokens_used", 0),
    )

    # Update remaining counts (decrement by 1)
    rate_info["remaining_today"] = max(0, rate_info["remaining_today"] - 1)
    rate_info["remaining_week"] = max(0, rate_info["remaining_week"] - 1)

    # 9. Return response
    return JsonResponse(
        {
            "response": result["response"],
            "sources": result["sources"],
            "remaining_today": rate_info["remaining_today"],
            "remaining_week": rate_info["remaining_week"],
        }
    )
