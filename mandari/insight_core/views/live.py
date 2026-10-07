# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Live-Seite einer Sitzung (Issue #915): ``/insight/termine/<uuid>/live/``.

Bewusst nicht verlinkt (kein Menü, kein Link auf der Sitzungsseite) und ``noindex``. Die Daten kommen nur über
``hub.live.selectors``. Gibt es für kein Gremium der Sitzung eine Übertragungsquelle, antwortet die Seite mit 404.

- **Player per Zwei-Klick:** Zuerst nur ein Hinweis, dass beim Abspielen Daten an den Anbieter gehen. Erst der Klick
  auf „Übertragung laden“ (``?player=1``) bettet den Player ein. Dafür erlaubt die Seite in ihrer
  Content-Security-Policy die Player-Ursprünge der registrierten Anbieter (``frame-src``); sonst gilt dieselbe
  Richtlinie, die der Reverse Proxy für alle Seiten setzt.
- **Aktualisierung:** Der Live-Teil lädt sich per htmx alle 15 Sekunden neu (``?teil=stand``), solange die
  Übertragung nicht beendet ist.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.csp import CSP
from django.views.decorators.csp import csp_override, csp_report_only_override
from django.views.decorators.http import require_GET

from hub.live import selectors as live
from hub.live.anbieter import player_urspruenge

#: Sekunden zwischen zwei Aktualisierungen des Live-Teils
AKTUALISIERUNG: Final = 15

#: Erzwungene Richtlinie dieser Seite: wie die des Reverse Proxy (Caddyfile), dazu die Player der Anbieter
_ERZWUNGEN: Final[dict[str, list[str]]] = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.UNSAFE_INLINE, CSP.UNSAFE_EVAL],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "img-src": [CSP.SELF, "data:", "https:"],
    "font-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF, "wss:"],
    "frame-src": [CSP.SELF, *player_urspruenge()],
    "frame-ancestors": [CSP.SELF],
}


def _nur_bericht() -> dict[str, list[str]]:
    """Report-Only-Richtlinie der Anwendung, ``frame-src`` um die Player ergänzt."""
    from django.conf import settings

    basis = {k: list(v) for k, v in dict(getattr(settings, "SECURE_CSP_REPORT_ONLY", {}) or {}).items()}
    if basis:
        basis["frame-src"] = [*basis.get("frame-src", [CSP.SELF]), *player_urspruenge()]
    return basis


def _version(stand: live.LiveStand) -> str:
    """Kurzer Stand des Live-Teils; ändert sich mit Status, TOP und Wortmeldungen."""
    wortmeldungen = sum(len(a.wortmeldungen) for a in stand.verlauf) + len(stand.ohne_abschnitt)
    jetzt = stand.jetzt_abschnitt.nummer if stand.jetzt_abschnitt else ""
    return f"{stand.status}.{len(stand.verlauf)}.{wortmeldungen}.{jetzt}"


@require_GET
@csp_override(_ERZWUNGEN)
@csp_report_only_override(_nur_bericht())
def meeting_live(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    """Live-Seite einer Sitzung; mit ``?teil=stand`` nur der Live-Teil (htmx)."""
    stand = live.live_stand(pk)
    if stand is None or stand.meeting.withdrawn_by_publisher:
        raise Http404("Keine Live-Übertragung für diese Sitzung")
    version = _version(stand)
    teil = request.GET.get("teil") == "stand"
    if teil and request.GET.get("v") == version:
        # Nichts Neues: htmx tauscht bei 204 nichts aus, Bildschirmleser lesen nichts erneut vor
        return HttpResponse(status=204)
    context: dict[str, Any] = {
        "stand": stand,
        "meeting": stand.meeting,
        "player": request.GET.get("player") == "1" and bool(stand.einbettung_url),
        "aktualisierung": AKTUALISIERUNG,
        "version": version,
        "teil": teil,
        "seo": {"robots": "noindex, nofollow"},
    }
    vorlage = "pages/meetings/live.html#jetzt" if teil else "pages/meetings/live.html"
    response = render(request, vorlage, context)
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cache-Control"] = "no-cache"
    return response
