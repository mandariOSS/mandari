"""
Context Processors für Mandari Insight.

Stellt globale Context-Variablen für alle Templates bereit.
"""

from .models import OParlBody


def navigation_context(request):
    """
    Erkennt den aktuellen Navigationskontext (Portal vs. Marketing).

    Basierend auf dem URL-Pfad wird entschieden, welche Navigation angezeigt wird.
    """
    path = request.path

    # Portal-Kontext: Alles unter /insight/
    is_portal = path.startswith("/insight/")

    # Marketing-Kontext: Alles andere (Landingpage, Produkt, Preise, etc.)
    is_marketing = not is_portal

    return {
        "is_portal": is_portal,
        "is_marketing": is_marketing,
        "nav_context": "portal" if is_portal else "marketing",
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

    return {
        "active_body": body,
        "available_bodies": bodies,
        "show_all_bodies": show_all_bodies,
    }
