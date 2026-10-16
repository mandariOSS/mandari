# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufe bei Diensten außerhalb der Ratsinformationssysteme (Issue #1036, ``insight_core/services/dienste.py``).

Live-Übertragung und Geodienste (Kartenkacheln, Nominatim, Overpass, Photon, Geoportale) melden sich mit derselben
Kennung samt Infoseite und Kontaktadresse wie die Abrufe bei Ratsinformationssystemen und halten einen Takt je Host
ein: Nominatim höchstens eine Anfrage je Sekunde, gemeinsam mit allen Prozessen über denselben Zeitstempel wie die
RIS-Abrufe. Keine fremden Server: Die Abrufe sind gefälscht, die Drossel läuft ohne Redis mit eigener Uhr.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from io import StringIO
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client

from insight_core.models import OParlBody, OParlSource, TileCache
from insight_core.services import dienste, host_pacing, robots

KENNUNG = "mandari-ingestor (+https://mandari.de/crawler/; support@mandari.de)"
PNG = b"\x89PNG\r\n\x1a\nkachel"


class _Uhr:
    """Uhr der Drossel; Schlafen läuft nicht ab, so zeigen die Wartezeiten die Warteschlange je Host."""

    def __init__(self) -> None:
        self.jetzt = 1000.0
        self.geschlafen: list[float] = []

    def __call__(self) -> float:
        return self.jetzt

    def sleep(self, sekunden: float) -> None:
        self.geschlafen.append(round(sekunden, 3))


@pytest.fixture
def takt(monkeypatch: pytest.MonkeyPatch, settings: Any) -> Iterator[_Uhr]:
    """Drossel eingeschaltet wie im Betrieb, ohne Redis (Rückfall im Prozess) und ohne echtes Warten."""
    settings.DIENSTE_DROSSEL = True
    settings.REDIS_URL = ""
    uhr = _Uhr()
    host_pacing.reset()
    cache.delete(dienste._QUELLEN_TAKTE_KEY)
    monkeypatch.setattr(host_pacing, "_now", uhr)
    monkeypatch.setattr("insight_core.services.host_pacing.time.sleep", uhr.sleep)
    yield uhr
    host_pacing.reset()
    cache.delete(dienste._QUELLEN_TAKTE_KEY)


@pytest.fixture
def gewartet(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, float | None]]:
    """Ersetzt ``dienste.warten``: hält (Adresse, Dienst, Höchstwartezeit) fest, ein Zeitpunkt ist immer frei."""
    aufrufe: list[tuple[str, str, float | None]] = []

    def warten(url: Any, dienst: str, *, max_wait: float | None = None) -> bool:
        aufrufe.append((str(url), dienst, max_wait))
        return True

    monkeypatch.setattr(dienste, "warten", warten)
    return aufrufe


def _kein_zeitpunkt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dienste, "warten", lambda *_a, **_kw: False)


# ---------------------------------------------------------------------------
# Kennung und Takt
# ---------------------------------------------------------------------------


def test_kennung_wie_bei_ris_abrufen_mit_infoseite_und_kontakt() -> None:
    assert dienste.USER_AGENT == KENNUNG
    assert dienste.USER_AGENT == robots.USER_AGENT
    assert dienste.headers() == {"User-Agent": KENNUNG}
    assert dienste.headers() is not dienste.headers(), "jeder Aufrufer bekommt ein eigenes Wörterbuch"


def test_nominatim_hoechstens_eine_anfrage_je_sekunde(takt: _Uhr) -> None:
    assert dienste.ABSTAND[dienste.NOMINATIM] >= 1.0
    for _ in range(3):
        assert dienste.warten("https://nominatim.openstreetmap.org/search?q=a", dienste.NOMINATIM)
    assert takt.geschlafen == [1.0, 2.0]
    # Ein anderer Host hat seinen eigenen Takt
    assert dienste.warten("https://photon.komoot.io/api/", dienste.PHOTON)
    assert takt.geschlafen == [1.0, 2.0]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("dienst", "sekunden"),
    [
        (dienste.LIVE, 1.0),
        (dienste.GEOPORTAL, 1.0),
        (dienste.OVERPASS, 1.0),
        (dienste.PHOTON, 1.0),
        (dienste.KACHELN, 0.05),
        (dienste.KACHELN_VORAB, 0.5),
    ],
)
def test_abstand_je_dienst(takt: _Uhr, dienst: str, sekunden: float) -> None:
    for _ in range(2):
        assert dienste.warten("https://dienst.example.org/a", dienst)
    assert takt.geschlafen == [sekunden]


@pytest.mark.django_db
def test_takt_gemeinsam_mit_ris_abrufen_und_jeder_schreibweise(takt: _Uhr, settings: Any) -> None:
    """Liegt ein Geodienst auf dem Server eines RIS, teilen sich beide den Takt (Hostname ohne Port)."""
    settings.RIS_REQUEST_INTERVAL = 1.0
    assert host_pacing.wait("https://geo.stadt.example/oparl/system")
    assert dienste.warten("https://GEO.stadt.example:443/wfs?REQUEST=GetFeature", dienste.GEOPORTAL)
    assert takt.geschlafen == [1.0]


@pytest.mark.django_db
def test_langsamere_quelle_auf_demselben_server_gilt_auch_fuer_dienste(takt: _Uhr) -> None:
    """
    Geoportal und Live-Übertragung auf dem Server einer Quelle mit größerem ``request_interval`` halten deren
    Abstand ein, in beide Richtungen: Auch der nächste RIS-Abruf kommt erst danach. Dienste von OpenStreetMap
    und Photon (nicht in ``NEBEN_RIS``) und andere Server behalten ihren Abstand.
    """
    sync_config = {"request_interval": 30}
    OParlSource.objects.create(name="Langsam", url="https://ris.stadt.example/oparl/system", sync_config=sync_config)

    assert dienste.abstand(dienste.GEOPORTAL, "https://RIS.stadt.example:8443/wfs") == 30.0
    assert dienste.abstand(dienste.LIVE, "https://ris.stadt.example/live.m3u8") == 30.0
    assert dienste.abstand(dienste.PHOTON, "https://ris.stadt.example/api/") == 1.0
    assert dienste.abstand(dienste.GEOPORTAL, "https://geo.anderswo.example/wfs") == 1.0
    assert dienste.abstand(dienste.GEOPORTAL) == 1.0

    assert host_pacing.wait("https://ris.stadt.example/oparl/system", sync_config=sync_config)
    assert dienste.warten("https://ris.stadt.example/wfs", dienste.GEOPORTAL)
    assert host_pacing.wait("https://ris.stadt.example/oparl/body", sync_config=sync_config)
    assert takt.geschlafen == [30.0, 60.0]


@pytest.mark.django_db
def test_abstaende_der_quellen_aus_dem_cache(
    takt: _Uhr, django_assert_num_queries: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    OParlSource.objects.create(
        name="Langsam", url="https://ris.stadt.example/oparl/system", sync_config={"request_interval": 20}
    )
    with django_assert_num_queries(1):
        for _ in range(3):
            assert dienste.abstand(dienste.GEOPORTAL, "https://ris.stadt.example/wfs") == 20.0

    # Gestörter Cache: Die Quellen werden je Aufruf gelesen, der Abstand bleibt
    class _GestoerterCache:
        def get(self, *_a: Any, **_kw: Any) -> Any:
            raise ConnectionError("Redis weg")

        def set(self, *_a: Any, **_kw: Any) -> None:
            raise ConnectionError("Redis weg")

    monkeypatch.setattr(dienste, "cache", _GestoerterCache())
    with django_assert_num_queries(2):
        for _ in range(2):
            assert dienste.abstand(dienste.GEOPORTAL, "https://ris.stadt.example/wfs") == 20.0


def test_hoechstwartezeit_reserviert_nichts(takt: _Uhr) -> None:
    for _ in range(3):
        dienste.warten("https://photon.komoot.io/api/", dienste.PHOTON)
    # Der nächste freie Zeitpunkt liegt 3 s voraus: mit 2 s Höchstwartezeit keine Reservierung
    assert not dienste.warten("https://photon.komoot.io/api/", dienste.PHOTON, max_wait=2.0)
    assert dienste.warten("https://photon.komoot.io/api/", dienste.PHOTON, max_wait=3.0)
    assert takt.geschlafen == [1.0, 2.0, 3.0]


def test_drossel_aus_nur_in_tests(takt: _Uhr, settings: Any) -> None:
    settings.DIENSTE_DROSSEL = False
    for _ in range(3):
        assert dienste.warten("https://nominatim.openstreetmap.org/search", dienste.NOMINATIM)
    assert takt.geschlafen == []


def test_weiterleitung_im_takt_des_ziels(gewartet: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    vor_weiterleitung = dienste.vor_weiterleitung(dienste.GEOPORTAL)
    vor_weiterleitung(httpx.Request("GET", "https://geo.example.org/wfs"))
    assert gewartet == [("https://geo.example.org/wfs", dienste.GEOPORTAL, None)]
    _kein_zeitpunkt(monkeypatch)
    with pytest.raises(dienste.DienstBeschaeftigtError):
        vor_weiterleitung(httpx.Request("GET", "https://geo.example.org/wfs"))


# ---------------------------------------------------------------------------
# Abrufstellen: Kennung und Takt vor jeder Anfrage
# ---------------------------------------------------------------------------


def test_overpass(gewartet: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from insight_core.services import overpass

    gesendet: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        gesendet.append({"url": url, **kwargs})
        return httpx.Response(200, json={"elements": [{"id": 1}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr("insight_core.services.overpass.httpx.post", post)
    assert overpass.run_overpass("[out:json];relation(1);out;", 10) == [{"id": 1}]
    assert gewartet == [(overpass.OVERPASS_ENDPOINTS[0], dienste.OVERPASS, None)]
    assert gesendet[0]["headers"]["User-Agent"] == KENNUNG


def test_photon_und_nominatim_der_georeferenzierung(gewartet: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from insight_core.services import georeferencing

    gesendet: list[tuple[str, str]] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        gesendet.append((url, kwargs["headers"]["User-Agent"]))
        inhalt: Any = {"features": []} if "photon" in url else []
        return httpx.Response(200, json=inhalt, request=httpx.Request("GET", url))

    monkeypatch.setattr("insight_core.services.georeferencing.httpx.get", get)
    monkeypatch.setattr(georeferencing, "_rate_limit", lambda: None)
    ohne_geodaten = dict.fromkeys(("latitude", "longitude", "bbox_south", "bbox_north", "bbox_west", "bbox_east"))
    body = SimpleNamespace(name="Beispielstadt", **ohne_geodaten)
    assert georeferencing.geocode_address("Domplatz 1", body) is None

    photon, nominatim = georeferencing.PHOTON_API_URL, georeferencing.NOMINATIM_SEARCH_URL
    assert gewartet == [
        (photon, dienste.PHOTON, None),
        (photon, dienste.PHOTON, None),
        (nominatim, dienste.NOMINATIM, None),
    ]
    assert gesendet == [(photon, KENNUNG), (photon, KENNUNG), (nominatim, KENNUNG)]


def test_photon_adresssuche_wartet_hoechstens_kurz(gewartet: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from insight_core.services import neighborhood

    gesendet: list[str] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        gesendet.append(kwargs["headers"]["User-Agent"])
        return httpx.Response(200, json={"features": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr("insight_core.services.neighborhood.httpx.get", get)
    assert neighborhood.photon_autocomplete(None, "Domplatz") == []
    assert gesendet == [KENNUNG]
    assert gewartet[0][1:] == (dienste.PHOTON, neighborhood.PHOTON_MAX_WAIT_SECONDS)

    # Kein freier Zeitpunkt: keine Vorschläge, keine Anfrage
    _kein_zeitpunkt(monkeypatch)
    assert neighborhood.photon_autocomplete(None, "Domplatz") == []
    assert gesendet == [KENNUNG]


def test_geoportal_der_bebauungsplaene(gewartet: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from insight_core.services import plan_boundaries

    aufrufe: list[dict[str, Any]] = []

    def download_to(target: io.BytesIO, url: str, **kwargs: Any) -> None:
        aufrufe.append({"url": url, **kwargs})
        # Eine Weiterleitung: Der Abruf reserviert vorher einen Zeitpunkt beim Ziel-Host
        kwargs["before_redirect"](httpx.Request("GET", "https://ziel.geo.example/ogc/items"))
        target.write(b'{"type": "FeatureCollection", "features": []}')

    monkeypatch.setattr(plan_boundaries, "download_to", download_to)
    assert plan_boundaries._get_json("https://geo.example/ogc/items?f=json") == {
        "type": "FeatureCollection",
        "features": [],
    }
    assert aufrufe[0]["user_agent"] == KENNUNG
    assert gewartet == [
        ("https://geo.example/ogc/items?f=json", dienste.GEOPORTAL, None),
        ("https://ziel.geo.example/ogc/items", dienste.GEOPORTAL, None),
    ]


@pytest.mark.django_db
def test_kacheln_mit_kennung_beim_kachelserver(gewartet: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    abrufe: list[tuple[str, str]] = []

    class _Client:
        def __init__(self, *args: Any, headers: dict[str, str] | None = None, **kwargs: Any) -> None:
            self.headers = headers or {}

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            abrufe.append((url, self.headers.get("User-Agent", "")))
            return httpx.Response(200, content=PNG, request=httpx.Request("GET", url))

    monkeypatch.setattr("insight_core.views.maps.httpx.Client", _Client)
    response = Client().get("/insight/tiles/4/3/2")

    assert response.status_code == 200
    assert abrufe == [("https://tile.openstreetmap.org/4/3/2.png", KENNUNG)]
    assert gewartet == [("https://tile.openstreetmap.org/4/3/2.png", dienste.KACHELN, 5.0)]


@pytest.mark.django_db
def test_kachel_ohne_freien_zeitpunkt_fragt_nicht_an(monkeypatch: pytest.MonkeyPatch) -> None:
    _kein_zeitpunkt(monkeypatch)

    def kein_abruf(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("ohne freien Zeitpunkt keine Anfrage bei OSM")

    monkeypatch.setattr("insight_core.views.maps.httpx.Client", kein_abruf)
    response = Client().get("/insight/tiles/4/3/2")
    assert response.status_code == 503
    assert response["Retry-After"] == "5"
    assert TileCache.objects.count() == 0


@pytest.mark.django_db
def test_kacheln_vorab_im_eigenen_takt(
    gewartet: list[Any], monkeypatch: pytest.MonkeyPatch, geo_body: OParlBody
) -> None:
    abrufe: list[tuple[str, str]] = []

    class _Client:
        def __init__(self, *args: Any, headers: dict[str, str] | None = None, **kwargs: Any) -> None:
            self.headers = headers or {}

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            abrufe.append((url, self.headers.get("User-Agent", "")))
            return httpx.Response(200, content=PNG, request=httpx.Request("GET", url))

    monkeypatch.setattr("insight_core.management.commands.prefetch_tiles.httpx.Client", _Client)
    call_command("prefetch_tiles", body_id=str(geo_body.id), zoom="1,1", stdout=StringIO())

    assert abrufe, "mindestens eine Kachel"
    assert all(url.startswith("https://tile.openstreetmap.org/1/") for url, _ in abrufe)
    assert {kennung for _, kennung in abrufe} == {KENNUNG}
    assert [eintrag[1:] for eintrag in gewartet] == [(dienste.KACHELN_VORAB, None)] * len(abrufe)


@pytest.mark.django_db
def test_nominatim_der_geodaten_einer_kommune(
    gewartet: list[Any], monkeypatch: pytest.MonkeyPatch, geo_body: OParlBody
) -> None:
    abrufe: list[tuple[str, str]] = []

    class _Client:
        def __init__(self, *args: Any, headers: dict[str, str] | None = None, **kwargs: Any) -> None:
            self.headers = headers or {}

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            abrufe.append((url, self.headers.get("User-Agent", "")))
            return httpx.Response(200, json=[{"lat": "51.9", "lon": "7.6"}], request=httpx.Request("GET", url))

    monkeypatch.setattr("insight_core.management.commands.fetch_osm_geodata.httpx.Client", _Client)
    call_command("fetch_osm_geodata", body_id=str(geo_body.id), stdout=StringIO())
    geo_body.refresh_from_db()
    assert (float(geo_body.latitude or 0), float(geo_body.longitude or 0)) == (51.9, 7.6)
    assert abrufe == [("https://nominatim.openstreetmap.org/lookup", KENNUNG)]
    assert gewartet == [("https://nominatim.openstreetmap.org/lookup", dienste.NOMINATIM, None)]
