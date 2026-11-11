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
import io
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

#: Grenzen für Bilder fremder Server je Dokument
MAX_REMOTE_IMAGES = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_SECONDS = 5.0
TOTAL_SECONDS = 20.0

#: Grenze für eigene Dateien (Logo)
MAX_FILE_BYTES = 5 * 1024 * 1024

_REMOTE_IMG = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(https?://[^"'\s>]+)\2""", re.IGNORECASE)


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


def _fetch(url: str, seconds: float) -> str:
    """Ein Bild laden und als ``data:``-Adresse liefern; leer, wenn es sich nicht laden lässt."""
    from insight_core.services import safe_fetch

    target = io.BytesIO()
    try:
        download = safe_fetch.download_to(
            target,
            url,
            max_bytes=MAX_IMAGE_BYTES,
            total_seconds=seconds,
            timeout=httpx.Timeout(min(seconds, IMAGE_SECONDS)),
            # Unkomprimiert: Die Größengrenze zählt dann die empfangenen Bytes
            headers={"Accept-Encoding": "identity"},
        )
    except (httpx.HTTPError, safe_fetch.TooLargeError, safe_fetch.DeadlineExceededError) as exc:
        logger.info("Bild für das PDF nicht geladen (%s)", type(exc).__name__)
        return ""
    content_type = download.content_type.split(";")[0].strip().lower()
    if content_type not in IMAGE_TYPES:
        return ""
    return _data_uri(content_type, target.getvalue())


def inline_remote_images(html: str) -> str:
    """
    Bilder fremder Server (``<img src="http…">``) durch eingebettete ersetzen.

    Höchstens ``MAX_REMOTE_IMAGES`` verschiedene Adressen und ``TOTAL_SECONDS`` je Dokument. Bilder darüber hinaus
    und solche, die sich nicht laden lassen, verlieren ihre Adresse und fehlen im PDF.
    """
    if "<img" not in html.lower():
        return html
    geladen: dict[str, str] = {}
    deadline = time.monotonic() + TOTAL_SECONDS

    def ersetzen(match: re.Match[str]) -> str:
        url = match.group(3)
        if url not in geladen:
            rest = deadline - time.monotonic()
            if len(geladen) >= MAX_REMOTE_IMAGES or rest <= 0:
                geladen[url] = ""
            else:
                geladen[url] = _fetch(url, min(rest, IMAGE_SECONDS))
        return f"{match.group(1)}{match.group(2)}{geladen[url]}{match.group(2)}"

    return _REMOTE_IMG.sub(ersetzen, html)
