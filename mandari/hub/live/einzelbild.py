# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einzelbild aus einem HLS-Stream, nur im Arbeitsspeicher (Issue #915).

Ohne ffmpeg-Programm: Die Playlists liest ``httpx``, das Segment dekodiert PyAV (FFmpeg als Bibliothek).

1. Master-Playlist → Variante mit einer Höhe von mindestens 720 Bildpunkten, am nächsten an 1080 (ohne Angaben die
   erste Variante). Ist die Adresse schon eine Medien-Playlist, entfällt der Schritt.
2. Medien-Playlist → letztes Segment (bei fragmentiertem MP4 mit dem Initialisierungssegment davor).
3. Erstes dekodierbares Bild → Pillow-Bild.

Segment und Bild liegen nur im Speicher; weder Datei noch Datenbank noch Protokoll bekommen Bilddaten. Grenzen:
Größe des Segments (``http.MAX_SEGMENT``), Zeitgrenzen der Abrufe, höchstens ``MAX_PAKETE`` dekodierte Pakete.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin

import httpx
from PIL import Image

from .anbieter import AnbieterError
from .anbieter.http import MAX_SEGMENT, client, hole, hole_text

#: Bevorzugte Höhe der Variante
ZIELHOEHE: Final = 1080
MINDESTHOEHE: Final = 720
#: So viele Pakete werden höchstens dekodiert, bis ein Bild entsteht
MAX_PAKETE: Final = 600
_AUFLOESUNG = re.compile(r"RESOLUTION=(\d{2,5})x(\d{2,5})", re.IGNORECASE)
_MAP_URI = re.compile(r'#EXT-X-MAP:.*?URI="([^"]+)"', re.IGNORECASE)


class KeinBildError(AnbieterError):
    """Aus dem Stream ließ sich kein Bild gewinnen; die Meldung ist ein fester Text."""


@dataclass(frozen=True)
class Variante:
    url: str
    hoehe: int | None


def varianten(master: str, basis: str) -> list[Variante]:
    """Varianten einer Master-Playlist (Adresse absolut, Höhe aus ``RESOLUTION``)."""
    ergebnis: list[Variante] = []
    zeilen = [z.strip() for z in master.splitlines()]
    for nummer, zeile in enumerate(zeilen):
        if not zeile.startswith("#EXT-X-STREAM-INF"):
            continue
        hoehe_treffer = _AUFLOESUNG.search(zeile)
        hoehe = int(hoehe_treffer.group(2)) if hoehe_treffer else None
        for folge in zeilen[nummer + 1 :]:
            if folge and not folge.startswith("#"):
                ergebnis.append(Variante(urljoin(basis, folge), hoehe))
                break
    return ergebnis


def waehle_variante(liste: list[Variante]) -> Variante | None:
    """Höhe ≥ 720, am nächsten an 1080; sonst die höchste; ohne Höhenangaben die erste."""
    if not liste:
        return None
    mit_hoehe = [v for v in liste if v.hoehe]
    if not mit_hoehe:
        return liste[0]
    passend = [v for v in mit_hoehe if (v.hoehe or 0) >= MINDESTHOEHE]
    if passend:
        return min(passend, key=lambda v: abs((v.hoehe or 0) - ZIELHOEHE))
    return max(mit_hoehe, key=lambda v: v.hoehe or 0)


def letztes_segment(medien: str, basis: str) -> tuple[str, str | None]:
    """(Adresse des letzten Segments, Adresse des Initialisierungssegments oder ``None``)."""
    segmente = [z.strip() for z in medien.splitlines() if z.strip() and not z.strip().startswith("#")]
    if not segmente:
        raise KeinBildError("Playlist ohne Segment")
    karte = _MAP_URI.search(medien)
    return urljoin(basis, segmente[-1]), urljoin(basis, karte.group(1)) if karte else None


def bild_aus_segment(daten: bytes) -> Image.Image:
    """Erstes dekodierbares Bild eines Segments (MPEG-TS oder fragmentiertes MP4)."""
    import av  # erst hier: FFmpeg-Bibliotheken nur laden, wo Bilder gelesen werden

    try:
        with av.open(io.BytesIO(daten), mode="r") as container:
            if not container.streams.video:
                raise KeinBildError("Segment ohne Videospur")
            spur = container.streams.video[0]
            for nummer, paket in enumerate(container.demux(spur)):
                if nummer >= MAX_PAKETE:
                    break
                for bild in paket.decode():
                    if isinstance(bild, av.VideoFrame):
                        ergebnis: Image.Image = bild.to_image()  # type: ignore[no-untyped-call]
                        return ergebnis
    except av.FFmpegError as fehler:
        raise KeinBildError(f"Segment nicht dekodierbar ({type(fehler).__name__})") from fehler
    raise KeinBildError("kein dekodierbares Bild im Segment")


def einzelbild(hls_url: str, *, http: httpx.Client | None = None) -> Image.Image:
    """Aktuelles Einzelbild eines HLS-Streams; wirft ``AnbieterError``/``KeinBildError``."""
    eigener = http is None
    verbindung = http or client()
    try:
        playlist = hole_text(hls_url, http=verbindung)
        adresse = hls_url
        if "#EXT-X-STREAM-INF" in playlist:
            variante = waehle_variante(varianten(playlist, hls_url))
            if variante is None:
                raise KeinBildError("Master-Playlist ohne Variante")
            adresse = variante.url
            playlist = hole_text(adresse, http=verbindung)
        segment_url, init_url = letztes_segment(playlist, adresse)
        daten = hole(segment_url, max_bytes=MAX_SEGMENT, http=verbindung)
        if init_url:
            daten = hole(init_url, max_bytes=MAX_SEGMENT, http=verbindung) + daten
        try:
            return bild_aus_segment(daten)
        finally:
            del daten
    finally:
        if eigener:
            verbindung.close()
