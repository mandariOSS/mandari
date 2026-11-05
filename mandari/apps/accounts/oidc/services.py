# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schreibende Abläufe des OpenID-Connect-Anbieters: Widerruf durch Person und Administration (Issue #1206), Sperren
einer Anwendung mit Widerruf aller ihrer Tokens (Issue #1215).
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from . import audit
from .models import OAuthAccessToken, OAuthApplication, OAuthGrant, OAuthIDToken, OAuthRefreshToken


@transaction.atomic
def revoke_for_user(request: Any, user: Any, application_id: Any, *, by: str = audit.BY_PERSON) -> bool:
    """
    Alle Tokens und offenen Codes einer Anwendung für diese Person widerrufen.

    Refresh-Tokens bleiben als widerrufene Einträge stehen (sonst erkennte die Bibliothek die erneute Nutzung
    eines rotierten Tokens nicht mehr), Access-Tokens, ID-Tokens und Codes werden gelöscht.

    Returns: ``True``, wenn es etwas zu widerrufen gab.
    """
    application = OAuthApplication.objects.filter(pk=application_id).first()
    if application is None:
        return False
    refresh_tokens = list(
        OAuthRefreshToken.objects.select_for_update().filter(user=user, application=application, revoked__isnull=True)
    )
    for refresh_token in refresh_tokens:
        refresh_token.revoke()
    access_deleted, _ = OAuthAccessToken.objects.filter(user=user, application=application).delete()
    OAuthIDToken.objects.filter(user=user, application=application).delete()
    OAuthGrant.objects.filter(user=user, application=application).delete()
    if not refresh_tokens and not access_deleted:
        return False
    # Der Widerruf gilt auch, wenn das Protokoll gerade nicht geschrieben werden kann
    audit.record(
        audit.EVENT_REVOKED,
        request,
        user_ref=getattr(user, "pk", None),
        anwendung=application.client_id,
        durch=by,
    )
    return True


@transaction.atomic
def revoke_application(request: Any, application: Any, *, by: str = audit.BY_ADMINISTRATION, **details: Any) -> int:
    """
    Alle Tokens und offenen Codes einer Anwendung widerrufen, für alle Personen und für den Dienst selbst.

    Wie :func:`revoke_for_user`: Refresh-Tokens bleiben als widerrufene Einträge stehen, Access-Tokens, ID-Tokens
    und Codes werden gelöscht. Nach dem Sperren einer Anwendung (``is_active``) lebt so auch beim Entsperren kein
    alter Zugang wieder auf. Der Eintrag im Prüfprotokoll nennt nur Anzahlen.

    Returns: Anzahl widerrufener Access- und Refresh-Tokens.
    """
    refresh_tokens = list(
        OAuthRefreshToken.objects.select_for_update().filter(application=application, revoked__isnull=True)
    )
    # Vor dem Widerruf zählen: Ein widerrufenes Refresh-Token nimmt sein Access-Token mit
    access_count = int(OAuthAccessToken.objects.filter(application=application).count())
    for refresh_token in refresh_tokens:
        refresh_token.revoke()
    OAuthAccessToken.objects.filter(application=application).delete()
    OAuthIDToken.objects.filter(application=application).delete()
    OAuthGrant.objects.filter(application=application).delete()
    revoked = len(refresh_tokens) + access_count
    if revoked:
        audit.record(
            audit.EVENT_REVOKED,
            request,
            anwendung=application.client_id,
            durch=by,
            anzahl=revoked,
            **details,
        )
    return revoked
