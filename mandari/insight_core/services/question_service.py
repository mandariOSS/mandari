"""
Service für öffentliche Ratsfragen.

E-Mail-Versand und Hilfsfunktionen für den Ratsfragen-Workflow.
"""

import logging

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


def _get_site_url() -> str:
    return getattr(settings, "SITE_URL", "http://localhost:8000")


def send_verification_email(question) -> bool:
    """Sendet Verifizierungs-E-Mail an Fragesteller:in."""
    from apps.common.email import send_template_email

    site_url = _get_site_url()
    verify_url = f"{site_url}/insight/fragen/verifizieren/{question.verification_token}/"

    return send_template_email(
        subject=f"Bitte bestätigen Sie Ihre Frage an {question.recipient.display_name}",
        template_name="emails/questions/verification",
        context={
            "question": question,
            "verify_url": verify_url,
            "site_url": site_url,
        },
        to=[question.questioner_email],
        fail_silently=True,
    )


def send_question_notification_to_recipient(question) -> bool:
    """Benachrichtigt Ratsmitglied über freigeschaltete Frage."""
    from apps.common.email import send_template_email

    if not question.recipient.email:
        logger.warning(
            f"Ratsmitglied {question.recipient} hat keine E-Mail-Adresse."
        )
        return False

    site_url = _get_site_url()
    answer_url = f"{site_url}/insight/fragen/antworten/{question.answer_token}/"
    question_url = f"{site_url}/insight/personen/{question.recipient.id}/"

    return send_template_email(
        subject=f"Neue Frage von {question.questioner_name}",
        template_name="emails/questions/notification",
        context={
            "question": question,
            "answer_url": answer_url,
            "question_url": question_url,
            "site_url": site_url,
        },
        to=[question.recipient.email],
        fail_silently=True,
    )


def send_answer_notification_to_questioner(question) -> bool:
    """Benachrichtigt Fragesteller:in über veröffentlichte Antwort."""
    from apps.common.email import send_template_email

    site_url = _get_site_url()
    question_url = f"{site_url}/insight/personen/{question.recipient.id}/"

    return send_template_email(
        subject=f"{question.recipient.display_name} hat Ihre Frage beantwortet",
        template_name="emails/questions/answer_notification",
        context={
            "question": question,
            "question_url": question_url,
            "site_url": site_url,
        },
        to=[question.questioner_email],
        fail_silently=True,
    )


def send_answer_reminder(question) -> bool:
    """Sendet Erinnerung an Ratsmitglied."""
    from apps.common.email import send_template_email

    if not question.recipient.email:
        return False

    site_url = _get_site_url()
    answer_url = f"{site_url}/insight/fragen/antworten/{question.answer_token}/"

    return send_template_email(
        subject=f"Erinnerung: Unbeantwortete Frage von {question.questioner_name}",
        template_name="emails/questions/answer_reminder",
        context={
            "question": question,
            "answer_url": answer_url,
            "site_url": site_url,
        },
        to=[question.recipient.email],
        fail_silently=True,
    )


def get_answer_stats(person) -> dict:
    """Berechnet Antwort-Statistiken für eine Person."""
    from ..models import PublicQuestion

    published = PublicQuestion.objects.filter(
        recipient=person,
        status="published",
    )
    total = published.count()
    answered = published.filter(answer_status="published").count()

    return {
        "total": total,
        "answered": answered,
        "rate": round(answered / total * 100) if total > 0 else 0,
    }


def check_rate_limit(email: str, max_per_day: int = 3) -> bool:
    """Prüft ob E-Mail-Adresse das Tageslimit überschritten hat."""
    from ..models import PublicQuestion

    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    count = PublicQuestion.objects.filter(
        questioner_email=email,
        created_at__gte=today_start,
    ).count()
    return count < max_per_day
