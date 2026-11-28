# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigene Anwendungen einer Organisation über die Anmeldung von mandari (Issue #1591).

Work meldet bei der Plattform die Art ``work-organisation`` an (``apps.accounts.oidc.bindings``): Die Plattform speichert
an einer Anwendung, die eine Fraktion registriert, nur die fachfreie Kennung ``work-organisation:<Kennung>``; Work löst
sie auf und nennt die Organisation. Anwendungen einer Organisation erhalten nur :data:`SCOPES` und gelten bei
Ressourcen von Work nur für diese Organisation (``OAuthAccess.allows``).
"""

from __future__ import annotations

import uuid
from typing import Final

from apps.accounts.oidc import bindings, catalog

#: Scopes für Anwendungen, die eine Organisation selbst registriert: Anmeldung, Ratsdaten, Einreichung und Work im
#: Namen von Personen; Nichtöffentliches (#565) nie
SCOPES: Final = (
    catalog.OPENID,
    catalog.PROFILE,
    catalog.EMAIL,
    catalog.RATSDATEN_LESEN,
    catalog.EINREICHUNG_SCHREIBEN,
    catalog.WORK_LESEN,
    catalog.WORK_SCHREIBEN,
)


def _aufloesen(slug: str) -> str | None:
    from apps.tenants.models import Organization

    pk = Organization.objects.filter(slug=slug, is_active=True).values_list("pk", flat=True).first()
    return str(pk) if pk is not None else None


def _beschreiben(key: str) -> str | None:
    from apps.tenants.models import Organization

    try:
        pk = uuid.UUID(key)
    except ValueError:
        return None
    name = Organization.objects.filter(pk=pk).values_list("name", flat=True).first()
    return str(name) if name else None


ART: Final = bindings.Kind(
    name=bindings.ORGANIZATION, label="Organisation", resolve=_aufloesen, describe=_beschreiben, scopes=SCOPES
)


def owner(organization_pk: object) -> str:
    """Kennung der Organisation als Bereich einer Anwendung."""
    return bindings.compose(bindings.ORGANIZATION, str(organization_pk))


def register() -> None:
    """Art „Organisation“ bei der Plattform anmelden (aus ``WorkConfig.ready``)."""
    bindings.register(ART)
