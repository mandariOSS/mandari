# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Automatischer Versand des Protokolls (Issue #871).

Organisationseinstellung ``faction.protocol_dispatch`` (Standard ``off``):

- ``after_completion``: eine einstellbare Zahl Stunden nach dem Ende einer abgeschlossenen Sitzung
- ``after_approval``: eine einstellbare Zahl Stunden nach der Genehmigung des Protokolls

Empfänger sind die aktiven Mitglieder (ohne Gäste) mit Leserecht für das Protokoll, über den
Versandweg der Organisation. Die Fassung folgt denselben Regeln wie der PDF-Download
(``views/exports.py``): Vereidigte mit ``protocols.view_full`` erhalten die interne Fassung samt
nichtöffentlichem Teil, alle anderen mit ``protocols.view_public`` die öffentliche.

Beim Einschalten merkt sich die Organisation den Zeitpunkt (``protocol_dispatch_since``): Versendet
wird nur, was danach fällig wird – ältere Protokolle gehen nicht nachträglich raus. Je Sitzung höchstens
einmal (``protocol_sent_at``, vor dem Senden in der Datenbank beansprucht). Der Lauf hängt als Zeitplan
``fraktionsprotokolle_versenden`` im Worker (``apps/work/schedules.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

PROTOCOL_DISPATCH_MODES = ("off", "after_completion", "after_approval")
PROTOCOL_DISPATCH_DEFAULTS: dict[str, Any] = {
    "protocol_dispatch": "off",
    "protocol_dispatch_delay_hours": 24,
}
PROTOCOL_DISPATCH_MAX_DELAY_HOURS = 24 * 30

_PROTOCOL_LOCK_KEY = "faction:protocol:lock"
_PROTOCOL_LOCK_TIMEOUT = 10 * 60


def get_protocol_dispatch_settings(organization: Any) -> dict[str, Any]:
    """Einstellungen des Protokollversands der Organisation (mit Standardwerten)."""
    faction_settings = (organization.settings or {}).get("faction", {})
    result: dict[str, Any] = dict(PROTOCOL_DISPATCH_DEFAULTS)
    mode = faction_settings.get("protocol_dispatch")
    if mode in PROTOCOL_DISPATCH_MODES:
        result["protocol_dispatch"] = mode
    try:
        delay = int(faction_settings.get("protocol_dispatch_delay_hours", result["protocol_dispatch_delay_hours"]))
        result["protocol_dispatch_delay_hours"] = max(0, min(delay, PROTOCOL_DISPATCH_MAX_DELAY_HOURS))
    except (TypeError, ValueError):
        pass
    since = faction_settings.get("protocol_dispatch_since")
    result["protocol_dispatch_since"] = parse_datetime(since) if isinstance(since, str) else None
    return result


def protocol_due_at(meeting: Any, settings: dict[str, Any]) -> datetime | None:
    """Zeitpunkt, ab dem das Protokoll versendet wird, oder ``None`` (aus bzw. noch nicht so weit)."""
    mode = settings["protocol_dispatch"]
    delay = timedelta(hours=settings["protocol_dispatch_delay_hours"])
    base: datetime | None = None
    if mode == "after_completion" and meeting.status == "completed":
        base = meeting.end or meeting.start
    elif mode == "after_approval" and meeting.protocol_approved:
        base = meeting.protocol_approved_at
    return base + delay if base is not None else None


def protocol_recipients(meeting: Any) -> list[tuple[Any, bool]]:
    """Empfänger mit Fassung: ``(membership, intern)`` – wie beim PDF-Download der Niederschrift."""
    from .visibility import can_view_internal

    memberships = meeting.organization.memberships.filter(is_active=True, is_guest=False).select_related("user")
    recipients = []
    for membership in memberships:
        if not membership.has_permission("faction.view_public"):
            continue
        if can_view_internal(membership) and membership.has_permission("protocols.view_full"):
            recipients.append((membership, True))
        elif membership.has_permission("protocols.view_public"):
            recipients.append((membership, False))
    return recipients


def send_protocol(meeting: Any) -> int:
    """
    Protokoll als PDF an die Empfänger senden (je Fassung einmal erzeugt).

    Returns:
        Anzahl versendeter E-Mails.
    """
    from apps.common import mail
    from apps.common.email import render_email

    from .services import FactionMeetingEmailService, build_faction_protocol_pdf

    recipients = protocol_recipients(meeting)
    if not recipients:
        return 0
    pdfs: dict[bool, bytes] = {}
    meeting_url = FactionMeetingEmailService().get_meeting_url(meeting)
    date = timezone.localtime(meeting.start, timezone.get_default_timezone()).strftime("%Y-%m-%d")

    sent = 0
    for membership, internal in recipients:
        user = membership.user
        if not user.email:
            continue
        if internal not in pdfs:
            pdfs[internal] = build_faction_protocol_pdf(meeting, internal=internal)
        variant = "intern" if internal else "oeffentlich"
        context = {
            "meeting": meeting,
            "organization": meeting.organization,
            "user": user,
            "internal": internal,
            "meeting_url": meeting_url,
        }
        try:
            html_body, text_body = render_email("work/faction/email/protocol.html", context)
            if mail.send(
                kind="work.fraktion.protokoll",
                organization=meeting.organization,
                subject=f"Protokoll: {meeting.title}",
                body=text_body,
                html_body=html_body,
                to=[user.email],
                attachments=[(f"niederschrift-{date}-{variant}.pdf", pdfs[internal], "application/pdf")],
                fail_silently=True,
            ):
                sent += 1
        except Exception:
            # Eine Adresse darf die übrigen nicht aufhalten; das Log nennt keine Empfänger
            logger.exception("Protokollversand an ein Mitglied fehlgeschlagen (meeting=%s)", meeting.id)
    return sent


def _due_window(organization: Any, settings: dict[str, Any], now: datetime) -> Any:
    """
    Sitzungen der Organisation, deren Protokoll im Fenster ``[Einschalten, jetzt]`` fällig wird.

    Gefiltert wird in der Datenbank: fällig = Bezugszeitpunkt + Verzögerung, also Bezugszeitpunkt zwischen
    Einschalten minus Verzögerung und jetzt minus Verzögerung. Ältere Sitzungen (vor dem Einschalten)
    bekommen nie ``protocol_sent_at`` und würden sonst bei jedem Lauf erneut geladen.
    """
    from django.db.models.functions import Coalesce

    from .models import FactionMeeting

    since = settings["protocol_dispatch_since"]
    if since is None:
        return FactionMeeting.objects.none()
    delay = timedelta(hours=settings["protocol_dispatch_delay_hours"])
    meetings = FactionMeeting.objects.filter(
        organization=organization, status="completed", protocol_sent_at__isnull=True
    )
    if settings["protocol_dispatch"] == "after_completion":
        return meetings.annotate(versand_bezug=Coalesce("end", "start")).filter(
            versand_bezug__gte=since - delay, versand_bezug__lte=now - delay
        )
    if settings["protocol_dispatch"] == "after_approval":
        return meetings.filter(
            protocol_approved=True,
            protocol_approved_at__gte=since - delay,
            protocol_approved_at__lte=now - delay,
        )
    return FactionMeeting.objects.none()


def _candidates(now: datetime) -> Iterator[tuple[Any, dict[str, Any]]]:
    """Kandidaten je eingeschalteter Organisation, mit deren Einstellungen."""
    from apps.tenants.models import Organization

    organizations = Organization.objects.filter(
        is_active=True,
        settings__faction__protocol_dispatch__in=["after_completion", "after_approval"],
    )
    for organization in organizations:
        settings = get_protocol_dispatch_settings(organization)
        for meeting in _due_window(organization, settings, now):
            meeting.organization = organization
            yield meeting, settings


def run_faction_protocol_pass(now: datetime | None = None) -> dict[str, Any]:
    """
    Periodischer Protokollversand (Zeitplan ``fraktionsprotokolle_versenden``).

    Versendet je Sitzung höchstens einmal, sobald der eingestellte Zeitpunkt erreicht ist und er nach
    dem Einschalten liegt. Ein Cache-Lock verhindert parallele Läufe, der Anspruch in der Datenbank
    doppelte Mails.

    Returns:
        Statistik-Dict (meetings, sent bzw. skipped-Grund).
    """
    from django.core.cache import cache

    from .audit import log_event
    from .models import FactionMeeting

    now = now or timezone.now()
    if not cache.add(_PROTOCOL_LOCK_KEY, "1", timeout=_PROTOCOL_LOCK_TIMEOUT):
        return {"skipped": "lock"}

    try:
        stats = {"meetings": 0, "sent": 0}
        for meeting, settings in _candidates(now):
            due_at = protocol_due_at(meeting, settings)
            since = settings["protocol_dispatch_since"]
            if due_at is None or due_at > now or since is None or due_at < since:
                continue
            claimed = FactionMeeting.objects.filter(pk=meeting.pk, protocol_sent_at__isnull=True).update(
                protocol_sent_at=now
            )
            if not claimed:
                continue
            try:
                sent = send_protocol(meeting)
            except Exception:
                # Versand als Ganzes gescheitert (z. B. PDF): Anspruch zurückgeben, nächster Lauf versucht es erneut
                logger.exception("Protokollversand fehlgeschlagen (meeting=%s)", meeting.id)
                FactionMeeting.objects.filter(pk=meeting.pk, protocol_sent_at=now).update(protocol_sent_at=None)
                continue
            meeting.protocol_sent_at = now
            log_event("protocol_sent", meeting, is_internal=False, changes={"empfaenger": sent})
            stats["meetings"] += 1
            stats["sent"] += sent

        if stats["meetings"]:
            logger.info("Fraktions-Protokollversand: %d Sitzung(en), %d E-Mail(s)", stats["meetings"], stats["sent"])
        return stats
    finally:
        cache.delete(_PROTOCOL_LOCK_KEY)
