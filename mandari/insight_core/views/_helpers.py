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


class ActiveBodyRequiredMixin:
    """Leitet Seiten mit Kommune-Bezug ohne gewählte Kommune zur Auswahl um.

    Vorher zeigten diese Seiten im "Alle Kommunen"-Modus stillschweigend die
    Daten der ersten Kommune. Jetzt gilt konsistent: erst Kommune wählen
    (Auswahlseite auf /insight/), kommunenübergreifend bleibt die Suche.
    Existiert genau eine Kommune (Self-Hosting), wird sie automatisch gewählt.
    """

    def dispatch(self, request, *args, **kwargs):
        if is_all_bodies_mode(request):
            bodies = list(OParlBody.objects.filter(deleted=False)[:2])
            if len(bodies) == 1:
                request.session["active_body_id"] = str(bodies[0].id)
                request.session.modified = True
            else:
                from django.shortcuts import redirect

                return redirect("insight_core:insight:portal_home")
        return super().dispatch(request, *args, **kwargs)
