# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufe bei Streaming-Anbietern (Issue #915): nur https, Zeitgrenzen, Größengrenze, fester User-Agent.

Fehler werden zu ``AnbieterError`` mit festem Text (Statuscode, Zeitgrenze, zu groß); Antwortinhalte gelangen
nie in Meldungen.
"""

from __future__ import annotations

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


class NichtGefundenError(AnbieterError):
    """Die Adresse antwortet mit 404 (bei 3Q und HLS: Stream offline)."""


def client() -> httpx.Client:
    """Neuer Client mit Zeitgrenzen und User-Agent; Aufrufer schließt ihn (``with``)."""
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def hole(url: str, *, max_bytes: int = MAX_TEXT, http: httpx.Client | None = None) -> bytes:
    """Inhalt einer https-Adresse, höchstens ``max_bytes``; wirft ``AnbieterError``."""
    if not url.startswith("https://"):
        raise AnbieterError("nur https-Adressen")
    eigener = http is None
    verbindung = http or client()
    try:
        with verbindung.stream("GET", url) as antwort:
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
    except httpx.TimeoutException as fehler:
        raise AnbieterError("Zeitgrenze") from fehler
    except httpx.HTTPError as fehler:
        raise AnbieterError(f"Verbindungsfehler ({type(fehler).__name__})") from fehler
    finally:
        if eigener:
            verbindung.close()


def hole_json(url: str, *, http: httpx.Client | None = None) -> Any:
    """JSON einer https-Adresse; wirft ``AnbieterError``."""
    try:
        return json.loads(hole(url, http=http))
    except (ValueError, UnicodeDecodeError) as fehler:
        raise AnbieterError("kein gültiges JSON") from fehler


def hole_text(url: str, *, http: httpx.Client | None = None) -> str:
    """Text (UTF-8) einer https-Adresse; wirft ``AnbieterError``."""
    return hole(url, http=http).decode("utf-8", errors="replace")
