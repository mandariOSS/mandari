"""
Context Processors für Mandari Insight.

Stellt globale Context-Variablen für alle Templates bereit.
"""

from .models import OParlBody, OParlMeeting


def navigation_context(request):
    """
    Setzt den Navigationskontext.

    Mandari only serves portal pages now (under /insight/).
    Marketing pages are served by the separate Wagtail site.
    """
    return {
        "is_portal": True,
        "is_marketing": False,
        "nav_context": "portal",
    }


def active_body(request):
    """
    Stellt die aktive Kommune (Body) im Template-Context bereit.

    Die Kommune wird aus der Session oder URL ermittelt.
    Wenn "all" in der Session steht, wird keine spezifische Kommune ausgewählt.
    """
    # Versuche Body aus Session zu laden
    body_id = request.session.get("active_body_id")
    body = None
    bodies = []
    show_all_bodies = False

    try:
        bodies = list(OParlBody.objects.all().order_by("name"))

        # "all" bedeutet: Alle Kommunen anzeigen (keine spezifische ausgewählt)
        if body_id == "all":
            show_all_bodies = True
            body = None
        elif body_id:
            try:
                body = OParlBody.objects.get(id=body_id)
            except OParlBody.DoesNotExist:
                body = None

        # Kein Fallback mehr - wenn keine Kommune ausgewählt, zeigen wir alle
        # Nur bei erster Nutzung (keine Session) setzen wir auf "all"
        if body_id is None and bodies:
            show_all_bodies = True
            request.session["active_body_id"] = "all"

    except Exception:
        # Datenbank noch nicht migriert oder andere Fehler
        pass

    # Count upcoming meetings for sidebar badge
    upcoming_count = 0
    if body:
        from django.utils import timezone

        upcoming_count = OParlMeeting.objects.filter(
            body=body, start__gte=timezone.now(), cancelled=False
        ).count()

    return {
        "active_body": body,
        "available_bodies": bodies,
        "show_all_bodies": show_all_bodies,
        "upcoming_meeting_count": upcoming_count,
    }
