# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Keine neuen Sitzungen für Crawler und Kalender-Abos (Issue #1091).

Suchmaschinen, KI-Crawler und Kalenderprogramme schicken das Sitzungs-Cookie nicht zurück. Schreibt eine Seite für
sie etwas in die Sitzung (Kommunenwahl der Stadtseite, ``?kommune=``, Rückfall auf die erste Kommune), entsteht
bei jedem ihrer Aufrufe eine neue Zeile in ``django_session`` – in Produktion Zehntausende am Tag. Diese Middleware
verwirft eine solche neue Sitzung, bevor ``SessionMiddleware`` sie speichert. Wer schon eine Sitzung hat (Cookie
mitgeschickt), ist nicht betroffen, Menschen im Browser auch nicht.

Steht in ``MIDDLEWARE`` direkt hinter ``SessionMiddleware``: Antworten durchlaufen die Liste rückwärts, diese
Middleware sieht die Antwort also vor dem Speichern.
"""

from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

#: Pfadendungen von Abos, die Programme ohne Cookie abrufen (Kalender-Feed)
ABO_ENDUNGEN = (".ics",)


def ohne_neue_sitzung(request: HttpRequest) -> bool:
    """Crawler, Abrufprogramm oder Kalender-Abo – eine neue Sitzung käme nie wieder zurück."""
    from .services.file_cache_limit import is_crawler

    kennung = request.META.get("HTTP_USER_AGENT", "")
    return is_crawler(kennung) or request.path.endswith(ABO_ENDUNGEN)


class KeineSitzungFuerCrawlerMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        session = getattr(request, "session", None)
        # Nur eine neue, beschriebene Sitzung ohne mitgeschicktes Cookie: Antworten, die die Sitzung nicht anfassen
        # (Sitemaps, Dateien), bleiben ohne „Vary: Cookie“, und die Erkennung läuft nur bei Bedarf
        if (
            session is None
            or not session.modified
            or session.session_key
            or settings.SESSION_COOKIE_NAME in request.COOKIES
        ):
            return response
        if ohne_neue_sitzung(request):
            # Leere, nie gespeicherte Sitzung: SessionMiddleware speichert nichts und setzt kein Cookie
            session.clear()
        return response
