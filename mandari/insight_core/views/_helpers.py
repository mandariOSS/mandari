# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.http import HttpRequest

from ..models import (
    OParlBody,
)
from ..portal import get_portal

# =============================================================================
# Helper Functions
# =============================================================================


def get_active_body(request: HttpRequest) -> OParlBody | None:
    """Holt die aktive Kommune aus der Session oder setzt einen Standard."""
    # Bürgerportal einer Körperschaft (Issue #317): Die Kommune ist festgelegt
    portal = get_portal(request)
    if portal is not None:
        return portal.body
    body_id = request.session.get("active_body_id")
    if body_id == "all":
        # "Alle Kommunen"-Modus: Auswahl NICHT überschreiben. Views, die zwingend
        # eine einzelne Kommune brauchen, erhalten die erste Kommune als Fallback,
        # is_all_bodies_mode() bleibt dabei True.
        return OParlBody.objects.listed().first()
    if body_id:
        try:
            return OParlBody.objects.get(id=body_id)
        except OParlBody.DoesNotExist:
            pass
    # Fallback: Erste Kommune als Standard
    default_body = OParlBody.objects.listed().first()
    if default_body:
        request.session["active_body_id"] = str(default_body.id)
        return default_body
    return None


def link_confirmation(request: HttpRequest, *, title: str, message: str, button: str, icon: str = "mail-check"):
    """
    Bestätigungsseite für Links aus E-Mails: Der Aufruf per GET ändert nichts.

    Mail-Scanner und Vorschauen öffnen Links vorab. Bestätigen und Abmelden wirken deshalb erst
    mit dem Klick auf der Seite (POST, CSRF-geschützt). Links in bereits versandten Mails bleiben
    gültig und führen auf diese Seite.
    """
    from django.shortcuts import render

    response = render(
        request, "pages/link_confirm.html", {"title": title, "message": message, "button": button, "icon": icon}
    )
    response["X-Robots-Tag"] = "noindex"
    response["Cache-Control"] = "no-store"
    return response


def page_number(request: HttpRequest, maximum: int = 500) -> int:
    """``?page=`` als Zahl zwischen 1 und ``maximum``; Ungültiges ergibt Seite 1 statt eines Fehlers."""
    try:
        page = int(str(request.GET.get("page", "1")).strip())
    except ValueError:
        return 1
    return min(max(page, 1), maximum)


def is_all_bodies_mode(request):
    """Prüft ob der 'Alle Kommunen' Modus aktiv ist."""
    if get_portal(request) is not None:
        return False
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
            bodies = list(OParlBody.objects.listed()[:2])
            if len(bodies) == 1:
                request.session["active_body_id"] = str(bodies[0].id)
                request.session.modified = True
            else:
                from django.shortcuts import redirect

                return redirect("insight_core:insight:portal_home")
        return super().dispatch(request, *args, **kwargs)
