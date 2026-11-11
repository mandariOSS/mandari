# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bilder für die PDF-Erzeugung vorab einbetten.

``apps.common.pdf.html_to_pdf`` liest beim Umwandeln weder lokale Dateien noch fremde Server (enge Richtlinie der
Bibliothek). Bilder, die ein PDF zeigen soll, stehen deshalb als ``data:``-Adresse im HTML:

- :func:`file_data_uri` für eigene Dateien (Logo im Briefkopf), gelesen über die Ablage statt über einen Pfad.
- :func:`inline_remote_images` für Bilder fremder Server im Text eines Dokuments: Sie werden vor dem Umwandeln über
  den abgesicherten Abruf geladen (nur öffentliche Adressen, an die geprüfte Adresse gebunden, mit Größen- und
  Zeitgrenze) und eingebettet. Was sich nicht laden lässt, fehlt im PDF; der Export selbst gelingt.
"""

from __future__ import annotations

import base64
import concurrent.futures
import html as html_lib
import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Bildarten, die eingebettet werden (wie die Positivliste des Editors für ``data:``-Bilder)
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif", "image/webp": "webp"}
_SUFFIX_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

#: Grenzen für Bilder fremder Server je Dokument: verschiedene Adressen, Größe und Zeit je Bild, Zeit insgesamt
#: und die Menge eingebetteter Daten (dieselbe Adresse kann im Text beliebig oft stehen)
MAX_REMOTE_IMAGES = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_SECONDS = 5.0
TOTAL_SECONDS = 20.0
MAX_EMBEDDED_BYTES = 30 * 1024 * 1024
MAX_REDIRECTS = 3

#: Grenze für eigene Dateien (Logo)
MAX_FILE_BYTES = 5 * 1024 * 1024

_REMOTE_IMG = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(https?://[^"'\s>]+)\2""", re.IGNORECASE)


def _now() -> float:
    """Uhr der Fristen (in Tests ersetzbar)."""
    return time.monotonic()


def _data_uri(content_type: str, data: bytes) -> str:
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


def file_data_uri(field_file: Any) -> str:
    """Eigene Bilddatei (``FieldFile``) als ``data:``-Adresse; leer, wenn sie fehlt, zu groß oder kein Bild ist."""
    name = str(getattr(field_file, "name", "") or "")
    content_type = next((typ for suffix, typ in _SUFFIX_TYPES.items() if name.lower().endswith(suffix)), "")
    if not name or not content_type:
        return ""
    try:
        with field_file.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except (OSError, ValueError):
        return ""
    if not data or len(data) > MAX_FILE_BYTES:
        return ""
    return _data_uri(content_type, data)


def _fetch_bounded(url: str, deadline: float) -> str:
    """
    Wie :func:`_fetch`, aber der Aufrufer wartet höchstens bis ``deadline``: Der Abruf läuft in einem eigenen
    Thread, und nach der Frist geht der Export ohne das Bild weiter. Verbindungsaufbau, Antwortkopf und ein
    Stillstand kurz vor der Frist verlängern ihn so nicht. Der Thread selbst endet durch die Wartezeiten des
    Abrufs von allein.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-bild")
    future = executor.submit(_fetch, url, deadline)
    try:
        return future.result(timeout=max(0.1, deadline - _now()))
    except concurrent.futures.TimeoutError:
        logger.info("Bild für das PDF nicht geladen (Frist)")
        return ""
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _fetch(url: str, deadline: float) -> str:
    """
    Ein Bild laden und als ``data:``-Adresse liefern; leer, wenn es sich nicht laden lässt.

    Der Rumpf wird so gelesen, wie er ankommt, und nach jedem Stück gegen ``deadline`` (``time.monotonic()``) und
    die Größengrenze geprüft; ohne Daten greift die Wartezeit je Lesevorgang. Die harte Frist für den Aufrufer
    setzt :func:`_fetch_bounded`.
    """
    from insight_core.services import safe_fetch

    seconds = max(0.1, min(deadline - _now(), IMAGE_SECONDS))
    parts: list[bytes] = []
    size = 0
    try:
        with (
            safe_fetch.guarded_client(
                timeout=httpx.Timeout(seconds), follow_redirects=True, max_redirects=MAX_REDIRECTS
            ) as client,
            # Unkomprimiert: Die Größengrenze zählt die empfangenen Bytes
            client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response,
        ):
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if response.status_code != 200 or content_type not in IMAGE_TYPES:
                return ""
            if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                return ""
            for chunk in response.iter_raw():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES or _now() > deadline:
                    return ""
                parts.append(chunk)
    except httpx.HTTPError as exc:
        logger.info("Bild für das PDF nicht geladen (%s)", type(exc).__name__)
        return ""
    return _data_uri(content_type, b"".join(parts)) if parts else ""


def inline_remote_images(html: str) -> str:
    """
    Bilder fremder Server (``<img src="http…">``) durch eingebettete ersetzen.

    Höchstens ``MAX_REMOTE_IMAGES`` verschiedene Adressen, ``TOTAL_SECONDS`` und ``MAX_EMBEDDED_BYTES`` eingebettete
    Daten je Dokument. Bilder darüber hinaus und solche, die sich nicht laden lassen, verlieren ihre Adresse und
    fehlen im PDF.
    """
    if "<img" not in html.lower():
        return html
    geladen: dict[str, str] = {}
    deadline = _now() + TOTAL_SECONDS
    budget = MAX_EMBEDDED_BYTES

    def ersetzen(match: re.Match[str]) -> str:
        nonlocal budget
        # Der Attributwert steht im HTML maskiert (``&amp;`` in Adressen mit mehreren Parametern)
        url = html_lib.unescape(match.group(3))
        if url not in geladen:
            if len(geladen) >= MAX_REMOTE_IMAGES or _now() >= deadline:
                geladen[url] = ""
            else:
                geladen[url] = _fetch_bounded(url, min(deadline, _now() + IMAGE_SECONDS))
        eingebettet = geladen[url]
        # Dieselbe Adresse kann beliebig oft im Text stehen: Jede Stelle zählt gegen das Budget
        if len(eingebettet) > budget:
            eingebettet = ""
        budget -= len(eingebettet)
        return f"{match.group(1)}{match.group(2)}{eingebettet}{match.group(2)}"

    return _REMOTE_IMG.sub(ersetzen, html)
