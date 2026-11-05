# SPDX-License-Identifier: AGPL-3.0-or-later
"""
mandari als OpenID-Connect-Anbieter (Issue #1206, ADR ``20261105-eine-schnittstelle-api-first``).

Grundlage ist django-oauth-toolkit. Die Plattform legt dessen austauschbare Modelle selbst fest
(:mod:`.models`), dazu Scopes (:mod:`.catalog`, :mod:`.scopes`), Prüfregeln (:mod:`.validator`),
Signaturschlüssel (:mod:`.signing_keys`), Endpunkte (:mod:`.views`, :mod:`.urls`), Prüfprotokoll
(:mod:`.audit`) und die Prüfung von Access-Tokens für Ressourcen wie die Session-API (:mod:`.access`).

Der Schalter ``OIDC_PROVIDER_ENABLED`` ist standardmäßig aus; dann antworten alle Endpunkte mit 404
und kein Access-Token wird angenommen. Für Anwendungsentwickler: ``docs/schnittstelle/anmeldung.md``.
"""

from __future__ import annotations

from django.conf import settings


def provider_enabled() -> bool:
    """Ist der Anbieter eingeschaltet (``OIDC_PROVIDER_ENABLED``)?"""
    return bool(getattr(settings, "OIDC_PROVIDER_ENABLED", False))
