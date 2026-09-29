# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliches Beschluss-Tracking „Was wurde aus …?“ (Issue #48).

Die Verwaltung pflegt im Session-RIS den Umsetzungsstand ihrer Beschlüsse
(Issue #37). Schaltet sie „Umsetzungsstand im Bürgerportal veröffentlichen“
ein, erscheinen angenommene, öffentliche Beschlüsse mit Status-Zeitleiste und
öffentlicher Statusmeldung in Insight. Bürger:innen können einzelne Beschlüsse
abonnieren und erhalten eine E-Mail, wenn sich der Stand ändert.

Es werden ausschließlich Daten gezeigt, die die Verwaltung dafür freigegeben
hat: öffentliche Sitzung, öffentlicher TOP, Beschluss angenommen, Schalter am
Mandanten und am Beschluss aktiv. Interne Erledigungsvermerke bleiben intern —
nach außen geht nur die separate öffentliche Statusmeldung.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.common.email import render_email

logger = logging.getLogger(__name__)

PUBLIC_STATUS_LABELS = {
    "open": "Beschlossen, Umsetzung steht an",
    "in_progress": "In Umsetzung",
    "done": "Umgesetzt",
    "deferred": "Zurückgestellt",
}


#: Sichtbarkeit eines Beschlusses bzw. der Beschlussseiten einer Kommune (Issue #618)
VISIBLE = "visible"  # veröffentlicht: lesbar, Abos und Benachrichtigungen
ARCHIVED = "archived"  # Archiv: lesbar mit Hinweis, keine Abos und Benachrichtigungen mehr
PAUSED = "paused"  # vorübergehend abgeschaltet: Hinweis statt Inhalt (503)
WITHDRAWN = "withdrawn"  # dauerhaft zurückgenommen: nicht mehr verfügbar (410)
HIDDEN = "hidden"  # nie oder nicht (mehr) freigegeben: 404


def _tenant_visibility(tenant: Any) -> str:
    """Beschlussseiten eines Mandanten folgen der Veröffentlichung im Bürgerportal (Issue #618)."""
    if not (tenant.is_active and tenant.implementation_publish):
        return HIDDEN
    if tenant.insight_publish:
        return VISIBLE
    return {
        tenant.PORTAL_END_ARCHIVED: ARCHIVED,
        tenant.PORTAL_END_PAUSED: PAUSED,
        tenant.PORTAL_END_WITHDRAWN: WITHDRAWN,
    }.get(tenant.insight_end_mode or "", HIDDEN)


def publishing_tenants(body: Any) -> Any:
    """Mandanten, deren Beschlüsse lesbar sind: veröffentlichend oder als Archiv."""
    from django.db.models import Q

    from apps.session.models import SessionTenant

    return SessionTenant.objects.filter(oparl_body=body, is_active=True, implementation_publish=True).filter(
        Q(insight_publish=True) | Q(insight_end_mode=SessionTenant.PORTAL_END_ARCHIVED)
    )


def body_visibility(body: Any) -> str:
    """Beschlussseiten einer Kommune: lesbar, Archiv, abgeschaltet, zurückgenommen oder nichts."""
    from apps.session.models import SessionTenant

    found = {
        _tenant_visibility(tenant)
        for tenant in SessionTenant.objects.filter(oparl_body=body, is_active=True, implementation_publish=True)
    }
    for visibility in (VISIBLE, ARCHIVED, PAUSED, WITHDRAWN):
        if visibility in found:
            return visibility
    return HIDDEN


def public_decisions(body: Any) -> Any:
    """Öffentlich sichtbare Beschlüsse einer Kommune (nur mit Freigabe der Verwaltung)."""
    from apps.session.models import SessionAgendaItem

    return (
        SessionAgendaItem.objects.filter(
            meeting__tenant__in=publishing_tenants(body),
            vote_result="approved",
            is_public=True,
            meeting__is_public=True,
            implementation_public=True,
        )
        .exclude(is_withdrawn=True)
        .select_related("meeting__organization", "meeting__tenant", "paper")
        .order_by("-meeting__start", "order")
    )


def visibility(item: Any) -> str:
    """Sichtbarkeit eines Beschlusses (``VISIBLE``, ``ARCHIVED``, ``PAUSED``, ``WITHDRAWN``, ``HIDDEN``)."""
    tenant = item.meeting.tenant
    if not (
        tenant.oparl_body_id
        and item.vote_result == "approved"
        and item.is_public
        and item.meeting.is_public
        and item.implementation_public
        and not item.is_withdrawn
    ):
        return HIDDEN
    return _tenant_visibility(tenant)


def is_readable(item: Any) -> bool:
    """Seite lesbar (veröffentlicht oder Archiv)."""
    return visibility(item) in (VISIBLE, ARCHIVED)


def is_publicly_visible(item: Any) -> bool:
    """Aktiv veröffentlicht: Abos und Benachrichtigungen (im Archiv nicht mehr)."""
    return visibility(item) == VISIBLE


def stats(body) -> dict:
    qs = public_decisions(body)
    counts = {status: qs.filter(implementation_status=status).count() for status, _ in PUBLIC_STATUS_LABELS.items()}
    counts["total"] = qs.count()
    counts["overdue"] = (
        qs.filter(implementation_deadline__lt=timezone.localdate()).exclude(implementation_status="done").count()
    )
    return counts


def timeline(item) -> list[dict]:
    """Status-Zeitleiste: beschlossen → in Umsetzung → umgesetzt (bzw. zurückgestellt)."""
    status = item.implementation_status
    decided_at = item.meeting.start
    updated = item.implementation_updated_at
    steps = [
        {"key": "decided", "label": "Beschlossen", "date": decided_at, "reached": True, "current": status == "open"},
        {
            "key": "in_progress",
            "label": "In Umsetzung",
            "date": updated if status in ("in_progress", "done") else None,
            "reached": status in ("in_progress", "done"),
            "current": status == "in_progress",
        },
        {
            "key": "done",
            "label": "Umgesetzt",
            "date": updated if status == "done" else None,
            "reached": status == "done",
            "current": status == "done",
        },
    ]
    if status == "deferred":
        steps.append({"key": "deferred", "label": "Zurückgestellt", "date": updated, "reached": True, "current": True})
    return steps


def public_url(item) -> str:
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000").rstrip("/")
    return f"{site_url}/insight/beschluesse/{item.id}/"


# =============================================================================
# Abos
# =============================================================================


def _from_email() -> str:
    return getattr(settings, "INSIGHT_DIGEST_FROM_EMAIL", "") or getattr(
        settings, "DEFAULT_FROM_EMAIL", "noreply@mandari.de"
    )


def _send(subject: str, template: str, context: dict, recipient: str) -> bool:
    try:
        html_message, text_message = render_email(template, context)
        send_mail(
            subject=subject,
            message=text_message,
            from_email=_from_email(),
            recipient_list=[recipient],
            html_message=html_message,
        )
        return True
    except Exception as exc:  # Mailfehler dürfen die Seite nicht brechen
        logger.warning("Beschluss-Abo-Mail an %s fehlgeschlagen: %s", recipient, exc)
        return False


def send_confirmation(subscription) -> bool:
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000").rstrip("/")
    confirm_url = f"{site_url}/insight/beschluesse/abo/bestaetigen/{subscription.token}/"
    item = subscription.agenda_item
    return _send(
        subject="Bitte bestätigen: Benachrichtigung zum Beschluss",
        template="emails/decisions/confirm.html",
        context={"item": item, "confirm_url": confirm_url, "site_url": site_url, "decision_url": public_url(item)},
        recipient=subscription.email,
    )


def notify_status_change(item, old_status: str | None, old_public_note: str | None) -> int:
    """Alle bestätigten Abonnent:innen über einen neuen Umsetzungsstand informieren."""
    from ..models import DecisionSubscription

    if not is_publicly_visible(item):
        return 0
    status_changed = old_status is not None and old_status != item.implementation_status
    note_changed = (
        old_public_note is not None
        and (old_public_note or "").strip() != (item.implementation_public_note or "").strip()
    )
    if not (status_changed or note_changed):
        return 0

    subscriptions = DecisionSubscription.objects.filter(agenda_item=item, confirmed=True, unsubscribed_at__isnull=True)
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000").rstrip("/")
    label = PUBLIC_STATUS_LABELS.get(item.implementation_status, item.get_implementation_status_display())
    sent = 0
    for subscription in subscriptions:
        ok = _send(
            subject=f"Neuer Stand: {label} — {item.name}"[:150],
            template="emails/decisions/status.html",
            context={
                "item": item,
                "status_label": label,
                "status_changed": status_changed,
                "decision_url": public_url(item),
                "unsubscribe_url": f"{site_url}/insight/beschluesse/abo/abmelden/{subscription.token}/",
                "site_url": site_url,
            },
            recipient=subscription.email,
        )
        sent += 1 if ok else 0
    return sent
