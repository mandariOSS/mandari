# SPDX-License-Identifier: AGPL-3.0-or-later
"""Anbieter-Adapter (Issue #915): 3Q und allgemeines HLS mit gemockten Abrufen, Register, Grenzen der Abrufe."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from hub.live.anbieter import (
    AnbieterError,
    Phase,
    StreamInfo,
    anbieter,
    codes,
    dreiq,
    hls,
    ohne_bilder,
    player_urspruenge,
)
from hub.live.anbieter import http as abruf

EMBED = "0a1b2c3d-4e5f-11ee-8a9b-0c1d2e3f4a5b"


def test_register_kennt_3q_und_hls() -> None:
    assert {"3q", "hls"} <= set(codes())
    assert anbieter("3q").name == "3Q"
    assert player_urspruenge() == ("https://playout.3qsdn.com",), "nur 3Q bettet einen Player ein"
    with pytest.raises(KeyError):
        anbieter("unbekannt")


def test_3q_aufloesen(monkeypatch: pytest.MonkeyPatch) -> None:
    abgerufen: list[str] = []

    def hole_json(url: str, **_: Any) -> Any:
        abgerufen.append(url)
        return {"stream": 63961, "sources": {"hls": "https://cdn.example/63961/63961_264_live.m3u8"}, "disableDVR": 1}

    monkeypatch.setattr(dreiq, "hole_json", hole_json)
    info = anbieter("3q").aufloesen(EMBED)
    assert abgerufen == [f"https://playout.3qsdn.com/config/{EMBED}?key=0&timestamp=0"]
    assert info == StreamInfo(
        stream_id="63961",
        hls_url="https://cdn.example/63961/63961_264_live.m3u8",
        embed_url=f"https://playout.3qsdn.com/embed/{EMBED}",
    )
    assert StreamInfo.aus_dict(info.als_dict()) == info


@pytest.mark.parametrize("kennung", ["", "../config", "a b c d e f g h", "x" * 80, "abc/def/ghi"])
def test_3q_ungueltige_kennung_ohne_abruf(monkeypatch: pytest.MonkeyPatch, kennung: str) -> None:
    monkeypatch.setattr(dreiq, "hole_json", lambda *a, **k: pytest.fail("kein Abruf"))
    with pytest.raises(AnbieterError):
        anbieter("3q").aufloesen(kennung)
    assert anbieter("3q").einbettung_url(kennung) is None


def test_3q_konfiguration_ohne_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dreiq, "hole_json", lambda *a, **k: {"sources": {}})
    with pytest.raises(AnbieterError):
        anbieter("3q").aufloesen(EMBED)


def _antwort(online: bool, zustand: str | None, tafel: str = "") -> list[dict[str, Any]]:
    metadaten: dict[str, Any] = {"boardtitleline1": tafel, "posterImage": "https://cdn.example/poster.jpg"}
    if zustand is not None:
        metadaten["playoutState"] = zustand
    return [
        {
            "IsOnline": online,
            "Listeners": "17",
            "Metadata": metadaten,
            "PreviewImage": "https://cdn.example/vorschau.jpg",
            "Snapshot": "https://cdn.example/a.png",
        }
    ]


@pytest.mark.parametrize(
    ("online", "zustand", "tafel", "phase"),
    [
        (True, "pre", "Die Sitzung beginnt um 16:15", Phase.VORHER),
        (True, "live", "", Phase.LIVE),
        (True, "onair", "Die Sitzung wurde um 18:25 beendet", Phase.LIVE),
        (True, "post", "Die Sitzung wurde um 18:25 beendet", Phase.NACHHER),
        (False, "post", "", Phase.NACHHER),
        (True, None, "Die Sitzung wurde um 18:25 beendet", Phase.NACHHER),
        (False, None, "", Phase.UNBEKANNT),
        (False, "", "", Phase.UNBEKANNT),
    ],
)
def test_3q_status_phasen(
    monkeypatch: pytest.MonkeyPatch, online: bool, zustand: str | None, tafel: str, phase: Phase
) -> None:
    """Standbild nach dem Ende (online, Zustand post) ist nicht live; eine alte Tafel überstimmt keinen Zustand."""
    monkeypatch.setattr(dreiq, "hole_json", lambda *a, **k: _antwort(online, zustand, tafel))
    status = anbieter("3q").status(StreamInfo(stream_id="63961"))
    assert status.phase == phase
    assert status.online is online
    assert status.zuschauer == 17


def test_3q_status_ohne_bild_adressen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dreiq, "hole_json", lambda *a, **k: _antwort(True, "live"))
    status = anbieter("3q").status(StreamInfo(stream_id="63961"))
    text = repr(status.roh).lower()
    assert ".jpg" not in text and ".png" not in text and "poster" not in text and "preview" not in text
    assert status.roh["IsOnline"] is True


def test_3q_status_ungueltige_streamnummer() -> None:
    with pytest.raises(AnbieterError):
        anbieter("3q").status(StreamInfo(stream_id="1; drop"))


def test_ohne_bilder_kuerzt_und_filtert() -> None:
    sauber = ohne_bilder(
        {
            "titel": "x" * 900,
            "thumbnail": "https://a.example/t.jpg",
            "logo": "https://a.example/logo.png",
            "liste": [1, 2],
            "zahl": 3,
        }
    )
    assert sauber == {"titel": "x" * 500, "zahl": 3}


def test_hls_status(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = anbieter("hls")
    url = "https://stream.musterstadt.example/live.m3u8"
    info = adapter.aufloesen(url)
    assert info.hls_url == url and adapter.einbettung_url(url) is None

    monkeypatch.setattr(hls, "hole_text", lambda *a, **k: "#EXTM3U\n#EXTINF:6,\nseg1.ts\n")
    assert adapter.status(info).phase == Phase.LIVE
    monkeypatch.setattr(hls, "hole_text", lambda *a, **k: "#EXTM3U\nseg1.ts\n#EXT-X-ENDLIST\n")
    assert adapter.status(info).phase == Phase.NACHHER

    def offline(*_: Any, **__: Any) -> str:
        raise abruf.NichtGefundenError("HTTP 404")

    monkeypatch.setattr(hls, "hole_text", offline)
    status = adapter.status(info)
    assert (status.online, status.phase) == (False, Phase.UNBEKANNT)
    with pytest.raises(AnbieterError):
        adapter.aufloesen("http://unsicher.example/live.m3u8")


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_abruf_nur_https_mit_user_agent_und_grenzen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    gesehen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        if request.url.path == "/gross":
            return httpx.Response(200, content=b"x" * 2048)
        if request.url.path == "/fehlt":
            return httpx.Response(404)
        if request.url.path == "/kaputt":
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    with pytest.raises(AnbieterError, match="https"):
        abruf.hole("http://a.example/x")
    with _client(handler) as client:
        client.headers["User-Agent"] = abruf.USER_AGENT
        assert abruf.hole_json("https://a.example/ok", http=client) == {"ok": True}
        with pytest.raises(AnbieterError, match="zu groß"):
            abruf.hole("https://a.example/gross", max_bytes=1024, http=client)
        with pytest.raises(abruf.NichtGefundenError):
            abruf.hole("https://a.example/fehlt", http=client)
        with pytest.raises(AnbieterError, match="HTTP 500"):
            abruf.hole("https://a.example/kaputt", http=client)
    assert gesehen[0].headers["User-Agent"] == "mandari (+https://mandari.de)"
    assert abruf.client().headers["User-Agent"] == "mandari (+https://mandari.de)"


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example/live.m3u8",
        "https://localhost/live.m3u8",
        "https://cdn.localhost/live.m3u8",
        "https://postgres:5432/",
        "https://127.0.0.1/live.m3u8",
        "https://10.0.0.5/live.m3u8",
        "https://192.168.1.1/live.m3u8",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/live.m3u8",
        "https://[::ffff:127.0.0.1]/live.m3u8",
        "https://2130706433/live.m3u8",
        "https://0x7f000001/live.m3u8",
    ],
)
def test_abruf_nur_oeffentliche_https_ziele(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover – darf nicht aufgerufen werden
        raise AssertionError(f"Abruf von {request.url}")

    with _client(handler) as client, pytest.raises(AnbieterError):
        abruf.hole(url, http=client)


def test_abruf_oeffentliche_ziele_erlaubt() -> None:
    for url in ("https://cdn.example/a.m3u8", "https://93.184.215.14/a", "https://xn--bcher-kva.example/a"):
        assert abruf.ziel_pruefen(url).scheme == "https"


def test_abruf_folgt_weiterleitungen_nur_auf_gepruefte_ziele(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    gesehen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(str(request.url))
        ziele = {
            "/start": "https://cdn.example/relativ",
            "/relativ": "/ziel",
            "/ziel": None,
            "/nach-http": "http://cdn.example/ziel",
            "/nach-intern": "https://10.0.0.5/admin",
            "/nach-dienst": "https://redis:6379/",
            "/kreis": "/kreis",
        }
        ziel = ziele[request.url.path]
        if ziel is None:
            return httpx.Response(200, content=b"#EXTM3U")
        return httpx.Response(302, headers={"Location": ziel})

    with _client(handler) as client:
        assert abruf.hole("https://a.example/start", http=client) == b"#EXTM3U"
        assert gesehen == ["https://a.example/start", "https://cdn.example/relativ", "https://cdn.example/ziel"]
        for start in ("/nach-http", "/nach-intern", "/nach-dienst"):
            gesehen.clear()
            with pytest.raises(AnbieterError):
                abruf.hole(f"https://a.example{start}", http=client)
            assert gesehen == [f"https://a.example{start}"], "das unzulässige Ziel wird nicht abgerufen"
        gesehen.clear()
        with pytest.raises(AnbieterError, match="Weiterleitungen"):
            abruf.hole("https://a.example/kreis", http=client)
        assert len(gesehen) == abruf.MAX_WEITERLEITUNGEN + 1
    assert abruf.client().follow_redirects is False
