# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from ..models import (
    OParlBody,
)

# =============================================================================
# Helper Functions
# =============================================================================


def get_active_body(request):
    """Holt die aktive Kommune aus der Session oder setzt einen Standard."""
    body_id = request.session.get("active_body_id")
    if body_id == "all":
        # "Alle Kommunen"-Modus: Auswahl NICHT überschreiben. Views, die zwingend
        # eine einzelne Kommune brauchen, erhalten die erste Kommune als Fallback,
        # is_all_bodies_mode() bleibt dabei True.
        return OParlBody.objects.first()
    if body_id:
        try:
            return OParlBody.objects.get(id=body_id)
        except OParlBody.DoesNotExist:
            pass
    # Fallback: Erste Kommune als Standard
    default_body = OParlBody.objects.first()
    if default_body:
        request.session["active_body_id"] = str(default_body.id)
        return default_body
    return None


def is_all_bodies_mode(request):
    """Prüft ob der 'Alle Kommunen' Modus aktiv ist."""
    body_id = request.session.get("active_body_id")
    return body_id is None or body_id == "all"
