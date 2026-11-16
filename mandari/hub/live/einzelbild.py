# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einzelbild aus einem HLS-Stream, nur im Arbeitsspeicher (Issue #915).

Ohne ffmpeg-Programm: Die Playlists liest ``httpx``, das Segment dekodiert PyAV (FFmpeg als Bibliothek).

1. Master-Playlist → Variante mit einer Höhe von mindestens 720 Bildpunkten, am nächsten an 1080 (ohne Angaben die
   erste Variante). Ist die Adresse schon eine Medien-Playlist, entfällt der Schritt.
2. Medien-Playlist → letztes Segment (bei fragmentiertem MP4 mit dem Initialisierungssegment davor).
3. Erstes dekodierbares Bild → Pillow-Bild.

Segment und Bild liegen nur im Speicher; weder Datei noch Datenbank noch Protokoll bekommen Bilddaten. Grenzen:
Größe des Segments (``http.MAX_SEGMENT``), Zeitgrenzen der Abrufe, höchstens ``MAX_PAKETE`` dekodierte Pakete,
höchstens ``MAX_BILDPUNKTE`` je Bild (die Größe des Segments begrenzt die Bildmaße nicht) und höchstens
``MAX_VARIANTEN`` Varianten und ``MAX_ZEILE`` Zeichen je ausgewerteter Playlist-Zeile. Playlists werden Zeile für
Zeile in einem Durchlauf gelesen, ohne Muster über den ganzen Text.
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
#: Größtes Bild, das dekodiert wird (4K); üblich sind 1280x720 und 1920x1080
MAX_BILDPUNKTE: Final = 3840 * 2160
#: FFmpeg lehnt größere Bilder schon beim Öffnen ab, bevor es Speicher für sie anlegt
_DEKODER_GRENZE: Final = {"max_pixels": str(MAX_BILDPUNKTE)}
#: So viele Varianten einer Master-Playlist werden höchstens gelesen
MAX_VARIANTEN: Final = 200
#: Längere Zeilen einer Playlist werden nicht nach Angaben durchsucht
MAX_ZEILE: Final = 2048
_AUFLOESUNG = re.compile(r"RESOLUTION=(\d{2,5})x(\d{2,5})", re.IGNORECASE)
_MAP: Final = "#EXT-X-MAP:"
_URI = re.compile(r'URI="([^"]+)"', re.IGNORECASE)


class KeinBildError(AnbieterError):
    """Aus dem Stream ließ sich kein Bild gewinnen; die Meldung ist ein fester Text."""


@dataclass(frozen=True)
class Variante:
    url: str
    hoehe: int | None


def varianten(master: str, basis: str) -> list[Variante]:
    """Varianten einer Master-Playlist (Adresse absolut, Höhe aus ``RESOLUTION``)."""
    # Ein Durchlauf: Jede Adresszeile gehört zu allen Angaben ``#EXT-X-STREAM-INF`` seit der letzten Adresszeile.
    # Für jede Angabe den Rest der Playlist nach ihrer Adresse abzusuchen, kostete bei vielen Angaben ohne
    # Adresse quadratische Zeit und hielte den Worker minutenlang auf.
    ergebnis: list[Variante] = []
    offen: list[int | None] = []
    for zeile in map(str.strip, master.splitlines()):
        if zeile.startswith("#EXT-X-STREAM-INF"):
            hoehe_treffer = _AUFLOESUNG.search(zeile) if len(zeile) <= MAX_ZEILE else None
            offen.append(int(hoehe_treffer.group(2)) if hoehe_treffer else None)
        elif zeile and not zeile.startswith("#"):
            ergebnis.extend(Variante(urljoin(basis, zeile), hoehe) for hoehe in offen)
            offen.clear()
        if len(ergebnis) + len(offen) > MAX_VARIANTEN:
            raise KeinBildError("Master-Playlist mit zu vielen Varianten")
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
    return urljoin(basis, segmente[-1]), _initialisierung(medien, basis)


def _initialisierung(medien: str, basis: str) -> str | None:
    """
    Adresse des Initialisierungssegments: die erste Zeile ``#EXT-X-MAP:`` mit ``URI``.

    Zeile für Zeile, die Adresse nur in der Zeile selbst gesucht: Ein Muster über den ganzen Text sucht von jeder
    Fundstelle bis zum Zeilenende und braucht bei vielen Fundstellen ohne Adresse in einer Zeile quadratische Zeit.
    """
    for zeile in map(str.strip, medien.splitlines()):
        if zeile[: len(_MAP)].upper() == _MAP and len(zeile) <= MAX_ZEILE:
            treffer = _URI.search(zeile)
            if treffer:
                return urljoin(basis, str(treffer.group(1)))
    return None


def bild_aus_segment(daten: bytes) -> Image.Image:
    """Erstes dekodierbares Bild eines Segments (MPEG-TS oder fragmentiertes MP4)."""
    import av  # erst hier: FFmpeg-Bibliotheken nur laden, wo Bilder gelesen werden

    try:
        # Die Grenze schon beim Öffnen: Bereits die Analyse der Spuren legt Speicher nach den Bildmaßen an
        with av.open(io.BytesIO(daten), mode="r", options=dict(_DEKODER_GRENZE)) as container:
            if not container.streams.video:
                raise KeinBildError("Segment ohne Videospur")
            spur = container.streams.video[0]
            spur.codec_context.options = dict(_DEKODER_GRENZE)
            if spur.codec_context.width * spur.codec_context.height > MAX_BILDPUNKTE:
                raise KeinBildError("Bild zu groß")
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
