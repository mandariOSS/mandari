# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anmeldungen, Abmeldungen und fehlgeschlagene Anmeldungen protokollieren (Issue #221).

Grundlage sind Djangos Signale ``user_logged_in``, ``user_logged_out`` und
``user_login_failed``; sie gelten für jede Anmeldung an der Plattform, egal über welches Portal.
Zusätzlich meldet der zweite Anmeldeschritt einen falschen Code über :func:`log_second_factor_failed`.

Wohin ein Ereignis geschrieben wird:

- Ist das Konto aktiver Nutzer eines oder mehrerer Session-Mandanten, steht das Ereignis im
  Protokoll jedes dieser Mandanten (``SessionAuditLog``, Objekt ist der Session-Nutzer). So sieht
  die Revision eines Mandanten die Anmeldungen ihrer Nutzer.
- Sonst – Work-Portal, Bürgerportal, Plattform-Administration, unbekannte Kennungen – steht es im
  mandantenübergreifenden Sicherheitsprotokoll (:class:`~apps.accounts.models.SecurityAuditLog`).

Datensparsamkeit:

- Das Passwort wird nie gelesen oder gespeichert. Django maskiert es im Signal ohnehin; dieses
  Modul greift nur auf den Anmeldenamen zu.
- Die eingegebene Kennung wird nur bei Fehlversuchen ohne passendes Konto festgehalten, und zwar
  als HMAC-SHA-256 mit einem aus ``SECRET_KEY`` abgeleiteten Schlüssel. Ein einfacher SHA-256
  ließe sich mit einer Liste üblicher Adressen zurückrechnen; der geheime Schlüssel verhindert das,
  gleiche Kennungen bleiben trotzdem erkennbar (z. B. Angriffe auf eine Adresse). Tippt jemand
  versehentlich sein Passwort in das Namensfeld, ist es so ebenfalls nicht lesbar.
- Fehler beim Protokollieren verhindern keine Anmeldung; sie landen im Betriebslog.
"""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.utils.crypto import salted_hmac

from apps.common import audit_core

logger = logging.getLogger(__name__)

#: Salt der Schlüsselableitung für Kennungs-Hashes (nicht ändern: sonst passen alte Hashes nicht)
IDENTIFIER_SALT = "mandari.security_audit.identifier"

#: Gründe eines Fehlversuchs (Klartext im Protokoll)
REASON_CREDENTIALS = "Anmeldedaten ungültig"
REASON_SECOND_FACTOR = "Zweiter Faktor ungültig"


def identifier_hash(identifier: str) -> str:
    """Schlüsselgebundener Hash einer eingegebenen Kennung (normalisiert: getrimmt, klein)."""
    normalized = (identifier or "").strip().lower()
    return salted_hmac(IDENTIFIER_SALT, normalized, algorithm="sha256").hexdigest()


def _session_users(user: Any) -> list[Any]:
    from apps.session.models import SessionUser

    return list(
        SessionUser.objects.filter(user=user, is_active=True, tenant__is_active=True).select_related("tenant", "user")
    )


def _write_global(event: str, request: Any, *, user: Any = None, identifier: str = "", reason: str = "") -> None:
    from apps.accounts.models import SecurityAuditLog

    ip_address, user_agent = audit_core.get_client_meta(request)
    SecurityAuditLog.objects.create(
        event=event,
        user_ref=getattr(user, "pk", None),
        identifier_hash=identifier_hash(identifier) if identifier and user is None else "",
        ip_address=ip_address,
        user_agent=user_agent[:300],
        details={"grund": reason} if reason else {},
    )


def record(event: str, request: Any, *, user: Any = None, identifier: str = "", reason: str = "") -> None:
    """Ein Anmeldeereignis schreiben (Mandantenprotokoll oder Sicherheitsprotokoll), fehlertolerant."""
    try:
        session_users = _session_users(user) if user is not None else []
        if not session_users:
            _write_global(event, request, user=user, identifier=identifier, reason=reason)
            return
        from apps.session import audit as session_audit

        for session_user in session_users:
            session_audit.log_event(
                event,
                session_user,
                tenant=session_user.tenant,
                # Beim Fehlversuch ist nicht belegt, wer es war – nur das betroffene Konto
                user=None if event == "login_failed" else session_user,
                request=request,
                changes={"grund": reason} if reason else None,
            )
    except Exception:
        # Anmeldung und Abmeldung dürfen am Protokoll nicht scheitern
        logger.exception("Anmeldeereignis %s konnte nicht protokolliert werden", event)


def log_second_factor_failed(request: Any, user: Any) -> None:
    """Falscher Code im zweiten Anmeldeschritt (Passwort war richtig)."""
    record("login_failed", request, user=user, reason=REASON_SECOND_FACTOR)


# =============================================================================
# Signal-Empfänger (in AccountsConfig.ready verbunden)
# =============================================================================


def on_user_logged_in(sender: Any, request: Any = None, user: Any = None, **kwargs: Any) -> None:
    if user is not None:
        record("login", request, user=user)


def on_user_logged_out(sender: Any, request: Any = None, user: Any = None, **kwargs: Any) -> None:
    if user is not None and getattr(user, "is_authenticated", False):
        record("logout", request, user=user)


def on_user_login_failed(sender: Any, credentials: Any = None, request: Any = None, **kwargs: Any) -> None:
    """Fehlversuch: nur der Anmeldename wird gelesen, nie das (maskierte) Passwort."""
    from apps.accounts.models import User

    credentials = credentials if isinstance(credentials, dict) else {}
    identifier = str(credentials.get("username") or credentials.get("email") or "")[:320]
    user = User.objects.filter(email__iexact=identifier.strip()).first() if identifier.strip() else None
    record("login_failed", request, user=user, identifier=identifier, reason=REASON_CREDENTIALS)


def connect() -> None:
    """Signal-Empfänger verbinden (idempotent über dispatch_uid)."""
    user_logged_in.connect(on_user_logged_in, dispatch_uid="security_audit_logged_in")
    user_logged_out.connect(on_user_logged_out, dispatch_uid="security_audit_logged_out")
    user_login_failed.connect(on_user_login_failed, dispatch_uid="security_audit_login_failed")
