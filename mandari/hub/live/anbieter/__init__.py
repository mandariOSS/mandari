# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anbieter-Adapter für Live-Übertragungen (Issue #915): kleine Schnittstelle, Register nach Code.

Ein Adapter übersetzt nur: Kennung beim Anbieter → Stream (``aufloesen``), Stream → Status (``status``),
Kennung → Adresse des Players (``einbettung_url``). Geschäftsregeln (Zeitfenster, Zustände, Entprellung) stehen in
``hub.live.services``. Neue Anbieter (YouTube, Vimeo, …) kommen als weiteres Modul mit ``registrieren``.

Alle Abrufe laufen über ``http.hole`` (Zeitgrenzen, Größengrenze, User-Agent „mandari (+https://mandari.de)“,
nur https). Rohdaten des Status kommen ohne Bild-Adressen (Vorschaubilder, Poster) zurück.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol


class Phase(enum.StrEnum):
    """Phase der Übertragung laut Anbieter."""

    VORHER = "vorher"
    LIVE = "live"
    NACHHER = "nachher"
    UNBEKANNT = "unbekannt"


@dataclass(frozen=True)
class StreamInfo:
    """Aufgelöster Stream: Kennung beim Anbieter, HLS-Quelle (für Einzelbilder), Player-Adresse."""

    stream_id: str
    hls_url: str | None = None
    embed_url: str | None = None

    def als_dict(self) -> dict[str, str | None]:
        return {"stream_id": self.stream_id, "hls_url": self.hls_url, "embed_url": self.embed_url}

    @classmethod
    def aus_dict(cls, daten: Mapping[str, Any]) -> StreamInfo | None:
        stream_id = daten.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id:
            return None
        hls = daten.get("hls_url")
        embed = daten.get("embed_url")
        return cls(
            stream_id=stream_id,
            hls_url=hls if isinstance(hls, str) and hls else None,
            embed_url=embed if isinstance(embed, str) and embed else None,
        )


@dataclass(frozen=True)
class StreamStatus:
    """Status einer Abfrage. ``roh`` enthält keine Bild-Adressen."""

    online: bool
    phase: Phase
    tafeltext: str = ""
    zuschauer: int | None = None
    roh: Mapping[str, Any] = field(default_factory=dict)


class AnbieterError(RuntimeError):
    """Abfrage beim Anbieter gescheitert; die Meldung ist ein fester Text ohne Antwortinhalt."""


class Anbieter(Protocol):
    """Schnittstelle eines Adapters."""

    code: str
    name: str
    #: Ursprünge der Player (für die Content-Security-Policy der Live-Seite), z. B. ``https://player.example``
    player_ursprung: tuple[str, ...]

    def kennung_gueltig(self, kennung: str) -> bool: ...

    def aufloesen(self, kennung: str) -> StreamInfo: ...

    def status(self, info: StreamInfo) -> StreamStatus: ...

    def einbettung_url(self, kennung: str) -> str | None: ...


_REGISTER: dict[str, Anbieter] = {}

#: Schlüssel in Rohdaten, die nie gespeichert werden (Bild-Adressen)
_BILD_SCHLUESSEL: Final = ("image", "poster", "thumb", "preview", "bild", "picture", "snapshot")


def registrieren(anbieter: Anbieter) -> Anbieter:
    """Nimmt einen Adapter ins Register auf (Code eindeutig)."""
    vorhanden = _REGISTER.get(anbieter.code)
    if vorhanden is not None and vorhanden is not anbieter:
        raise ValueError(f"Anbieter {anbieter.code!r} ist bereits registriert")
    _REGISTER[anbieter.code] = anbieter
    return anbieter


def anbieter(code: str) -> Anbieter:
    """Adapter zum Code; ``KeyError`` bei unbekanntem Code."""
    _laden()
    return _REGISTER[code]


def codes() -> tuple[str, ...]:
    _laden()
    return tuple(sorted(_REGISTER))


def player_urspruenge() -> tuple[str, ...]:
    """Alle Player-Ursprünge der registrierten Adapter (für ``frame-src``)."""
    _laden()
    return tuple(sorted({u for a in _REGISTER.values() for u in a.player_ursprung}))


def ohne_bilder(daten: Mapping[str, Any], *, max_laenge: int = 500) -> dict[str, Any]:
    """Flache Kopie ohne Bild-Adressen, nur einfache Werte, Texte gekürzt (für Protokoll und Zustand)."""
    sauber: dict[str, Any] = {}
    for schluessel, wert in list(daten.items())[:50]:
        name = str(schluessel)[:64]
        if any(teil in name.lower() for teil in _BILD_SCHLUESSEL):
            continue
        if isinstance(wert, bool | int | float) or wert is None:
            sauber[name] = wert
        elif isinstance(wert, str):
            if wert.lower().startswith(("http://", "https://")) and any(
                teil in wert.lower() for teil in (".jpg", ".jpeg", ".png", ".webp", ".gif")
            ):
                continue
            sauber[name] = wert[:max_laenge]
    return sauber


def _laden() -> None:
    # Adapter registrieren sich beim Import; die Module sind klein und ohne Nebenwirkungen
    from . import dreiq, hls  # noqa: F401


__all__ = [
    "Anbieter",
    "AnbieterError",
    "Phase",
    "StreamInfo",
    "StreamStatus",
    "anbieter",
    "codes",
    "ohne_bilder",
    "player_urspruenge",
    "registrieren",
]
