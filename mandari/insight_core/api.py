"""
Public API endpoints for Mandari Insight Core.

Provides statistics and body data for the Wagtail marketing site
and other consumers.
"""

from django.http import JsonResponse
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_GET

from .models import (
    ContactRequest,
    OParlBody,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)


@require_GET
@cache_page(60 * 5)  # 5 minutes cache
def stats(request):
    """
    Public statistics endpoint.

    GET /api/stats/
    Returns aggregate counts of all OParl data.
    """
    return JsonResponse(
        {
            "bodies": OParlBody.objects.count(),
            "organizations": OParlOrganization.objects.count(),
            "persons": OParlPerson.objects.count(),
            "meetings": OParlMeeting.objects.count(),
            "papers": OParlPaper.objects.count(),
        }
    )


@require_GET
@cache_page(60 * 15)  # 15 minutes cache
def stats_bodies(request):
    """
    Public body list endpoint.

    GET /api/stats/bodies/
    Returns list of all bodies with basic info.
    """
    bodies = OParlBody.objects.all().order_by("name")
    data = []
    for body in bodies:
        data.append(
            {
                "id": str(body.id),
                "name": body.name,
                "short_name": body.short_name,
                "display_name": body.get_display_name(),
                "slug": body.slug,
                "website": body.website or "",
                "latitude": float(body.latitude) if body.latitude else None,
                "longitude": float(body.longitude) if body.longitude else None,
                "organizations_count": OParlOrganization.objects.filter(body=body).count(),
                "persons_count": OParlPerson.objects.filter(body=body).count(),
                "meetings_count": OParlMeeting.objects.filter(body=body).count(),
                "papers_count": OParlPaper.objects.filter(body=body).count(),
            }
        )
    return JsonResponse({"bodies": data})


@require_GET
@cache_page(60 * 5)
def contact_subjects(request):
    """
    Returns available contact form subjects.

    GET /api/contact/subjects/
    """
    subjects = [
        {"value": "demo", "label": "Demo-Anfrage"},
        {"value": "preise", "label": "Preisanfrage"},
        {"value": "support", "label": "Support-Anfrage"},
        {"value": "datenschutz", "label": "Datenschutz-Anfrage"},
        {"value": "sonstiges", "label": "Sonstige Anfrage"},
    ]
    return JsonResponse({"subjects": subjects})


def _get_client_ip(request):
    """Extract client IP address from request."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def contact_submit(request):
    """
    Contact form submission endpoint.

    POST /api/contact/
    Accepts JSON body with: name, email, organization, subject, message
    """
    import json
    import logging

    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    logger = logging.getLogger(__name__)

    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    # Honeypot check
    if data.get("website"):
        logger.warning(f"Honeypot triggered from IP {_get_client_ip(request)}")
        return JsonResponse({"error": "Submission rejected"}, status=400)

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    organization = (data.get("organization") or "").strip()
    subject = (data.get("subject") or "").strip()
    message = (data.get("message") or "").strip()

    # Validation
    if not all([name, email, subject, message]):
        return JsonResponse({"error": "Missing required fields: name, email, subject, message"}, status=400)

    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse({"error": "Invalid email address"}, status=400)

    valid_subjects = ["demo", "preise", "support", "datenschutz", "sonstiges"]
    if subject not in valid_subjects:
        return JsonResponse({"error": f"Invalid subject. Must be one of: {', '.join(valid_subjects)}"}, status=400)

    try:
        contact = ContactRequest.objects.create(
            name=name,
            email=email,
            organization_name=organization,
            subject=subject,
            message=message,
            ip_address=_get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
        )
    except Exception as e:
        logger.error(f"Failed to create ContactRequest: {e}")
        return JsonResponse({"error": "Internal server error"}, status=500)

    # Send notification emails
    CONTACT_EMAIL = "hello@mandari.de"
    try:
        from apps.common.email import send_template_email

        subject_map = {
            "demo": "Demo-Anfrage",
            "preise": "Preisanfrage",
            "support": "Support-Anfrage",
            "datenschutz": "Datenschutz-Anfrage",
            "sonstiges": "Kontaktanfrage",
        }

        email_context = {
            "contact": contact,
            "admin_url": request.build_absolute_uri(f"/admin/insight_core/contactrequest/{contact.id}/change/"),
        }

        email_subject = f"[Mandari] {subject_map.get(subject, 'Kontaktanfrage')} von {name}"

        notification_sent = send_template_email(
            subject=email_subject,
            template_name="emails/contact/notification",
            context=email_context,
            to=[CONTACT_EMAIL],
            reply_to=[email],
            fail_silently=True,
        )
        if notification_sent:
            contact.notification_sent = True

        confirmation_sent = send_template_email(
            subject="Ihre Anfrage bei Mandari - Bestätigung",
            template_name="emails/contact/confirmation",
            context=email_context,
            to=[email],
            fail_silently=True,
        )
        if confirmation_sent:
            contact.confirmation_sent = True

        contact.save(update_fields=["notification_sent", "confirmation_sent"])
    except Exception as e:
        logger.error(f"Error sending contact emails: {e}")

    return JsonResponse(
        {
            "success": True,
            "message": "Vielen Dank für Ihre Nachricht! Wir werden uns schnellstmöglich bei Ihnen melden.",
        },
        status=201,
    )
