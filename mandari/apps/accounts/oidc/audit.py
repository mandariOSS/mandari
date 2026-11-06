# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfprotokoll des OpenID-Connect-Anbieters im Sicherheitsprotokoll (``SecurityAuditLog``, Issue #1206).

Festgehalten werden:

- ``oidc_token``: jede Ausgabe eines Access-Tokens am Token-Endpunkt (Code, Refresh-Token oder Client Credentials)
- ``oidc_widerruf``: jeder Widerruf, ob durch die Anwendung (RFC 7009), die Person („Verbundene Anwendungen“,
  „Alle Sitzungen beenden“), einen Passwortwechsel oder die Plattform-Administration (auch beim Sperren einer
  Anwendung, dann mit Anzahl)
- ``oidc_anwendung``: Anlegen, Ändern und Löschen einer Anwendung im Admin, Anlegen, Sperren und Entsperren mit
  ``manage.py oidc_anwendung`` (``durch``: der Befehl)

Ein Eintrag nennt die Client-ID, den Ablauf und die Scopes, das Konto nur als Referenz. Tokens, Codes und
Secrets stehen nie darin. Ausgabe und Widerruf durch die Anwendung scheitern nicht am Protokoll (wie die
Anmeldung); Änderungen im Admin und mit dem Befehl laufen mit dem Eintrag in einer Transaktion und scheitern mit
ihm.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from django.db import transaction

from apps.common import audit_core

logger = logging.getLogger(__name__)

EVENT_TOKEN = "oidc_token"
EVENT_REVOKED = "oidc_widerruf"
EVENT_APPLICATION = "oidc_anwendung"

#: Wer widerrufen hat (``details["durch"]``)
BY_APPLICATION = "Anwendung"
BY_PERSON = "Person"
BY_ADMINISTRATION = "Plattform-Administration"
#: Passwort geändert oder über den Link aus der E-Mail neu gesetzt
BY_PASSWORD_CHANGE = "Passwortwechsel"
#: Herkunft von Änderungen auf der Kommandozeile (``details["durch"]`` bei ``oidc_anwendung``)
BY_COMMAND = "manage.py oidc_anwendung"


def http_request_of(oauthlib_request: Any) -> Any:
    """Ein Objekt mit ``META`` aus einer oauthlib-Anfrage (die Bibliothek reicht ``request.META`` als Header durch)."""
    headers = getattr(oauthlib_request, "headers", None) or {}
    return SimpleNamespace(META=headers)


def write(event: str, request: Any, *, user_ref: Any = None, **details: Any) -> None:
    """Eintrag schreiben; Fehler gehen an den Aufrufer (für Änderungen im Admin)."""
    from apps.accounts.models import SecurityAuditLog

    ip_address, user_agent = audit_core.get_client_meta(request)
    SecurityAuditLog.objects.create(
        event=event,
        user_ref=user_ref,
        ip_address=ip_address,
        user_agent=user_agent[:300],
        details={key: value for key, value in details.items() if value not in (None, "")},
    )


def record(event: str, request: Any, *, user_ref: Any = None, **details: Any) -> None:
    """Eintrag schreiben, fehlertolerant: Ausgabe und Widerruf scheitern nicht am Protokoll.

    Eigener Sicherungspunkt: Ein Datenbankfehler beim Protokollieren reißt eine umgebende Transaktion
    (etwa den Widerruf) nicht mit.
    """
    try:
        with transaction.atomic():
            write(event, request, user_ref=user_ref, **details)
    except Exception:
        logger.exception("OIDC-Ereignis %s konnte nicht protokolliert werden", event)


def on_app_authorized(sender: Any, request: Any = None, token: Any = None, **kwargs: Any) -> None:
    """Signal ``app_authorized`` der Bibliothek: ein Token wurde am Token-Endpunkt ausgegeben."""
    if token is None:
        return
    application = getattr(token, "application", None)
    record(
        EVENT_TOKEN,
        request,
        user_ref=getattr(token, "user_id", None),
        anwendung=getattr(application, "client_id", ""),
        ablauf=str(getattr(request, "POST", {}).get("grant_type", "")) if request is not None else "",
        scopes=str(getattr(token, "scope", "")),
        # private_key_jwt (RFC 7523, #565); sonst Client-Secret bzw. PKCE ohne Secret
        anmeldung="private_key_jwt" if getattr(application, "uses_private_key_jwt", False) else None,
        bindung=str(getattr(application, "binding", "") or "") or None,
    )


def connect() -> None:
    """Signal-Empfänger verbinden (idempotent über ``dispatch_uid``)."""
    from oauth2_provider.signals import app_authorized

    app_authorized.connect(on_app_authorized, dispatch_uid="oidc_audit_app_authorized")
