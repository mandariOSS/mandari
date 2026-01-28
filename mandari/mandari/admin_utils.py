"""
Utility-Funktionen für Django Unfold Admin.
"""

import os


def environment_callback(request):
    """
    Gibt das aktuelle Environment als Badge zurück.
    Wird oben rechts im Admin angezeigt.

    Returns:
        tuple: (Label-Text, Farbe)
        Farben: "info", "danger", "warning", "success"
    """
    debug = os.environ.get("DEBUG", "True").lower() in ("true", "1", "yes")

    if debug:
        return ("Development", "warning")

    # Prüfe auf Staging-Domain
    host = request.get_host()
    if "mandari.dev" in host or "staging" in host:
        return ("Staging", "info")

    return ("Production", "success")
