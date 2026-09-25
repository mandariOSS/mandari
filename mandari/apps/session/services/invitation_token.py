# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Signierter Rückmeldelink zur Ladung (Issue #225).

Jeder Ladungsempfänger (SessionInvitationRecipient) bekommt einen persönlichen Link, über den
er ohne Anmeldung den Erhalt bestätigt und Zusage, Absage oder Vertretungswunsch meldet.

Token-Design:
- Inhalt ``<empfänger-uuid>.<sitzungs-uuid>.<link-schlüssel>``, signiert mit
  ``TimestampSigner`` (HMAC-SHA256 über SECRET_KEY) und eigenem Salt – das Token taugt nur
  für diese Rückmeldung, nicht für andere signierte Werte im Projekt.
- Gebunden an Empfänger UND Sitzung: Beide IDs stehen im signierten Wert und werden beim
  Einlösen gegen die Datenbank geprüft.
- Der Link-Schlüssel (``response_nonce``) liegt nur in der Datenbank. Wer ihn neu setzt,
  macht alle bisherigen Links dieses Empfängers ungültig; ein bekannt gewordener SECRET_KEY
  allein genügt nicht, um Links zu fälschen.
- Ablauf: höchstens ``MAX_AGE`` nach Ausstellung und in jedem Fall mit Sitzungsbeginn.
- Missbrauchsschutz: Ratenbegrenzung je IP-Adresse über den Cache (Muster wie bei der
  Selbstregistrierung und der OParl-API); ungültige Tokens zählen strenger.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from django.conf import settings
from django.core.cache import cache
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from apps.accounts.two_factor_policy import client_ip
from apps.session.models import SessionInvitationRecipient

SALT = "mandari.session.ladung.rueckmeldung"
MAX_AGE = timedelta(days=120)

# Ratenbegrenzung je IP: alle Aufrufe des Rückmeldelinks bzw. nur ungültige Tokens
RATE_LIMIT_REQUESTS = 60
RATE_LIMIT_REQUESTS_WINDOW = 10 * 60
RATE_LIMIT_FAILURES = 20
RATE_LIMIT_FAILURES_WINDOW = 60 * 60

TokenState = Literal["ok", "invalid", "expired", "cancelled", "closed"]


@dataclass(frozen=True)
class TokenCheck:
    """Ergebnis der Token-Prüfung; ``recipient`` ist nur bei ok/cancelled/closed gesetzt."""

    state: TokenState
    recipient: SessionInvitationRecipient | None = None

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=SALT)


def make_token(recipient: SessionInvitationRecipient) -> str:
    """Signiertes Rückmelde-Token für einen Ladungsempfänger."""
    meeting_id: uuid.UUID = recipient.dispatch.meeting_id
    return _signer().sign(f"{recipient.pk.hex}.{meeting_id.hex}.{recipient.response_nonce}")


def response_url(recipient: SessionInvitationRecipient) -> str:
    """Absolute Adresse des Rückmeldelinks (für Mails, Serienbriefe und CSV)."""
    base_url = str(getattr(settings, "SITE_URL", "https://mandari.de")).rstrip("/")
    return f"{base_url}{reverse('session_invitation_response', kwargs={'token': make_token(recipient)})}"


def check_token(token: str) -> TokenCheck:
    """
    Token prüfen und den Empfänger laden.

    Zustände: ``invalid`` (Signatur, Aufbau, Empfänger, Sitzung oder Link-Schlüssel passen
    nicht), ``expired`` (älter als MAX_AGE), ``cancelled`` (Sitzung abgesagt), ``closed``
    (Sitzung hat begonnen) und ``ok``.
    """
    try:
        value = _signer().unsign(token, max_age=MAX_AGE)
    except SignatureExpired:
        return TokenCheck("expired")
    except BadSignature:
        return TokenCheck("invalid")

    parts = value.split(".")
    if len(parts) != 3:
        return TokenCheck("invalid")
    try:
        recipient_id = uuid.UUID(hex=parts[0])
        meeting_id = uuid.UUID(hex=parts[1])
    except ValueError:
        return TokenCheck("invalid")

    recipient = (
        SessionInvitationRecipient.objects.select_related(
            "dispatch__meeting__organization", "dispatch__meeting__tenant", "person", "substitute_for"
        )
        .filter(pk=recipient_id, dispatch__meeting_id=meeting_id)
        .first()
    )
    if (
        recipient is None
        or recipient.person is None
        or not recipient.person.is_active
        or not constant_time_compare(recipient.response_nonce, parts[2])
        or not recipient.dispatch.meeting.tenant.is_active
    ):
        return TokenCheck("invalid")

    meeting = recipient.dispatch.meeting
    if meeting.cancelled or meeting.meeting_state == "cancelled":
        return TokenCheck("cancelled", recipient)
    if timezone.now() >= meeting.start:
        return TokenCheck("closed", recipient)
    return TokenCheck("ok", recipient)


def _count(key: str, window: int) -> int:
    """Fixed-Window-Zähler im Cache (atomar genug für ein Soft-Limit)."""
    if cache.add(key, 1, timeout=window):
        return 1
    try:
        return int(cache.incr(key))
    except ValueError:  # Schlüssel zwischenzeitlich abgelaufen
        cache.add(key, 1, timeout=window)
        return 1


def rate_limited(request: HttpRequest) -> bool:
    """Zu viele Aufrufe des Rückmeldelinks von dieser IP-Adresse?"""
    ip = client_ip(request) or "unbekannt"
    failures = cache.get(f"session-ladung:fehler:{ip}", 0)
    if int(failures or 0) >= RATE_LIMIT_FAILURES:
        return True
    return _count(f"session-ladung:aufrufe:{ip}", RATE_LIMIT_REQUESTS_WINDOW) > RATE_LIMIT_REQUESTS


def record_failure(request: HttpRequest) -> None:
    """Ungültiges Token von dieser IP-Adresse zählen (Schutz vor Durchprobieren)."""
    ip = client_ip(request) or "unbekannt"
    _count(f"session-ladung:fehler:{ip}", RATE_LIMIT_FAILURES_WINDOW)
