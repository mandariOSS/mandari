"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

import logging

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView

from ..models import (
    InsightSubscriber,
)
from ._helpers import get_active_body

# =============================================================================
# Benachrichtigungen (Subscriptions)
# =============================================================================


class SubscribeView(TemplateView):
    """Abo-Formular für E-Mail-Benachrichtigungen."""

    template_name = "pages/subscribe.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        context["active_body"] = body

        # Vorausgefüllte Werte aus Query-Parametern
        context["prefill_type"] = self.request.GET.get("type", "")
        context["prefill_keyword"] = self.request.GET.get("keyword", "")
        context["prefill_lat"] = self.request.GET.get("lat", "")
        context["prefill_lon"] = self.request.GET.get("lon", "")
        context["prefill_name"] = self.request.GET.get("name", "")

        return context

    def post(self, request, *args, **kwargs):
        """Erstellt Subscriber + sendet Bestätigungsmail."""
        body = get_active_body(request)
        if not body:
            return JsonResponse({"error": "Keine Kommune ausgewählt"}, status=400)

        email = request.POST.get("email", "").strip().lower()
        if not email or "@" not in email:
            return render(
                request, "partials/subscribe_error.html", {"error": "Bitte geben Sie eine gültige E-Mail-Adresse ein."}
            )

        # Abo-Typen aus Feldinhalt ableiten (kein Checkbox mehr)
        neighborhood_name = request.POST.get("neighborhood_name", "").strip()
        neighborhood_lat = request.POST.get("neighborhood_lat", "").strip()
        neighborhood_lon = request.POST.get("neighborhood_lon", "").strip()
        keyword = request.POST.get("keyword", "").strip()
        digest_frequency = request.POST.get("digest_frequency", "weekly")

        if digest_frequency not in ("weekly", "biweekly"):
            digest_frequency = "weekly"

        neighborhood_active = bool(neighborhood_lat and neighborhood_lon)
        keyword_active = bool(keyword)

        if not neighborhood_active and not keyword_active:
            return render(
                request,
                "partials/subscribe_error.html",
                {"error": "Bitte geben Sie eine Adresse oder einen Suchbegriff ein."},
            )

        # Subscriber erstellen oder aktualisieren
        subscriber, created = InsightSubscriber.objects.get_or_create(
            email=email,
            body=body,
            defaults={
                "digest_frequency": digest_frequency,
            },
        )

        if not created and subscriber.confirmed and subscriber.unsubscribed_at is None:
            # Bereits bestätigt und aktiv → zur Verwaltungsseite leiten
            return render(
                request,
                "partials/subscribe_success.html",
                {
                    "message": "Sie haben bereits ein aktives Abo. Überprüfen Sie Ihre E-Mail für den Verwaltungslink.",
                    "already_exists": True,
                },
            )

        # Abo-Daten aktualisieren
        subscriber.neighborhood_active = neighborhood_active
        if neighborhood_active:
            subscriber.neighborhood_lat = neighborhood_lat or None
            subscriber.neighborhood_lon = neighborhood_lon or None
            subscriber.neighborhood_name = neighborhood_name or None
            try:
                subscriber.neighborhood_radius = int(request.POST.get("neighborhood_radius", "500"))
            except (ValueError, TypeError):
                subscriber.neighborhood_radius = 500
        else:
            subscriber.neighborhood_lat = None
            subscriber.neighborhood_lon = None
            subscriber.neighborhood_name = None

        subscriber.keyword_active = keyword_active
        subscriber.keyword = keyword or None

        subscriber.digest_frequency = digest_frequency
        subscriber.unsubscribed_at = None  # Resubscribe falls abgemeldet
        subscriber.save()

        # Bestätigungsmail senden
        _send_confirmation_email(subscriber)

        return render(
            request,
            "partials/subscribe_success.html",
            {
                "message": "Fast geschafft! Bitte bestätigen Sie Ihr Abo über den Link in der E-Mail.",
            },
        )


def _send_confirmation_email(subscriber):
    """Sendet Double-Opt-In-Bestätigungsmail."""
    from django.conf import settings as django_settings
    from django.core.mail import send_mail
    from django.template.loader import render_to_string

    site_url = getattr(django_settings, "SITE_URL", "http://localhost:8000")
    confirm_url = f"{site_url}/insight/abo/bestaetigen/{subscriber.token}/"

    subject = "Bitte bestätigen Sie Ihr Mandari-Abo"

    html_message = render_to_string(
        "emails/insight_confirm.html",
        {
            "subscriber": subscriber,
            "confirm_url": confirm_url,
            "site_url": site_url,
        },
    )

    from_email = getattr(django_settings, "INSIGHT_DIGEST_FROM_EMAIL", "") or getattr(
        django_settings, "DEFAULT_FROM_EMAIL", "noreply@mandari.de"
    )

    try:
        send_mail(
            subject=subject,
            message=f"Bestätigen Sie Ihr Abo: {confirm_url}",
            from_email=from_email,
            recipient_list=[subscriber.email],
            html_message=html_message,
            fail_silently=True,
        )
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to send confirmation email: {e}")


@require_GET
def confirm_subscription(request, token):
    """Bestätigt Double Opt-In."""
    subscriber = get_object_or_404(InsightSubscriber, token=token)

    if subscriber.confirmed:
        return render(
            request,
            "pages/subscription_confirmed.html",
            {
                "subscriber": subscriber,
                "already_confirmed": True,
            },
        )

    subscriber.confirmed = True
    subscriber.confirmed_at = timezone.now()
    subscriber.save(update_fields=["confirmed", "confirmed_at", "updated_at"])

    return render(
        request,
        "pages/subscription_confirmed.html",
        {
            "subscriber": subscriber,
            "already_confirmed": False,
        },
    )


def manage_subscription(request, token):
    """Abo verwalten (GET zeigt Formular, POST aktualisiert)."""
    subscriber = get_object_or_404(InsightSubscriber, token=token)

    if request.method == "POST":
        # Abo-Typen aus Feldinhalt ableiten (kein Checkbox mehr)
        neighborhood_name = request.POST.get("neighborhood_name", "").strip()
        neighborhood_lat = request.POST.get("neighborhood_lat", "").strip()
        neighborhood_lon = request.POST.get("neighborhood_lon", "").strip()
        keyword = request.POST.get("keyword", "").strip()

        subscriber.neighborhood_active = bool(neighborhood_lat and neighborhood_lon)
        if subscriber.neighborhood_active:
            subscriber.neighborhood_lat = neighborhood_lat or None
            subscriber.neighborhood_lon = neighborhood_lon or None
            subscriber.neighborhood_name = neighborhood_name or None
            try:
                subscriber.neighborhood_radius = int(request.POST.get("neighborhood_radius", "500"))
            except (ValueError, TypeError):
                subscriber.neighborhood_radius = 500
        else:
            subscriber.neighborhood_lat = None
            subscriber.neighborhood_lon = None
            subscriber.neighborhood_name = None

        subscriber.keyword_active = bool(keyword)
        subscriber.keyword = keyword or None

        freq = request.POST.get("digest_frequency", "weekly")
        subscriber.digest_frequency = freq if freq in ("weekly", "biweekly") else "weekly"

        subscriber.save()

        return render(
            request,
            "pages/subscription_manage.html",
            {
                "subscriber": subscriber,
                "saved": True,
            },
        )

    return render(
        request,
        "pages/subscription_manage.html",
        {
            "subscriber": subscriber,
        },
    )


@require_GET
def unsubscribe(request, token):
    """Sofort abmelden (1-Klick)."""
    subscriber = get_object_or_404(InsightSubscriber, token=token)

    if subscriber.unsubscribed_at is None:
        subscriber.unsubscribed_at = timezone.now()
        subscriber.save(update_fields=["unsubscribed_at", "updated_at"])

    return render(
        request,
        "pages/subscription_unsubscribed.html",
        {
            "subscriber": subscriber,
        },
    )
