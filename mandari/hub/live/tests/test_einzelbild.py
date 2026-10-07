# SPDX-License-Identifier: AGPL-3.0-or-later
"""Einzelbild aus HLS ohne ffmpeg-Programm (Issue #915): Playlists, Variante, Segment, Dekodieren im Speicher."""

from __future__ import annotations

import httpx
import pytest

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
