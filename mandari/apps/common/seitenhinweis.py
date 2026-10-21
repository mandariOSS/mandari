"""
Betriebshinweis oben in Insight und Work (``SEITENHINWEIS``).

Für Störungen außerhalb von mandari, etwa wenn viele Ratsinformationssysteme gleichzeitig offline sind
(21.10.2026, PHP2Shell). Der Text kommt nur aus der Umgebung: Ein- und Ausschalten braucht keinen Deploy,
nur das Neuerstellen der Container. Leer heißt aus.
"""

from django.conf import settings
from django.http import HttpRequest


def seitenhinweis_context(request: HttpRequest) -> dict[str, str]:
    """Kontextprozessor: ``seitenhinweis`` und ``seitenhinweis_link`` für ``partials/seitenhinweis.html``."""
    return {
        "seitenhinweis": getattr(settings, "SEITENHINWEIS", ""),
        "seitenhinweis_link": getattr(settings, "SEITENHINWEIS_LINK", ""),
    }
