# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Adapter „hls“ (Issue #915): allgemeiner HLS-Stream ohne Status-Schnittstelle.

Die Kennung ist die https-Adresse der Playlist. Live heißt: Die Playlist ist erreichbar und eine HLS-Playlist
(``#EXTM3U``) ohne ``#EXT-X-ENDLIST``. Ein Player wird nicht eingebettet; die Live-Seite verlinkt die Seite der
Kommune (``page_url`` der Quelle).
"""

from __future__ import annotations

import hashlib
from typing import Final

from . import AnbieterError, Phase, StreamInfo, StreamStatus, registrieren
from .http import NichtGefundenError, hole_text

_MAX_KENNUNG: Final = 1000


class Hls:
    """Adapter für einen HLS-Stream ohne Anbieter-Schnittstelle."""

    code = "hls"
    name = "HLS (allgemein)"
    player_ursprung: tuple[str, ...] = ()

    def kennung_gueltig(self, kennung: str) -> bool:
        return kennung.startswith("https://") and len(kennung) <= _MAX_KENNUNG and not any(z.isspace() for z in kennung)

    def aufloesen(self, kennung: str) -> StreamInfo:
        if not self.kennung_gueltig(kennung):
            raise AnbieterError("ungültige HLS-Adresse")
        stream_id = hashlib.sha256(kennung.encode()).hexdigest()[:16]
        return StreamInfo(stream_id=stream_id, hls_url=kennung, embed_url=None)

    def status(self, info: StreamInfo) -> StreamStatus:
        if not info.hls_url:
            raise AnbieterError("keine HLS-Adresse")
        try:
            playlist = hole_text(info.hls_url)
        except NichtGefundenError:
            return StreamStatus(online=False, phase=Phase.UNBEKANNT)
        if not playlist.lstrip().startswith("#EXTM3U"):
            raise AnbieterError("keine HLS-Playlist")
        if "#EXT-X-ENDLIST" in playlist:
            return StreamStatus(online=True, phase=Phase.NACHHER)
        return StreamStatus(online=True, phase=Phase.LIVE)

    def einbettung_url(self, kennung: str) -> str | None:
        return None


registrieren(Hls())
