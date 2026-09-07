# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Service für öffentliche Ratsfragen (Abgeordnetenwatch-Stil).

- Mandatsträger:innen-Erkennung (wer darf gefragt werden?)
- Fraktionszuordnung und Antwortquoten (Person, Kommune, Fraktion)
- Moderations-Workflow (Freischalten, Ablehnen, Antwort freischalten)
- E-Mail-Versand (Verifizierung, Benachrichtigungen, Moderation, Erinnerungen)
"""

import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Namen kommunaler Hauptorgane (OParl-Organisationen), deren aktive
#: Mitglieder als Mandatsträger:innen gelten.
COUNCIL_ORG_NAMES = [
    "Rat",
    "Stadtrat",
    "Gemeinderat",
    "Kreistag",
    "Regionalrat",
    "Stadtverordnetenversammlung",
    "Stadtvertretung",
    "Gemeindevertretung",
    "Landschaftsversammlung",
    "Verbandsversammlung",
    "Bezirksvertretung",
]

#: Rollen in Hauptorganen, die Verwaltung/Protokoll statt Mandat bedeuten.
NON_MANDATE_ROLE_HINTS = [
    "dezernent",
    "geschäftsstelle",
    "stadtdirektor",
    "verwaltung",
    "schriftführ",
    "protokoll",
    "zugriff",
    "prüfungsamt",
    "beigeordnet",
    "sachbearbeit",
    "mitarbeiter",
    "referent",
    "gast",
]


def _site_url() -> str:
    return getattr(settings, "SITE_URL", "http://localhost:8000")


# =============================================================================
# Mandat & Fraktion
# =============================================================================


def _faction_org_q(prefix: str = "organization__") -> Q:
    return (
        Q(**{f"{prefix}organization_type__iexact": "Fraktion"})
        | Q(**{f"{prefix}classification__icontains": "fraktion"})
        | Q(**{f"{prefix}name__icontains": "fraktion"})
    )


def _council_org_q(prefix: str = "organization__") -> Q:
    return (
        Q(**{f"{prefix}name__in": COUNCIL_ORG_NAMES})
        | Q(**{f"{prefix}classification__iexact": "Rat"})
        | Q(**{f"{prefix}organization_type__iexact": "Hauptorgan"})
    )


def _active_membership_q() -> Q:
    today = timezone.now().date()
    return Q(end_date__isnull=True) | Q(end_date__gte=today)


def mandate_memberships(person_ref):
    """
    Queryset aktiver Mitgliedschaften, die ein Mandat belegen: Fraktion oder
    Hauptorgan (ohne Verwaltungs-/Protokollrollen). ``person_ref`` kann eine
    Person oder ein OuterRef("pk") sein.
    """
    from ..models import OParlMembership

    faction = _faction_org_q()
    qs = (
        OParlMembership.objects.filter(person=person_ref)
        .filter(_active_membership_q())
        .filter(faction | _council_org_q())
    )
    for hint in NON_MANDATE_ROLE_HINTS:
        qs = qs.exclude(Q(role__icontains=hint) & ~faction)
    return qs.exclude(Q(role="-") & ~faction)


def is_mandate_holder(person) -> bool:
    """Darf diese Person öffentlich befragt werden?"""
    return mandate_memberships(person).exists()


def mandate_holders_queryset(body):
    """Alle aktuellen Mandatsträger:innen einer Kommune."""
    from ..models import OParlPerson

    return (
        OParlPerson.objects.filter(body=body, deleted=False)
        .annotate(has_mandate=Exists(mandate_memberships(OuterRef("pk"))))
        .filter(has_mandate=True)
    )


def get_faction(person):
    """Aktive Fraktion einer Person (OParlOrganization) oder None."""
    from ..models import OParlMembership

    membership = (
        OParlMembership.objects.filter(person=person)
        .filter(_active_membership_q())
        .filter(_faction_org_q())
        .select_related("organization")
        .order_by("organization__name")
        .first()
    )
    return membership.organization if membership else None


def get_faction_map(persons) -> dict:
    """{person_id: OParlOrganization} für viele Personen in einer Abfrage."""
    from ..models import OParlMembership

    ids = {p.id for p in persons}
    if not ids:
        return {}
    result = {}
    memberships = (
        OParlMembership.objects.filter(person_id__in=ids)
        .filter(_active_membership_q())
        .filter(_faction_org_q())
        .select_related("organization")
        .order_by("organization__name")
    )
    for m in memberships:
        result.setdefault(m.person_id, m.organization)
    return result


def get_council_role(person) -> str | None:
    """Rolle im Hauptorgan (z. B. „Ratsmitglied“), falls vorhanden."""
    from ..models import OParlMembership

    m = (
        OParlMembership.objects.filter(person=person)
        .filter(_active_membership_q())
        .filter(_council_org_q())
        .exclude(role__isnull=True)
        .exclude(role="")
        .order_by("organization__name")
        .first()
    )
    return m.role if m else None


# =============================================================================
# Statistiken
# =============================================================================


def _stats_from_questions(questions) -> dict:
    total = len(questions)
    answered = [q for q in questions if q.is_answered]
    open_questions = [q for q in questions if not q.is_answered]
    durations = [q.response_days for q in answered if q.response_days is not None]
    return {
        "total": total,
        "answered": len(answered),
        "open": len(open_questions),
        "rate": round(len(answered) / total * 100) if total else 0,
        "avg_response_days": round(sum(durations) / len(durations)) if durations else None,
        "open_over_14": sum(1 for q in open_questions if q.days_open > 14),
    }


def get_answer_stats(person) -> dict:
    """Antwort-Statistik einer Person (nur veröffentlichte Fragen)."""
    from ..models import PublicQuestion

    questions = list(PublicQuestion.objects.filter(recipient=person, status="published"))
    return _stats_from_questions(questions)


def get_body_stats(body) -> dict:
    """Antwort-Statistik der gesamten Kommune."""
    from ..models import PublicQuestion

    questions = list(PublicQuestion.objects.filter(body=body, status="published"))
    return _stats_from_questions(questions)


def get_faction_ranking(body) -> list[dict]:
    """
    Antwortquote je Fraktion (Abgeordnetenwatch-Stil): alle veröffentlichten
    Fragen der Kommune, gruppiert nach aktiver Fraktion der befragten Person.
    """
    from ..models import PublicQuestion

    questions = list(PublicQuestion.objects.filter(body=body, status="published").select_related("recipient"))
    if not questions:
        return []
    faction_map = get_faction_map([q.recipient for q in questions])
    grouped: dict = defaultdict(list)
    for q in questions:
        org = faction_map.get(q.recipient_id)
        key = org.id if org else None
        grouped[key].append((org, q))

    ranking = []
    for key, entries in grouped.items():
        org = entries[0][0]
        stats = _stats_from_questions([q for _org, q in entries])
        stats["organization"] = org
        stats["name"] = org.short_name or org.name if org else "Ohne Fraktion"
        stats["persons"] = len({q.recipient_id for _org, q in entries})
        ranking.append(stats)
    ranking.sort(key=lambda r: (-r["rate"], -r["total"], r["name"]))
    return ranking


def get_topic_counts(body) -> list[dict]:
    """Themenbereiche mit Anzahl veröffentlichter Fragen (nur belegte)."""
    from ..models import PublicQuestion

    labels = dict(PublicQuestion.TOPIC_CHOICES)
    rows = (
        PublicQuestion.objects.filter(body=body, status="published")
        .values("topic")
        .annotate(n=Count("id"))
        .order_by("-n", "topic")
    )
    return [{"key": r["topic"], "label": labels.get(r["topic"], r["topic"]), "count": r["n"]} for r in rows]


def get_top_recipients(body, limit: int = 5):
    """Mandatsträger:innen mit den meisten veröffentlichten Fragen."""
    from ..models import OParlPerson

    return (
        OParlPerson.objects.filter(body=body, deleted=False)
        .annotate(
            question_count=Count("public_questions", filter=Q(public_questions__status="published")),
            answered_count=Count(
                "public_questions",
                filter=Q(public_questions__status="published", public_questions__answer_status="published"),
            ),
        )
        .filter(question_count__gt=0)
        .order_by("-question_count", "family_name")[:limit]
    )


# =============================================================================
# Moderations-Workflow
# =============================================================================


def publish_question(question, user=None) -> bool:
    """Frage freischalten: veröffentlicht, Ratsmitglied + Fragesteller:in informieren."""
    if question.status != "pending":
        return False
    now = timezone.now()
    question.status = "published"
    question.published_at = now
    question.moderated_by = user if getattr(user, "pk", None) else None
    question.moderated_at = now
    question.save(update_fields=["status", "published_at", "moderated_by", "moderated_at", "updated_at"])
    send_question_notification_to_recipient(question)
    send_question_published_to_questioner(question)
    return True


def reject_question(question, user=None, reason: str = "") -> bool:
    if question.status != "pending":
        return False
    question.status = "rejected"
    question.moderated_by = user if getattr(user, "pk", None) else None
    question.moderated_at = timezone.now()
    if reason:
        question.rejection_reason = reason
    question.save(update_fields=["status", "moderated_by", "moderated_at", "rejection_reason", "updated_at"])
    return True


def publish_answer(question) -> bool:
    """Antwort freischalten und Fragesteller:in informieren."""
    if question.answer_status != "pending":
        return False
    question.answer_status = "published"
    question.save(update_fields=["answer_status", "updated_at"])
    send_answer_notification_to_questioner(question)
    return True


def get_moderator_emails() -> list[str]:
    """Empfänger für Moderations-Hinweise: Einstellung, sonst aktive Superuser."""
    configured = [e for e in getattr(settings, "INSIGHT_MODERATION_EMAILS", []) if e]
    if configured:
        return configured
    from django.contrib.auth import get_user_model

    return list(
        get_user_model()
        .objects.filter(is_superuser=True, is_active=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )


# =============================================================================
# E-Mails
# =============================================================================


def send_verification_email(question) -> bool:
    """Sendet Verifizierungs-E-Mail an Fragesteller:in."""
    from apps.common.email import send_template_email

    site_url = _site_url()
    verify_url = f"{site_url}/insight/fragen/verifizieren/{question.verification_token}/"

    return send_template_email(
        subject=f"Bitte bestätigen Sie Ihre Frage an {question.recipient.display_name}",
        template_name="emails/questions/verification",
        context={"question": question, "verify_url": verify_url, "site_url": site_url},
        to=[question.questioner_email],
        fail_silently=True,
    )


def send_question_notification_to_recipient(question) -> bool:
    """Benachrichtigt Ratsmitglied über freigeschaltete Frage."""
    from apps.common.email import send_template_email

    if not question.recipient.email:
        logger.warning(f"Ratsmitglied {question.recipient} hat keine E-Mail-Adresse.")
        return False

    site_url = _site_url()
    return send_template_email(
        subject=f"Neue Frage von {question.questioner_name}",
        template_name="emails/questions/notification",
        context={
            "question": question,
            "answer_url": f"{site_url}/insight/fragen/antworten/{question.answer_token}/",
            "question_url": f"{site_url}{question.get_absolute_url()}",
            "site_url": site_url,
        },
        to=[question.recipient.email],
        fail_silently=True,
    )


def send_question_published_to_questioner(question) -> bool:
    """Fragesteller:in: Frage ist jetzt öffentlich (mit Link)."""
    from apps.common.email import send_template_email

    site_url = _site_url()
    return send_template_email(
        subject=f"Ihre Frage an {question.recipient.display_name} ist jetzt öffentlich",
        template_name="emails/questions/published",
        context={
            "question": question,
            "question_url": f"{site_url}{question.get_absolute_url()}",
            "site_url": site_url,
        },
        to=[question.questioner_email],
        fail_silently=True,
    )


def send_answer_notification_to_questioner(question) -> bool:
    """Benachrichtigt Fragesteller:in über veröffentlichte Antwort."""
    from apps.common.email import send_template_email

    site_url = _site_url()
    return send_template_email(
        subject=f"{question.recipient.display_name} hat Ihre Frage beantwortet",
        template_name="emails/questions/answer_notification",
        context={
            "question": question,
            "question_url": f"{site_url}{question.get_absolute_url()}",
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

    site_url = _site_url()
    return send_template_email(
        subject=f"Erinnerung: Unbeantwortete Frage von {question.questioner_name}",
        template_name="emails/questions/answer_reminder",
        context={
            "question": question,
            "answer_url": f"{site_url}/insight/fragen/antworten/{question.answer_token}/",
            "site_url": site_url,
        },
        to=[question.recipient.email],
        fail_silently=True,
    )


def send_moderation_notification(question, kind: str = "question") -> bool:
    """Moderator:innen: neue Frage bzw. neue Antwort wartet auf Freigabe."""
    from apps.common.email import send_template_email

    recipients = get_moderator_emails()
    if not recipients:
        logger.info("Keine Moderations-Empfänger konfiguriert — Hinweis-E-Mail entfällt.")
        return False

    site_url = _site_url()
    if kind == "answer":
        subject = f"Antwort wartet auf Freigabe: {question.subject}"
    else:
        subject = f"Neue Ratsfrage wartet auf Freigabe: {question.subject}"
    return send_template_email(
        subject=subject,
        template_name="emails/questions/moderation",
        context={
            "question": question,
            "kind": kind,
            "admin_url": f"{site_url}/admin/insight_core/publicquestion/{question.id}/change/",
            "site_url": site_url,
        },
        to=recipients,
        fail_silently=True,
    )


def send_due_reminders(days: int = 14, repeat_days: int = 14, dry_run: bool = False) -> int:
    """
    Erinnert Ratsmitglieder an offene Fragen: erstmals ``days`` Tage nach
    Veröffentlichung, danach alle ``repeat_days`` Tage. Liefert die Anzahl.
    """
    from ..models import PublicQuestion

    now = timezone.now()
    due = (
        PublicQuestion.objects.filter(status="published", answer_status="none")
        .filter(published_at__lte=now - timedelta(days=days))
        .filter(Q(reminder_sent_at__isnull=True) | Q(reminder_sent_at__lte=now - timedelta(days=repeat_days)))
        .select_related("recipient")
    )
    count = 0
    for question in due:
        if dry_run:
            count += 1
            continue
        if send_answer_reminder(question):
            question.reminder_sent_at = now
            question.save(update_fields=["reminder_sent_at"])
            count += 1
    return count


def check_rate_limit(email: str, max_per_day: int = 3) -> bool:
    """Prüft ob E-Mail-Adresse das Tageslimit überschritten hat."""
    from ..models import PublicQuestion

    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    count = PublicQuestion.objects.filter(questioner_email=email, created_at__gte=today_start).count()
    return count < max_per_day
