# SPDX-License-Identifier: AGPL-3.0-or-later
"""Template-Tags der öffentlichen Demo-Instanz (Issue #99): Zugänge auf der Anmeldeseite."""

from __future__ import annotations

from typing import Any

from django import template

from apps.common.demo import demo_kommune_url, demo_passwort, demo_zugaenge

register = template.Library()


@register.simple_tag
def demo_anmeldung() -> dict[str, Any] | None:
    """Zugänge, Passwort und Bürgerportal für die Anmeldeseite – ``None`` außerhalb der Demo-Instanz."""
    gruppen = demo_zugaenge()
    if not gruppen:
        return None
    return {"gruppen": gruppen, "passwort": demo_passwort(), "kommune_url": demo_kommune_url()}
