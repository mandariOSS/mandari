# SPDX-License-Identifier: AGPL-3.0-or-later
"""Einzelbild aus HLS ohne ffmpeg-Programm (Issue #915): Playlists, Variante, Segment, Dekodieren im Speicher."""

from __future__ import annotations

import time

import httpx
import pytest

from hub.live import einzelbild as einzelbild_modul
from hub.live.anbieter import AnbieterError
from hub.live.einzelbild import (
    KeinBildError,
    Variante,
    bild_aus_segment,
    einzelbild,
    letztes_segment,
    varianten,
    waehle_variante,
)
from hub.live.tests.bilder import mpegts

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360/live.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
720/live.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
1080/live.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=3840x2160
2160/live.m3u8
"""

MEDIEN = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:100
#EXTINF:6.0,
seg100.ts
#EXTINF:6.0,
seg101.ts
"""


def test_variante_nahe_1080_mit_mindestens_720() -> None:
    liste = varianten(MASTER, "https://cdn.example/s/master.m3u8")
    assert [v.hoehe for v in liste] == [360, 720, 1080, 2160]
    assert liste[2].url == "https://cdn.example/s/1080/live.m3u8"
    assert waehle_variante(liste) == liste[2]
    assert waehle_variante(liste[:2]) == liste[1]
    assert waehle_variante(liste[:1]) == liste[0], "nur kleinere: die größte"
    ohne = [Variante("https://a.example/a.m3u8", None), Variante("https://a.example/b.m3u8", None)]
    assert waehle_variante(ohne) == ohne[0]
    assert waehle_variante([]) is None


def test_letztes_segment_und_initialisierung() -> None:
    assert letztes_segment(MEDIEN, "https://cdn.example/s/1080/live.m3u8") == (
        "https://cdn.example/s/1080/seg101.ts",
        None,
    )
    fmp4 = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6,\nseg1.m4s\n'
    assert letztes_segment(fmp4, "https://cdn.example/x/live.m3u8") == (
        "https://cdn.example/x/seg1.m4s",
        "https://cdn.example/x/init.mp4",
    )
    with pytest.raises(KeinBildError):
        letztes_segment("#EXTM3U\n", "https://cdn.example/x/live.m3u8")


def test_initialisierung_nur_aus_der_eigenen_zeile() -> None:
    basis = "https://cdn.example/x/live.m3u8"
    segment = "#EXTINF:6,\nseg1.m4s\n"

    def karte(playlist: str) -> str | None:
        return letztes_segment(playlist + segment, basis)[1]

    assert karte('#EXTM3U\r\n  #ext-x-map:BYTERANGE="720@0",uri="init.mp4"\r\n') == "https://cdn.example/x/init.mp4"
    # Die erste Zeile mit Adresse gilt; eine Zeile ohne Adresse übernimmt nicht die der nächsten
    assert karte('#EXT-X-MAP:BYTERANGE="1@0"\n#EXT-X-MAP:URI="a.mp4"\n#EXT-X-MAP:URI="b.mp4"\n') == (
        "https://cdn.example/x/a.mp4"
    )
    assert karte('#EXT-X-MAP:BYTERANGE="1@0"\n#EXT-X-KEY:METHOD=NONE,URI="k"\n') is None
    # Überlange Zeilen werden nicht ausgewertet
    assert karte("#EXT-X-MAP:" + "X" * einzelbild_modul.MAX_ZEILE + 'URI="init.mp4"\n') is None


def test_playlists_werden_in_linearer_zeit_gelesen() -> None:
    """
    Eine Playlist darf 1 MiB groß sein. Viele Angaben ohne Adresse, in einer Zeile oder in vielen, dürfen den Worker
    nicht aufhalten: Mit einem Muster über den ganzen Text bzw. einer Suche je Angabe dauerte das Minuten.
    """
    megabyte = 1024 * 1024
    eine_zeile = "#EXTM3U\n" + "#EXT-X-MAP:" * (megabyte // 11) + "\n#EXTINF:6,\nseg1.m4s\n"
    viele_zeilen = "#EXTM3U\n" + "#EXT-X-MAP:BYTERANGE\n" * (megabyte // 21) + "#EXTINF:6,\nseg1.m4s\n"
    master = "#EXTM3U\n" + "#EXT-X-STREAM-INF:BANDWIDTH=1\n" * 50_000

    beginn = time.perf_counter()
    assert letztes_segment(eine_zeile, "https://cdn.example/x/live.m3u8")[1] is None
    assert letztes_segment(viele_zeilen, "https://cdn.example/x/live.m3u8")[1] is None
    with pytest.raises(KeinBildError, match="zu vielen Varianten"):
        varianten(master, "https://cdn.example/s/master.m3u8")
    assert time.perf_counter() - beginn < 5.0


def test_mehrere_angaben_vor_einer_adresse_und_obergrenze() -> None:
    basis = "https://cdn.example/s/master.m3u8"
    doppelt = "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=640x360\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\n\n# x\na.m3u8\n"
    assert varianten(doppelt, basis) == [
        Variante("https://cdn.example/s/a.m3u8", 360),
        Variante("https://cdn.example/s/a.m3u8", 720),
    ]
    # Angabe ohne folgende Adresse: keine Variante
    assert varianten("#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=640x360\n", basis) == []
    voll = "#EXTM3U\n" + "#EXT-X-STREAM-INF:RESOLUTION=1280x720\nv.m3u8\n" * einzelbild_modul.MAX_VARIANTEN
    assert len(varianten(voll, basis)) == einzelbild_modul.MAX_VARIANTEN
    with pytest.raises(KeinBildError):
        varianten(voll + "#EXT-X-STREAM-INF:RESOLUTION=1280x720\nv.m3u8\n", basis)


def test_zu_grosses_bild_wird_nicht_dekodiert(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Die Größe des Segments begrenzt die Bildmaße nicht: Wenige Kilobyte können ein Bild mit vielen Megapixeln
    beschreiben. Der Test senkt die Grenze, statt ein riesiges Bild zu erzeugen.
    """
    segment = mpegts(groesse=(320, 180))

    # Grenze des Dekoders (greift beim Öffnen und Dekodieren, vor dem Bild im Speicher)
    with monkeypatch.context() as gesenkt:
        gesenkt.setattr(einzelbild_modul, "_DEKODER_GRENZE", {"max_pixels": str(100 * 100)})
        with pytest.raises(KeinBildError):
            bild_aus_segment(segment)

    # Eigene Prüfung der gemeldeten Maße
    with monkeypatch.context() as gesenkt:
        gesenkt.setattr(einzelbild_modul, "MAX_BILDPUNKTE", 100 * 100)
        with pytest.raises(KeinBildError, match="Bild zu groß"):
            bild_aus_segment(segment)

    assert {"max_pixels": str(3840 * 2160)} == einzelbild_modul._DEKODER_GRENZE
    assert bild_aus_segment(segment).size == (320, 180)


def test_bild_aus_segment_im_speicher() -> None:
    bild = bild_aus_segment(mpegts(groesse=(320, 180)))
    assert bild.size == (320, 180)


def test_kaputtes_segment() -> None:
    with pytest.raises(KeinBildError):
        bild_aus_segment(b"\x00" * 4096)


def test_einzelbild_ueber_master_und_medien_playlist() -> None:
    segment = mpegts()
    abgerufen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        abgerufen.append(str(request.url))
        pfad = request.url.path
        if pfad == "/s/master.m3u8":
            return httpx.Response(200, text=MASTER)
        if pfad == "/s/1080/live.m3u8":
            return httpx.Response(200, text=MEDIEN)
        if pfad == "/s/1080/seg101.ts":
            return httpx.Response(200, content=segment)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bild = einzelbild("https://cdn.example/s/master.m3u8", http=client)
    assert bild.size == (320, 180)
    assert abgerufen == [
        "https://cdn.example/s/master.m3u8",
        "https://cdn.example/s/1080/live.m3u8",
        "https://cdn.example/s/1080/seg101.ts",
    ]


def test_einzelbild_offline() -> None:
    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client,
        pytest.raises(AnbieterError),
    ):
        einzelbild("https://cdn.example/s/master.m3u8", http=client)
