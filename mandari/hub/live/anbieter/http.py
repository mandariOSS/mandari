# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufe bei Streaming-Anbietern (Issue #915): nur https, Zeitgrenzen, Größengrenze, fester User-Agent.

Jede Adresse, auch jedes Ziel einer Weiterleitung (höchstens ``MAX_WEITERLEITUNGEN``), muss https sein und auf
einen öffentlichen Namen zeigen (``ziel_pruefen``): keine IP-Adresse aus privaten, Loopback- oder Link-Local-Bereichen,
kein ``localhost``, kein Name ohne Punkt (Dienstnamen im Container-Netz) und keine Zahl als Name. Namen, die erst
bei der Auflösung auf eine private Adresse zeigen, fängt das nicht ab (Folgearbeit).

Fehler werden zu ``AnbieterError`` mit festem Text (Statuscode, Zeitgrenze, zu groß); Antwortinhalte gelangen
nie in Meldungen.
"""

from __future__ import annotations

import ipaddress
import json
from typing import Any, Final

import httpx

from . import AnbieterError

USER_AGENT: Final = "mandari (+https://mandari.de)"
#: Zeitgrenzen in Sekunden: Verbindungsaufbau, Lesen
TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
#: Größengrenze für JSON und Playlists
MAX_TEXT: Final = 1024 * 1024
#: Größengrenze für ein Videosegment
MAX_SEGMENT: Final = 24 * 1024 * 1024
#: So vielen Weiterleitungen folgt ein Abruf höchstens
MAX_WEITERLEITUNGEN: Final = 5


class NichtGefundenError(AnbieterError):
    """Die Adresse antwortet mit 404 (bei 3Q und HLS: Stream offline)."""


def client() -> httpx.Client:
    """Neuer Client mit Zeitgrenzen und User-Agent; Aufrufer schließt ihn (``with``)."""
    # Weiterleitungen folgt ``hole`` selbst, damit jedes Ziel geprüft wird
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=False)


def ziel_pruefen(url: str | httpx.URL) -> httpx.URL:
    """Adresse für einen Abruf: https und ein öffentlicher Name (siehe Moduldokumentation); wirft ``AnbieterError``."""
    try:
        ziel = url if isinstance(url, httpx.URL) else httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError) as fehler:
        raise AnbieterError("ungültige Adresse") from fehler
    if ziel.scheme != "https" or not ziel.host:
        raise AnbieterError("nur https-Adressen")
    host = ziel.host.rstrip(".").lower()
    try:
        adresse = ipaddress.ip_address(host)
    except ValueError:
        adresse = None
    if adresse is not None:
        if not adresse.is_global:
            raise AnbieterError("Adresse im eigenen Netz")
        return ziel
    endung = host.rsplit(".", 1)[-1]
    if "." not in host or host.endswith(".localhost") or not (endung.isalpha() or endung.startswith("xn--")):
        raise AnbieterError("Adresse im eigenen Netz")
    return ziel


def hole(url: str, *, max_bytes: int = MAX_TEXT, http: httpx.Client | None = None) -> bytes:
    """Inhalt einer https-Adresse, höchstens ``max_bytes``; wirft ``AnbieterError``."""
    ziel = ziel_pruefen(url)
    eigener = http is None
    verbindung = http or client()
    try:
        for _ in range(MAX_WEITERLEITUNGEN + 1):
            with verbindung.stream("GET", ziel, follow_redirects=False) as antwort:
                if antwort.is_redirect:
                    ziel = ziel_pruefen(antwort.url.join(antwort.headers["location"]))
                    continue
                return _inhalt(antwort, max_bytes)
        raise AnbieterError("zu viele Weiterleitungen")
    except httpx.TimeoutException as fehler:
        raise AnbieterError("Zeitgrenze") from fehler
    except httpx.HTTPError as fehler:
        raise AnbieterError(f"Verbindungsfehler ({type(fehler).__name__})") from fehler
    finally:
        if eigener:
            verbindung.close()


def _inhalt(antwort: httpx.Response, max_bytes: int) -> bytes:
    if antwort.status_code == 404:
        raise NichtGefundenError("HTTP 404")
    if antwort.status_code != 200:
        raise AnbieterError(f"HTTP {antwort.status_code}")
    laenge = antwort.headers.get("content-length")
    if laenge and laenge.isdigit() and int(laenge) > max_bytes:
        raise AnbieterError("Antwort zu groß")
    teile: list[bytes] = []
    gelesen = 0
    for teil in antwort.iter_bytes():
        gelesen += len(teil)
        if gelesen > max_bytes:
            raise AnbieterError("Antwort zu groß")
        teile.append(teil)
    return b"".join(teile)


def hole_json(url: str, *, http: httpx.Client | None = None) -> Any:
    """JSON einer https-Adresse; wirft ``AnbieterError``."""
    try:
        return json.loads(hole(url, http=http))
    except (ValueError, UnicodeDecodeError) as fehler:
        raise AnbieterError("kein gültiges JSON") from fehler


def hole_text(url: str, *, http: httpx.Client | None = None) -> str:
    """Text (UTF-8) einer https-Adresse; wirft ``AnbieterError``."""
    return hole(url, http=http).decode("utf-8", errors="replace")
