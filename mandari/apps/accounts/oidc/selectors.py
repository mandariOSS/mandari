# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lesende Abfragen des OpenID-Connect-Anbieters (Issue #1206): verbundene Anwendungen einer Person."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.utils import timezone

from . import access, catalog
from .models import OAuthAccessToken, OAuthApplication, OAuthRefreshToken


@dataclass(frozen=True)
class ConnectedApplication:
    """Eine Anwendung, die gerade im Namen der Person handeln darf."""

    application_id: int
    name: str
    scopes: list[str]
    connected_since: datetime
    last_issued: datetime


def connected_applications(user: Any) -> list[ConnectedApplication]:
    """
    Anwendungen mit einem gültigen Access-Token oder einem nicht widerrufenen Refresh-Token der Person.

    ``scopes`` sind die Beschreibungen der Scopes dieser Tokens; ``connected_since`` und ``last_issued`` sind das
    älteste bzw. jüngste dieser Tokens.
    """
    now = timezone.now()
    rows: dict[int, dict[str, Any]] = {}
    access = OAuthAccessToken.objects.filter(user=user, expires__gt=now, application__isnull=False).values(
        "application_id", "scope", "created"
    )
    refresh = OAuthRefreshToken.objects.filter(user=user, revoked__isnull=True).values(
        "application_id", "access_token__scope", "created"
    )
    entries = [(row["application_id"], row["scope"], row["created"]) for row in access] + [
        (row["application_id"], row["access_token__scope"] or "", row["created"]) for row in refresh
    ]
    for application_id, scope, created in entries:
        row = rows.setdefault(application_id, {"scopes": set(), "first": created, "last": created})
        row["scopes"].update(str(scope).split())
        row["first"] = min(row["first"], created)
        row["last"] = max(row["last"], created)
    names = dict(OAuthApplication.objects.filter(pk__in=rows).values_list("pk", "name"))
    result = [
        ConnectedApplication(
            application_id=application_id,
            name=str(names.get(application_id) or "Anwendung ohne Namen"),
            scopes=[scope.description for scope in catalog.CATALOG if scope.name in row["scopes"]],
            connected_since=row["first"],
            last_issued=row["last"],
        )
        for application_id, row in rows.items()
        if application_id in names
    ]
    return sorted(result, key=lambda item: item.name.lower())


def own_access_token(application: Any, raw: str) -> OAuthAccessToken | None:
    """Access-Token zu ``raw``, aber nur, wenn es dieser Anwendung gehört (Introspektion, RFC 7662)."""
    if not raw or len(raw) > access.MAX_TOKEN_LENGTH:
        return None
    token: OAuthAccessToken | None = (
        OAuthAccessToken.objects.select_related("application", "user")
        .filter(token_checksum=access.checksum(raw), application_id=application.pk)
        .first()
    )
    return token
