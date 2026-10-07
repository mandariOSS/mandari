# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Adapter „3q“ (Issue #915): Player-Konfiguration und Streamstatus von 3Q (playout.3qsdn.com).

- **Kennung** ist die Embed-ID des Players (UUID-artig).
- **Auflösen:** ``GET /config/<embed-id>?key=0&timestamp=0`` liefert ``stream`` (Nummer) und ``sources.hls``.
- **Status:** ``GET /streamstatus?id=<stream>`` liefert eine Liste mit ``IsOnline``, ``Listeners`` und ``Metadata``
  (``playoutState``, ``boardtitleline1``). ``pre`` und ``post`` sind Tafeln vor bzw. nach der Sitzung; nach dem
  Ende sendet der Stream oft ein Standbild weiter (online, aber nicht live). Den genauen Wert für „live“ kennen
  wir nicht: live heißt online und weder ``pre`` noch ``post``. Die Tafel (``boardtitleline1``, etwa „Die Sitzung
  wurde um 18:25 beendet“) zählt nur, wenn 3Q keinen Zustand meldet.
- **Einbettung:** ``https://playout.3qsdn.com/embed/<embed-id>``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import quote

from . import AnbieterError, Phase, StreamInfo, StreamStatus, ohne_bilder, registrieren
from .http import hole_json

BASIS: Final = "https://playout.3qsdn.com"
_KENNUNG: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$")
_STREAM: Final = re.compile(r"^\d{1,12}$")
#: Tafeltexte, die das Ende der Sitzung melden
_ENDE: Final = re.compile(r"\b(beendet|geschlossen|ende der (sitzung|übertragung))\b", re.IGNORECASE)


class DreiQ:
    """Adapter für 3Q."""

    code = "3q"
    name = "3Q"
    player_ursprung: tuple[str, ...] = (BASIS,)

    def kennung_gueltig(self, kennung: str) -> bool:
        return bool(_KENNUNG.fullmatch(kennung))

    def _kennung(self, kennung: str) -> str:
        if not self.kennung_gueltig(kennung):
            raise AnbieterError("ungültige Embed-ID")
        return kennung

    def aufloesen(self, kennung: str) -> StreamInfo:
        konfiguration = hole_json(f"{BASIS}/config/{quote(self._kennung(kennung))}?key=0&timestamp=0")
        if not isinstance(konfiguration, Mapping):
            raise AnbieterError("Konfiguration ohne Objekt")
        stream = konfiguration.get("stream")
        if isinstance(stream, bool) or not isinstance(stream, int | str) or not _STREAM.fullmatch(str(stream)):
            raise AnbieterError("Konfiguration ohne Streamnummer")
        quellen = konfiguration.get("sources")
        hls = quellen.get("hls") if isinstance(quellen, Mapping) else None
        return StreamInfo(
            stream_id=str(stream),
            hls_url=hls if isinstance(hls, str) and hls.startswith("https://") else None,
            embed_url=self.einbettung_url(kennung),
        )

    def status(self, info: StreamInfo) -> StreamStatus:
        if not _STREAM.fullmatch(info.stream_id):
            raise AnbieterError("ungültige Streamnummer")
        antwort = hole_json(f"{BASIS}/streamstatus?id={info.stream_id}")
        eintrag = antwort[0] if isinstance(antwort, list) and antwort else antwort
        if not isinstance(eintrag, Mapping):
            raise AnbieterError("Status ohne Eintrag")
        return status_aus_antwort(eintrag)

    def einbettung_url(self, kennung: str) -> str | None:
        return f"{BASIS}/embed/{quote(kennung)}" if self.kennung_gueltig(kennung) else None


def _zuschauer(wert: Any) -> int | None:
    if isinstance(wert, bool):
        return None
    if isinstance(wert, int):
        return max(0, wert)
    if isinstance(wert, str) and wert.strip().isdigit():
        return int(wert.strip())
    return None


def status_aus_antwort(eintrag: Mapping[str, Any]) -> StreamStatus:
    """Status aus einem Eintrag der Antwort von ``/streamstatus`` (ohne Bild-Adressen)."""
    online = eintrag.get("IsOnline") is True
    metadaten = eintrag.get("Metadata")
    metadaten = metadaten if isinstance(metadaten, Mapping) else {}
    zustand = str(metadaten.get("playoutState") or "").strip().lower()
    tafel = str(metadaten.get("boardtitleline1") or "").strip()[:300]
    # Der Zustand geht vor; die Tafel nur, wenn 3Q keinen Zustand meldet (sie kann vom letzten Mal stehen bleiben)
    if zustand == "post" or (not zustand and tafel and _ENDE.search(tafel)):
        phase = Phase.NACHHER
    elif zustand == "pre":
        phase = Phase.VORHER
    elif online:
        phase = Phase.LIVE
    else:
        phase = Phase.UNBEKANNT
    roh = {
        **ohne_bilder({k: v for k, v in eintrag.items() if k != "Metadata"}),
        "Metadata": ohne_bilder(metadaten),
    }
    return StreamStatus(
        online=online, phase=phase, tafeltext=tafel, zuschauer=_zuschauer(eintrag.get("Listeners")), roh=roh
    )


registrieren(DreiQ())
