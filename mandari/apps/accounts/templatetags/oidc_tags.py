# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verbundene Anwendungen auf Profilseiten (Issue #1591).

``{% verbundene_anwendungen as liste %}`` liefert die Anwendungen, die gerade im Namen der angemeldeten Person handeln
dürfen (``apps.accounts.oidc.selectors.connected_applications``), bei ausgeschaltetem Anbieter eine leere Liste. Die
Vorlage ``accounts/oidc/_verbundene_liste.html`` zeigt sie mit „Widerrufen“; der Widerruf läuft über die Kontoseite
„Verbundene Anwendungen“ und führt zurück auf die Profilseite.
"""

from __future__ import annotations

from typing import Any

from django import template

from apps.accounts.oidc import provider_enabled, selectors

register = template.Library()


@register.simple_tag(takes_context=True)
def verbundene_anwendungen(context: template.Context) -> list[selectors.ConnectedApplication]:
    request: Any = context.get("request")
    user = getattr(request, "user", None)
    if not provider_enabled() or user is None or not getattr(user, "is_authenticated", False):
        return []
    return selectors.connected_applications(user)
