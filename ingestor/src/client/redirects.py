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

Antworten, die ganz in den Speicher kommen (robots.txt, Seiten der Scraper), liest :func:`read_limited`: höchstens
eine feste Menge entpackter Bytes. :class:`Deadline` begrenzt dazu die Zeit, die ein Abruf insgesamt im Netz
verbringt.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

import httpx
from mandari_oparl.robots import Decision, RobotsOverride

if TYPE_CHECKING:
    from src.client.host_pacing import HostSlot

#: Höchstens so viele Weiterleitungen je Abruf (RFC 9309 verlangt für die robots.txt mindestens fünf)
MAX_REDIRECTS = 5
#: Stückgröße beim Lesen einer gestreamten Antwort: So viele komprimierte Bytes liegen höchstens zugleich vor
READ_CHUNK_BYTES = 64 * 1024
#: Kompressionsverfahren, die :func:`read_limited` entpackt (``x-gzip`` ist der alte Name von ``gzip``)
SUPPORTED_ENCODINGS = frozenset({"gzip", "x-gzip", "deflate"})


class RedirectRefusedError(Exception):
    """Einer Weiterleitung folgen wir nicht: Die robots.txt ihres Ziels sperrt den Abruf oder ist nicht erreichbar."""

    def __init__(self, url: str, decision: Decision) -> None:
        self.url = url
        self.decision = decision
        super().__init__(f"Weiterleitung nicht gefolgt: {decision.reason}")


class Deadline:
    """
    Gesamtfrist eines Abrufs über alle seine Netzvorgänge: die Anfragen einer Weiterleitungskette und das Lesen der
    Antwort.

    Die Zeitlimits von ``httpx`` gelten je Lesevorgang. Eine Antwort, die tropfenweise kommt, hielte einen Abruf
    deshalb beliebig lange offen und mit ihm seinen Platz je Host. Gezählt wird nur die Zeit im Netz (:meth:`spend`),
    nicht das Warten auf den Takt je Host vor einer Weiterleitung: Das liegt an uns, nicht am Server. Nach Ablauf
    endet der Vorgang mit ``httpx.ReadTimeout``, also wie bei einem Zeitlimit von ``httpx`` selbst; Aufrufer brauchen
    keinen eigenen Zweig.
    """

    def __init__(self, seconds: float) -> None:
        self._left = max(0.0, seconds)

    @contextlib.asynccontextmanager
    async def spend(self, request: httpx.Request) -> AsyncIterator[None]:
        """Einen Netzvorgang ausführen; seine Dauer geht von der Frist ab."""
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._left):
                yield
        except TimeoutError as exc:
            raise httpx.ReadTimeout("Gesamtfrist des Abrufs überschritten", request=request) from exc
        finally:
            self._left = max(0.0, self._left - (time.monotonic() - started))


async def _send_once(
    client: httpx.AsyncClient, request: httpx.Request, stream: bool, deadline: Deadline | None
) -> httpx.Response:
    if deadline is None:
        return await client.send(request, stream=stream, follow_redirects=False)
    async with deadline.spend(request):
        return await client.send(request, stream=stream, follow_redirects=False)


async def send(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    before_redirect: Callable[[str], Awaitable[None]],
    stream: bool = False,
    max_redirects: int = MAX_REDIRECTS,
    deadline: Deadline | None = None,
) -> httpx.Response:
    """
    ``request`` senden und Weiterleitungen selbst folgen; vor jeder Weiterleitung ``before_redirect(ziel)``.

    Mit ``stream=True`` ist die Antwort gestreamt (der Aufrufer schließt sie); Antworten auf Weiterleitungen
    werden geschlossen, bevor wir dem Ziel folgen. ``before_redirect`` beendet die Kette mit einer Ausnahme
    (:class:`RedirectRefusedError`), mehr als ``max_redirects`` Weiterleitungen mit ``httpx.TooManyRedirects``.
    Mit ``deadline`` zählen alle Anfragen der Kette auf dieselbe Gesamtfrist (:class:`Deadline`).
    """
    redirects = 0
    response = await _send_once(client, request, stream, deadline)
    while response.next_request is not None:
        next_request = response.next_request
        await response.aclose()
        redirects += 1
        if redirects > max_redirects:
            raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=next_request)
        await before_redirect(str(next_request.url))
        response = await _send_once(client, next_request, stream, deadline)
    return response


class _Inflater:
    """
    Entpackt eine Kompressionslage stückweise und nie mehr als verlangt (``zlib`` mit ``max_length``).

    ``gzip`` und ``deflate`` mit zlib-Hülle erkennt ``zlib`` am Kopf. Manche Server senden unter ``deflate`` rohe
    Deflate-Daten ohne Hülle: Scheitert das erste Stück, entpacken wir es so (wie ``httpx``).
    """

    def __init__(self, encoding: str) -> None:
        self._inflater = zlib.decompressobj(zlib.MAX_WBITS | 32)
        self._may_be_raw = encoding == "deflate"

    def feed(self, data: bytes, max_length: int) -> bytes:
        may_be_raw, self._may_be_raw = self._may_be_raw, False
        try:
            return self._inflater.decompress(data, max_length)
        except zlib.error:
            if not may_be_raw:
                raise
            self._inflater = zlib.decompressobj(-zlib.MAX_WBITS)
            return self._inflater.decompress(data, max_length)

    @property
    def finished(self) -> bool:
        return self._inflater.eof


def _content_encoding(response: httpx.Response) -> str | None:
    """Das eine Kompressionsverfahren der Antwort (``None``: keines); sonst ``httpx.DecodingError``."""
    encodings = [
        value.strip().lower()
        for value in response.headers.get("content-encoding", "").split(",")
        if value.strip() and value.strip().lower() != "identity"
    ]
    if not encodings:
        return None
    # Mehrere Lagen übereinander vervielfachen, was ein gelesenes Stück entpackt belegt; so antwortet kein Server
    if len(encodings) > 1 or encodings[0] not in SUPPORTED_ENCODINGS:
        raise httpx.DecodingError(
            f"Content-Encoding nicht unterstützt: {', '.join(encodings)[:80]}", request=response.request
        )
    return encodings[0]


async def read_limited(response: httpx.Response, limit: int, *, deadline: Deadline | None = None) -> bytes:
    """
    Gestreamte Antwort (``send`` mit ``stream=True``) lesen und schließen: höchstens ``limit`` entpackte Bytes, der
    Rest bleibt ungelesen. Wer wissen muss, ob die Antwort größer war, liest ``limit + 1`` Bytes.

    ``httpx`` entpackt jedes gelesene Stück ohne Obergrenze und in allen angegebenen Lagen: Aus wenigen Kilobyte
    werden Hunderte Megabyte, bevor ein Aufrufer das erste Byte zählen kann. Deshalb lesen wir die rohen Bytes und
    entpacken selbst, nie über die Grenze hinaus. Mehrere Lagen, ein unbekanntes Verfahren und fehlerhafte Daten
    enden mit ``httpx.DecodingError`` (für Aufrufer ein Abruffehler, kein leeres Ergebnis). Mit ``deadline`` zählt das
    Lesen auf die Gesamtfrist des Abrufs.
    """
    try:
        if response.is_stream_consumed:
            # Die Antwort lag dem Transport schon ganz vor (kein Strom): Hier gibt es nichts mehr zu begrenzen
            return response.content[:limit]
        encoding = _content_encoding(response)
        inflater = _Inflater(encoding) if encoding is not None else None
        body = bytearray()
        async with contextlib.AsyncExitStack() as stack:
            if deadline is not None:
                await stack.enter_async_context(deadline.spend(response.request))
            async for raw in response.aiter_raw(READ_CHUNK_BYTES):
                room = limit - len(body)
                if room <= 0:
                    break
                if inflater is None:
                    body += raw[:room]
                    continue
                try:
                    body += inflater.feed(raw, room)
                except zlib.error as exc:
                    raise httpx.DecodingError(f"Antwort nicht entpackbar: {exc}", request=response.request) from exc
                if inflater.finished:
                    break
        return bytes(body)
    finally:
        await response.aclose()


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
