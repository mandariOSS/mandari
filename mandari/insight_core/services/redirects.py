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
  findet die Drossel innerhalb der Frist keinen Zeitpunkt, ebenfalls nicht (:class:`RedirectBusyError`). Die Frist
  (``deadline`` bzw. ``max_wait``) gilt für alle Weiterleitungen eines Abrufs zusammen, nicht je Schritt.
  Beide sind ``httpx.RequestError``: Wer sie nicht eigens behandelt, wertet sie wie einen Netzfehler (später
  erneut), nie als Abruf.
- Höchstens :data:`MAX_REDIRECTS` Weiterleitungen je Abruf, danach ``httpx.TooManyRedirects``.

Ein nicht öffentliches Ziel (``safe_fetch.check_url``) beendet die Kette schon vor dem Blick in seine robots.txt
(``safe_fetch.BlockedDestinationError``); ``safe_fetch.guarded_client`` prüft jede Anfrage zusätzlich im
Ereignis-Hook des Clients. Die erste Anfrage prüfen und takten die Aufrufer selbst.
"""

from __future__ import annotations

import time
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

import httpcore
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


class ResponseTooLargeError(httpx.HTTPError):
    """Der Rumpf einer Antwort überschreitet die Grenze des Aufrufers."""


#: Kompression, die :func:`read_limited` entpackt; Aufrufer fordern nur diese an (``ACCEPT_ENCODING``)
ACCEPT_ENCODING = "gzip, deflate"


@contextmanager
def _bounded_reads(response: httpx.Response, deadline: float | None) -> Iterator[None]:
    """
    Jeden Lesevorgang auf der Verbindung der gestreamten Antwort auf die Zeit bis ``deadline`` begrenzen.

    Die Prüfung nach jedem gelesenen Stück greift nicht, solange kein Stück ankommt: etwa wenn ein Server die
    Rahmung der Antwort (Chunk-Kopf) Byte für Byte schickt, jedes knapp vor dem Lese-Timeout. ``httpcore`` liest
    dann weiter von der Verbindung, jeweils mit dem Lese-Timeout der Anfrage. Hier bekommt jeder dieser Lesevorgänge
    höchstens die Restzeit, und nach der Frist beginnt keiner mehr (``httpcore.ReadTimeout``, bei ``httpx`` als
    ``ReadTimeout``). Ohne Thread: Die Grenze sitzt am Lesen selbst. Ohne Zugriff auf die Verbindung (Antwort aus
    einem Ersatz-Transport) bleibt es bei der Prüfung nach jedem Stück.
    """
    stream: Any = (getattr(response, "extensions", None) or {}).get("network_stream")
    read = getattr(stream, "read", None)
    if deadline is None or read is None:
        yield
        return

    def bounded(max_bytes: int, timeout: float | None = None) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpcore.ReadTimeout("Antwort dauert insgesamt zu lange")
        return bytes(read(max_bytes, timeout=remaining if timeout is None else min(timeout, remaining)))

    stream.read = bounded
    try:
        yield
    finally:
        # Die Verbindung kann im Pool bleiben: den Lesevorgang für die nächste Anfrage wieder freigeben
        with suppress(AttributeError):
            del stream.read


def read_limited(
    response: httpx.Response, max_bytes: int, *, truncate: bool = False, deadline: float | None = None
) -> bytes:
    """Wie :func:`_read_limited`; die Frist gilt zusätzlich für jeden Lesevorgang der Verbindung (``_bounded_reads``)."""
    with _bounded_reads(response, deadline):
        return _read_limited(response, max_bytes, truncate=truncate, deadline=deadline)


def _read_limited(
    response: httpx.Response, max_bytes: int, *, truncate: bool = False, deadline: float | None = None
) -> bytes:
    """
    Rumpf einer gestreamten Antwort lesen, höchstens ``max_bytes``; der Aufrufer schließt die Antwort.

    Ohne Streaming läse ``httpx`` die ganze Antwort und entpackte sie, bevor ein Aufrufer ihre Größe prüfen kann;
    auch gestreamt entpackt es jedes empfangene Stück vollständig. Hier werden die rohen Bytes gelesen und selbst
    entpackt, nie mehr als bis zur Grenze: Darüber endet das Lesen mit ``ResponseTooLargeError`` oder, mit
    ``truncate``, beim Stand der Grenze (für robots.txt, deren Anfang gilt). Entpackt werden gzip und deflate
    (``ACCEPT_ENCODING``); andere und mehrfach kodierte Antworten weisen wir ab. ``deadline``
    (``time.monotonic()``) begrenzt die Gesamtdauer: geprüft nach jedem Stück, wie es aus dem Netz kommt. Eine
    Antwort, die nur tröpfelt, endet damit spätestens einen Lese-Timeout des Clients nach der Frist.
    """
    if response.is_stream_consumed:
        # Der Rumpf liegt schon im Speicher (nicht gestreamt gesendet, oder ein Ersatz-Transport im Test): Es gibt
        # nichts mehr zu begrenzen als das Ergebnis
        ready = response.content
        if len(ready) > max_bytes:
            if not truncate:
                raise ResponseTooLargeError("Antwort zu groß")
            return ready[:max_bytes]
        return ready
    encodings = [
        teil.strip().lower()
        for teil in response.headers.get("content-encoding", "").split(",")
        if teil.strip() and teil.strip().lower() != "identity"
    ]
    if len(encodings) > 1:
        raise httpx.DecodingError("Mehrfach kodierte Antwort", request=response.request)
    decoder: Any = None
    if encodings:
        if encodings[0] in ("gzip", "x-gzip"):
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encodings[0] == "deflate":
            decoder = zlib.decompressobj()
        else:
            raise httpx.DecodingError(f"Kodierung {encodings[0]} wird nicht gelesen", request=response.request)
    parts: list[bytes] = []
    size = 0
    # Ohne Stückgröße: Mit einer festen Größe sammelte httpx so lange, bis sie erreicht ist, und die Frist unten
    # käme bei einer tröpfelnden Antwort erst nach Stunden zur Prüfung
    for raw in response.iter_raw():
        pending = raw
        while pending:
            if decoder is None:
                chunk, pending = pending, b""
            else:
                try:
                    # Höchstens ein Byte über die Grenze entpacken: genug, um das Überschreiten zu erkennen
                    chunk = decoder.decompress(pending, max_bytes - size + 1)
                except zlib.error as exc:
                    raise httpx.DecodingError(str(exc), request=response.request) from None
                pending = decoder.unconsumed_tail
            if size + len(chunk) > max_bytes:
                if not truncate:
                    raise ResponseTooLargeError("Antwort zu groß")
                parts.append(chunk[: max_bytes - size])
                return b"".join(parts)
            parts.append(chunk)
            size += len(chunk)
        if deadline is not None and time.monotonic() > deadline:
            raise httpx.ReadTimeout("Antwort dauert insgesamt zu lange", request=response.request)
    return b"".join(parts)


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


def remaining(deadline: float | None) -> float | None:
    """Restzeit bis ``deadline`` (``time.monotonic()``), nie negativ; ``None`` ohne Frist."""
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def deadline_after(max_wait: float | None) -> float | None:
    """Frist ``max_wait`` Sekunden ab jetzt (``time.monotonic()``); ``None`` ohne Höchstwartezeit."""
    return None if max_wait is None else time.monotonic() + max_wait


def guard(
    *,
    sync_config: Any = None,
    kind: str = KIND_FILES,
    max_wait: float | None = None,
    deadline: float | None = None,
    check_robots: bool = True,
) -> BeforeRedirect:
    """
    ``before_redirect`` für :func:`send`: robots.txt des Ziels prüfen (Art ``kind``, Kennung der Anfrage, Ausnahme
    aus ``sync_config``), dann einen Zeitpunkt für den Ziel-Host reservieren (Abstand aus ``sync_config``).

    Wartezeiten auf robots.txt und Drossel (Web-Anfragen) teilen sich eine Frist über alle Weiterleitungen: ``deadline``
    (``time.monotonic()``, etwa gemeinsam mit der Prüfung vor der ersten Anfrage), sonst ``max_wait`` Sekunden ab
    dem Aufruf von ``guard``. Jeder Schritt wartet höchstens die Restzeit, sonst :class:`RedirectBusyError`. Ohne
    beides wird ohne Grenze gewartet. ``check_robots=False`` nur für den Abruf der robots.txt selbst (immer erlaubt).
    """
    if deadline is None:
        deadline = deadline_after(max_wait)

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
                decision = robots.check(url, kind, sync_config=sync_config, agent=agent, max_wait=remaining(deadline))
            except host_pacing.PacingBusyError:
                raise RedirectBusyError("Drossel je Host: kein freier Zeitpunkt", request=request) from None
            if not decision.allowed:
                raise RedirectBlockedError(decision, request=request)
        if not host_pacing.wait(url, sync_config=sync_config, max_wait=remaining(deadline)):
            raise RedirectBusyError("Drossel je Host: kein freier Zeitpunkt", request=request)

    return before_redirect
