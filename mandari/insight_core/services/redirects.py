# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weiterleitungen bei Abrufen aus Django: im Takt je Host und nach der robots.txt des Ziels (Issue #1027).

Mit ``follow_redirects=True`` folgt ``httpx`` einer Weiterleitung sofort, ohne Reservierung in der Drossel je Host
und ohne Blick in die robots.txt des Ziels. Dokument-Cache, Vorschau, Löschabgleich, Textextraktion, Personenfotos
und der Abruf der robots.txt senden deshalb ohne automatische Weiterleitung und folgen ihr mit :func:`send`:

- Vor jeder Weiterleitung prüft :func:`guard` die robots.txt des Ziel-Hosts (mit der Kennung, die die Anfrage
  trägt, und der Ausnahme der Quelle) und reserviert danach einen Zeitpunkt in der Drossel je Host
  (``host_pacing``, Abstand der Quelle). Die robots.txt eines neuen Hosts wird dabei im selben Takt geladen.
- Sperrt die robots.txt das Ziel oder ist sie nicht erreichbar, folgen wir nicht (:class:`RedirectBlockedError`);
  findet die Drossel innerhalb von ``max_wait`` keinen Zeitpunkt, ebenfalls nicht (:class:`RedirectBusyError`).
  Beide sind ``httpx.RequestError``: Wer sie nicht eigens behandelt, wertet sie wie einen Netzfehler (später
  erneut), nie als Abruf.
- Höchstens :data:`MAX_REDIRECTS` Weiterleitungen je Abruf, danach ``httpx.TooManyRedirects``.

Ein nicht öffentliches Ziel (``safe_fetch.check_url``) beendet die Kette schon vor dem Blick in seine robots.txt
(``safe_fetch.BlockedDestinationError``); ``safe_fetch.guarded_client`` prüft jede Anfrage zusätzlich im
Ereignis-Hook des Clients. Die erste Anfrage prüfen und takten die Aufrufer selbst.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from mandari_oparl.robots import KIND_FILES, Decision

from . import host_pacing

#: Höchstens so viele Weiterleitungen je Abruf (RFC 9309 verlangt für die robots.txt mindestens fünf)
MAX_REDIRECTS = 5

BeforeRedirect = Callable[[httpx.Request], None]


class RedirectBlockedError(httpx.RequestError):
    """Die robots.txt des Ziels einer Weiterleitung sperrt den Abruf oder ist nicht erreichbar."""

    def __init__(self, decision: Decision, *, request: httpx.Request) -> None:
        super().__init__(f"Weiterleitung nicht gefolgt: {decision.reason}", request=request)
        self.decision = decision
        self.url = str(request.url)


class RedirectBusyError(httpx.RequestError):
    """Für das Ziel einer Weiterleitung war innerhalb der Höchstwartezeit kein Zeitpunkt frei."""


def send(
    client: httpx.Client,
    request: httpx.Request,
    *,
    before_redirect: BeforeRedirect,
    stream: bool = False,
    max_redirects: int = MAX_REDIRECTS,
) -> httpx.Response:
    """
    ``request`` senden und Weiterleitungen selbst folgen; vor jeder Weiterleitung ``before_redirect(ziel)``.

    Mit ``stream=True`` ist die Antwort gestreamt (der Aufrufer schließt sie); Antworten auf Weiterleitungen werden
    geschlossen, bevor wir dem Ziel folgen.
    """
    redirects = 0
    response = client.send(request, stream=stream, follow_redirects=False)
    while response.next_request is not None:
        next_request = response.next_request
        response.close()
        redirects += 1
        if redirects > max_redirects:
            raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=next_request)
        before_redirect(next_request)
        response = client.send(next_request, stream=stream, follow_redirects=False)
    return response


@contextmanager
def stream(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    before_redirect: BeforeRedirect,
    headers: dict[str, str] | None = None,
) -> Iterator[httpx.Response]:
    """Wie ``client.stream(...)``, aber Weiterleitungen über :func:`send` (``before_redirect`` vor jeder)."""
    response = send(
        client, client.build_request(method, url, headers=headers or {}), before_redirect=before_redirect, stream=True
    )
    try:
        yield response
    finally:
        response.close()


def guard(
    *,
    sync_config: Any = None,
    kind: str = KIND_FILES,
    max_wait: float | None = None,
    check_robots: bool = True,
) -> BeforeRedirect:
    """
    ``before_redirect`` für :func:`send`: robots.txt des Ziels prüfen (Art ``kind``, Kennung der Anfrage, Ausnahme
    aus ``sync_config``), dann einen Zeitpunkt für den Ziel-Host reservieren (Abstand aus ``sync_config``).

    ``max_wait``: höchstens so lange auf robots.txt und Drossel warten (Web-Anfragen), sonst
    :class:`RedirectBusyError`. ``check_robots=False`` nur für den Abruf der robots.txt selbst (immer erlaubt).
    """

    def before_redirect(request: httpx.Request) -> None:
        from . import robots, safe_fetch

        url = str(request.url)
        # Nicht öffentliche Ziele zuerst: dorthin auch keine Anfrage nach der robots.txt
        try:
            safe_fetch.check_url(url)
        except safe_fetch.BlockedDestinationError as exc:
            raise safe_fetch.BlockedDestinationError(str(exc), request=request) from None
        if check_robots:
            agent = request.headers.get("User-Agent") or robots.USER_AGENT
            try:
                decision = robots.check(url, kind, sync_config=sync_config, agent=agent, max_wait=max_wait)
            except host_pacing.PacingBusyError:
                raise RedirectBusyError("Drossel je Host: kein freier Zeitpunkt", request=request) from None
            if not decision.allowed:
                raise RedirectBlockedError(decision, request=request)
        if not host_pacing.wait(url, sync_config=sync_config, max_wait=max_wait):
            raise RedirectBusyError("Drossel je Host: kein freier Zeitpunkt", request=request)

    return before_redirect
