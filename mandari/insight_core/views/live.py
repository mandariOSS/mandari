# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Live-Seite einer Sitzung (Issue #915): ``/insight/termine/<uuid>/live/``, dazu der Kinomodus
``…/live/kino/`` (Video groß, schlanke Leiste, ohne Rahmen von Insight).

Bewusst nicht verlinkt (kein Menü, kein Link auf der Sitzungsseite) und ``noindex``. Die Daten kommen nur über
``hub.live.selectors``. Gibt es für kein Gremium der Sitzung eine Übertragungsquelle, antwortet die Seite mit 404.

- **Player nur bei laufender Übertragung, per Zwei-Klick:** Zuerst nur ein Hinweis, dass beim Abspielen Daten an den
  Anbieter gehen. Erst der Klick auf „Übertragung laden“ bzw. „Kinomodus“ (``?player=1``) bettet den Player ein.
  Läuft die Übertragung nicht, gibt es weder Knopf noch Player, auch nicht mit ``?player=1``. Dafür erlaubt die
  Seite in ihrer Content-Security-Policy die Player-Ursprünge der registrierten Anbieter (``frame-src``); sonst gilt
  dieselbe Richtlinie, die der Reverse Proxy für alle Seiten setzt.
- **Offizielle Quelle:** Hat die Quelle eine Seite der Kommune, steht der Link darauf in jedem Zustand da.
- **Aktualisierung:** Der Live-Teil lädt sich per htmx alle 15 Sekunden neu (``?teil=stand``), solange sich der
  Status noch ändern kann (``LiveStand.nachfragen``), nach dem Ende also noch, solange die Übertragung nach einer
  Pause wieder anlaufen kann. Den Player-Bereich schickt die Antwort nur bei einem Statuswechsel mit
  (``hx-swap-oob``); ein laufender Player wird sonst nicht angefasst.
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


def _status_aus_version(version: str) -> str:
    """Status, den die Seite zuletzt gezeigt hat (erster Teil der Version, siehe ``_version``)."""
    return version.split(".", 1)[0]


def _antwort(request: HttpRequest, pk: uuid.UUID, vorlage: str) -> HttpResponse:
    """Live-Seite bzw. Kinomodus; mit ``?teil=stand`` nur der Live-Teil (htmx)."""
    stand = live.live_stand(pk)
    if stand is None or stand.meeting.withdrawn_by_publisher:
        raise Http404("Keine Live-Übertragung für diese Sitzung")
    version = _version(stand)
    teil = request.GET.get("teil") == "stand"
    gezeigt = request.GET.get("v", "")
    if teil and gezeigt == version:
        # Nichts Neues: htmx tauscht bei 204 nichts aus, Bildschirmleser lesen nichts erneut vor
        return HttpResponse(status=204)
    zustimmung = request.GET.get("player") == "1"
    context: dict[str, Any] = {
        "stand": stand,
        "meeting": stand.meeting,
        # Knopf und Player nur, solange die Übertragung läuft und der Anbieter einen Player hat
        "abspielbar": stand.laeuft and bool(stand.einbettung_url),
        "player": zustimmung and stand.laeuft and bool(stand.einbettung_url),
        "zustimmung": zustimmung,
        "aktualisierung": AKTUALISIERUNG,
        "version": version,
        "teil": teil,
        # Player-Bereich nur bei einem Statuswechsel neu, sonst lädt ein laufender Player nicht neu
        "statuswechsel": teil and _status_aus_version(gezeigt) != stand.status,
        "seo": {"robots": "noindex, nofollow"},
    }
    response = render(request, f"{vorlage}#jetzt" if teil else vorlage, context)
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
@csp_override(_ERZWUNGEN)
@csp_report_only_override(_nur_bericht())
def meeting_live(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    """Live-Seite einer Sitzung; mit ``?teil=stand`` nur der Live-Teil (htmx)."""
    return _antwort(request, pk, "pages/meetings/live.html")


@require_GET
@csp_override(_ERZWUNGEN)
@csp_report_only_override(_nur_bericht())
def meeting_live_kino(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    """Kinomodus der Live-Seite: Video groß, Leiste mit Stand und Links, ohne Rahmen von Insight."""
    return _antwort(request, pk, "pages/meetings/live_kino.html")
