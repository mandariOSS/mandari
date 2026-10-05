# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sicherheitshinweise an die Person, der das Konto gehört.

Jede sicherheitsrelevante Änderung am eigenen Konto legt einen Eintrag an
(:class:`~apps.accounts.models.SecurityNotification`); die Sicherheitsseite im Konto zeigt die
letzten Einträge. Wichtige Ereignisse gehen zusätzlich per E-Mail an die Adresse des Kontos,
damit eine Änderung durch Dritte auffällt:

- Passwort geändert oder über den Link zurückgesetzt
- zweiter Faktor eingerichtet, abgeschaltet oder von der Administration zurückgesetzt
- Sicherheitsschlüssel/Passkey hinzugefügt oder entfernt

Beendete Sitzungen erscheinen nur auf der Seite (die Person hat sie dort selbst beendet).

Versand nach dem Commit über den Mail-Dienst auf dem Weg der Plattform (Mailart ``konto.sicherheit``). Fehler beim Anlegen oder
Versenden verhindern die Änderung nie; sie landen im Betriebslog.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone

from .device_names import device_name
from .models import SecurityNotification

logger = logging.getLogger(__name__)

PASSWORD_CHANGED = "password_changed"
TWO_FACTOR_ENABLED = "2fa_enabled"
TWO_FACTOR_DISABLED = "2fa_disabled"
DEVICE_ADDED = "device_added"
DEVICE_REMOVED = "device_removed"
SESSION_REVOKED = "session_revoked"

#: Ereignisse, die zusätzlich per E-Mail gemeldet werden
MAIL_TYPES = frozenset({PASSWORD_CHANGED, TWO_FACTOR_ENABLED, TWO_FACTOR_DISABLED, DEVICE_ADDED, DEVICE_REMOVED})

#: So viele Hinweise zeigt die Sicherheitsseite
RECENT_LIMIT = 10


def notify(user: Any, notification_type: str, title: str, message: str, *, request: Any = None) -> None:
    """Hinweis anlegen und – bei wichtigen Ereignissen – nach dem Commit per E-Mail melden."""
    try:
        ip_address, device_info = _client(request)
        notification = SecurityNotification.objects.create(
            user=user,
            notification_type=notification_type,
            title=title,
            message=message,
            ip_address=ip_address,
            device_info=device_info,
        )
    except Exception:
        logger.exception("Sicherheitshinweis %s konnte nicht angelegt werden", notification_type)
        return
    email = str(getattr(user, "email", None) or "")
    if notification_type in MAIL_TYPES and email:
        notification_id = notification.pk
        transaction.on_commit(lambda: send_mail(notification_id))


def send_mail(notification_id: Any) -> bool:
    """Hinweis-Mail versenden und ``email_sent`` setzen; ``False`` bei Fehlschlag (nie eine Ausnahme)."""
    from apps.common import mail
    from apps.common.email import render_email

    try:
        notification = SecurityNotification.objects.select_related("user").get(pk=notification_id)
        user = notification.user
        site_url = str(getattr(settings, "SITE_URL", "")).rstrip("/")
        context = {
            "notification": notification,
            "name": user.get_full_name(),
            "occurred_at": timezone.localtime(notification.created_at),
            "reset_url": f"{site_url}{reverse('accounts:password_reset')}",
        }
        html, text = render_email("accounts/emails/security_notification.html", context)
        sent = mail.send(
            kind="konto.sicherheit",
            subject=f"Sicherheitshinweis: {notification.title}",
            body=text,
            html_body=html,
            to=[user.email],
            # Ein Hinweis ist ein Ereignis für eine Person: höchstens eine Mail, auch bei Wiederholung
            idempotency_key=f"konto.sicherheit:{notification.pk}",
            fail_silently=True,
        )
    except Exception:
        logger.exception("Sicherheitshinweis konnte nicht per E-Mail versendet werden")
        return False
    if sent:
        SecurityNotification.objects.filter(pk=notification.pk).update(email_sent=True)
    return sent


def recent_for(user: Any, limit: int = RECENT_LIMIT) -> list[SecurityNotification]:
    """Die letzten Hinweise des Kontos (neueste zuerst)."""
    queryset: QuerySet[SecurityNotification] = SecurityNotification.objects.filter(user=user).order_by("-created_at")
    return list(queryset[:limit])


def mark_read(user: Any, notifications: list[SecurityNotification]) -> None:
    """Angezeigte, noch ungelesene Hinweise als gelesen markieren."""
    unread = [n.pk for n in notifications if not n.is_read]
    if unread:
        SecurityNotification.objects.filter(user=user, pk__in=unread).update(is_read=True, read_at=timezone.now())


def _client(request: Any) -> tuple[str | None, str]:
    """IP-Adresse und grobe Gerätebezeichnung der auslösenden Anfrage.

    Ohne übergebene Anfrage gilt die laufende (``OrganizationMiddleware`` hält sie für jede
    Anfrage bereit); außerhalb einer Anfrage, etwa in Befehlen, bleiben beide leer.
    """
    from apps.common.audit_core import get_client_meta, get_current_request

    request = request if request is not None else get_current_request()
    if request is None:
        return None, ""
    ip_address, _user_agent = get_client_meta(request)
    return ip_address, device_name(request)[:200]
