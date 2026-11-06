# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kontextprozessor des OpenID-Connect-Anbieters (Issue #1215).

``oidc_provider_enabled`` sagt den Vorlagen, ob der Anbieter eingeschaltet ist. Konto- und Profilseiten in Work,
Session und Insight verweisen nur dann auf „Verbundene Anwendungen“ (``accounts:connected_applications``), denn
ausgeschaltet antwortet die Seite mit 404.
"""

from __future__ import annotations

from django.http import HttpRequest

from . import provider_enabled


def oidc_kontext(request: HttpRequest) -> dict[str, bool]:
    """Kontextprozessor: ``oidc_provider_enabled`` für alle Vorlagen."""
    return {"oidc_provider_enabled": provider_enabled()}
