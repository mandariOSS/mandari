# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weiterleitungen folgt der Ingestor selbst: im Takt je Host und nach der robots.txt des Ziels (Issue #1027).

Mit ``follow_redirects=True`` folgt ``httpx`` einer Weiterleitung sofort, ohne Reservierung in der Drossel je Host
und ohne Blick in die robots.txt des Ziels. Eine Weiterleitung auf einen anderen Pfad oder Host ist aber eine
eigene Anfrage. Deshalb senden alle Abrufe bei Quellen (OParl-Abgleich, Textextraktion, Scraper, robots.txt)
ohne automatische Weiterleitung und folgen ihr hier:

- Vor jeder Weiterleitung ruft :func:`send` ``before_redirect`` mit dem Ziel auf. :func:`check_and_pace` prüft dort
  die robots.txt des Ziel-Hosts (wie vor der ersten Anfrage, mit derselben Kennung und Ausnahme der Quelle) und
  reserviert danach einen Zeitpunkt in der Drossel je Host. Sperrt die robots.txt das Ziel oder ist sie nicht
  erreichbar, folgen wir nicht (:class:`RedirectRefusedError`).
- Mit ``slot`` (``host_pacer.limit``) zieht der Platz des Abrufs in der Grenze gleichzeitiger Anfragen je Host
  vorher zum Ziel-Host um: Die Grenze gilt auch dort, nicht nur beim Host der ersten Anfrage.
- Höchstens :data:`MAX_REDIRECTS` Weiterleitungen je Abruf, danach ``httpx.TooManyRedirects``.

Die erste Anfrage prüft und taktet der Aufrufer selbst, wie bisher.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import httpx
from mandari_oparl.robots import Decision, RobotsOverride

if TYPE_CHECKING:
    from src.client.host_pacing import HostSlot

#: Höchstens so viele Weiterleitungen je Abruf (RFC 9309 verlangt für die robots.txt mindestens fünf)
MAX_REDIRECTS = 5


class RedirectRefusedError(Exception):
    """Einer Weiterleitung folgen wir nicht: Die robots.txt ihres Ziels sperrt den Abruf oder ist nicht erreichbar."""

    def __init__(self, url: str, decision: Decision) -> None:
        self.url = url
        self.decision = decision
        super().__init__(f"Weiterleitung nicht gefolgt: {decision.reason}")


async def send(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    before_redirect: Callable[[str], Awaitable[None]],
    stream: bool = False,
    max_redirects: int = MAX_REDIRECTS,
) -> httpx.Response:
    """
    ``request`` senden und Weiterleitungen selbst folgen; vor jeder Weiterleitung ``before_redirect(ziel)``.

    Mit ``stream=True`` ist die Antwort gestreamt (der Aufrufer schließt sie); Antworten auf Weiterleitungen
    werden geschlossen, bevor wir dem Ziel folgen. ``before_redirect`` beendet die Kette mit einer Ausnahme
    (:class:`RedirectRefusedError`), mehr als ``max_redirects`` Weiterleitungen mit ``httpx.TooManyRedirects``.
    """
    redirects = 0
    response = await client.send(request, stream=stream, follow_redirects=False)
    while response.next_request is not None:
        next_request = response.next_request
        await response.aclose()
        redirects += 1
        if redirects > max_redirects:
            raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=next_request)
        await before_redirect(str(next_request.url))
        response = await client.send(next_request, stream=stream, follow_redirects=False)
    return response


def check_and_pace(
    client: httpx.AsyncClient | None,
    *,
    user_agent: str,
    kind: str,
    override: RobotsOverride | None,
    pace: Callable[[str], Awaitable[None]],
    slot: HostSlot | None = None,
) -> Callable[[str], Awaitable[None]]:
    """
    ``before_redirect`` für :func:`send`: robots.txt des Ziels prüfen (Art ``kind``, Kennung ``user_agent``,
    Ausnahme der Quelle), dann mit ``pace`` einen Zeitpunkt für den Ziel-Host reservieren. Die robots.txt eines
    neuen Hosts wird dabei im selben Takt geladen (``pace``) wie jede andere Anfrage.

    ``slot``: Platz des Abrufs in der Grenze gleichzeitiger Anfragen je Host (``host_pacer.limit``). Er zieht vor
    allem anderen zum Ziel-Host um, sodass die Grenze auch dort gilt, schon für den Abruf seiner robots.txt.
    """

    async def before_redirect(url: str) -> None:
        from src.client.robots import robots_gate  # robots lädt die robots.txt selbst über send()

        if slot is not None:
            await slot.move_to(url)
        decision = await robots_gate.decide(client, url, user_agent=user_agent, kind=kind, override=override, pace=pace)
        if not decision.allowed:
            raise RedirectRefusedError(url, decision)
        await pace(url)

    return before_redirect


def pace_only(pace: Callable[[str], Awaitable[None]]) -> Callable[[str], Awaitable[None]]:
    """``before_redirect`` ohne robots.txt-Prüfung: nur für den Abruf der robots.txt selbst (immer erlaubt)."""

    async def before_redirect(url: str) -> None:
        await pace(url)

    return before_redirect
