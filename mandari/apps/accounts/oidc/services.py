# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schreibende Abläufe des OpenID-Connect-Anbieters (Issue #1206): Widerruf durch Person und Administration."""

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
