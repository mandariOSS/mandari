# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufe bei Diensten außerhalb der Ratsinformationssysteme: Kennung und Takt je Host (Issue #1036).

Dazu gehören die Live-Übertragung von Sitzungen (Streaming-Anbieter wie 3Q und HLS-Server,
``hub.live.anbieter.http``) und die Geodienste: Kartenkacheln von OpenStreetMap (``views.maps.tile_proxy``,
``prefetch_tiles``), Nominatim (``fetch_osm_geodata``, ``georeferencing``), Overpass (``overpass``), Photon
(``georeferencing``, ``neighborhood``) und die Geoportale für Umringe von Bebauungsplänen (``plan_boundaries``).

- **Kennung:** dieselbe wie bei Abrufen von Ratsinformationssystemen aus Django (:data:`USER_AGENT`,
  ``mandari-ingestor (+https://mandari.de/ingestor/; support@mandari.de)``, :func:`mandari_oparl.crawler.user_agent`).
  Sie nennt Infoseite und Kontaktadresse, wie es die Nutzungsregeln von Nominatim und der OSM-Kacheln verlangen.
- **Takt je Host:** Vor jeder Anfrage, auch vor jeder Weiterleitung, reserviert der Abruf einen Zeitpunkt für den
  Host, über denselben Zeitstempel in Redis wie die Abrufe bei Ratsinformationssystemen
  (:func:`insight_core.services.host_pacing.wait_seconds`, Hostname ohne Port und Schreibweise). Den Abstand legt
  der Dienst fest (:data:`ABSTAND`). Liegt ein Dienst auf dem Server eines RIS, etwa das Geoportal einer Kommune,
  teilen sich beide den Takt. Für Live-Übertragung und Geoportale (:data:`NEBEN_RIS`) gilt dann auch der größere
  Abstand einer dort registrierten Quelle (``request_interval``), wie bei Quellenanlage und Sondierung: Eine Quelle,
  die wir langsamer abrufen, bekommt auch von diesen Abrufen nicht mehr Anfragen. Ohne Redis drosselt jeder Prozess
  für sich.
- **robots.txt** werten diese Abrufe nicht aus: Sie durchsuchen keine Seiten, sondern fragen Schnittstellen nach
  den Nutzungsregeln des jeweiligen Dienstes ab (Nominatim: höchstens eine Anfrage je Sekunde).

In Web-Anfragen (Kartenkacheln, Adresssuche) wird nur mit Höchstwartezeit gewartet (``max_wait``); ist kein
Zeitpunkt frei, fragt der Abruf nicht an. ``DIENSTE_DROSSEL = False`` schaltet den Takt ab, nur für Tests.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

import httpx
from django.conf import settings
from django.core.cache import cache
from mandari_oparl.crawler import user_agent
from mandari_oparl.pacing import host_intervals, interval_for_host

from . import host_pacing

#: Kennung aller Abrufe bei Diensten, gleich der Kennung der Abrufe bei Ratsinformationssystemen aus Django
USER_AGENT: Final = user_agent()

#: Kachelserver von OpenStreetMap: ein Name, die Namen ``a``/``b``/``c.tile.openstreetmap.org`` sind veraltet
OSM_KACHEL_URL: Final = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

LIVE: Final = "live"
GEOPORTAL: Final = "geoportal"
NOMINATIM: Final = "nominatim"
OVERPASS: Final = "overpass"
PHOTON: Final = "photon"
KACHELN: Final = "kacheln"
KACHELN_VORAB: Final = "kacheln_vorab"

#: Abstand zwischen zwei Anfragen an denselben Host in Sekunden, je Dienst
ABSTAND: Final[Mapping[str, float]] = MappingProxyType(
    {
        # Wie bei Ratsinformationssystemen: eine Anfrage je Sekunde und Server
        LIVE: 1.0,
        GEOPORTAL: 1.0,
        # Nutzungsregeln von Nominatim: höchstens eine Anfrage je Sekunde
        NOMINATIM: 1.0,
        OVERPASS: 1.0,
        PHOTON: 1.0,
        # Kartenkacheln für Besucher: bis zu 20 je Sekunde, so viele wie die Gesamtgrenze des Kachel-Proxys
        # (INSIGHT_TILE_FETCHES_PER_MINUTE, 1200 je Minute)
        KACHELN: 0.05,
        # Vorabladen von Kacheln (prefetch_tiles): höchstens zwei je Sekunde
        KACHELN_VORAB: 0.5,
    }
)

#: Dienste, die auf dem Server eines RIS liegen können (Geoportal einer Kommune, Streaming beim RIS-Anbieter): Hat
#: eine registrierte Quelle auf demselben Hostnamen einen größeren ``request_interval``, gilt dieser auch für sie.
#: Die Dienste von OpenStreetMap und Photon liegen auf eigenen Servern und fragen die Quellen nicht ab.
NEBEN_RIS: Final = frozenset({LIVE, GEOPORTAL})

#: So lange gelten die Abstände registrierter Quellen je Hostname im gemeinsamen Cache (Sekunden): Ein neuer
#: ``request_interval`` wirkt für diese Abrufe spätestens nach fünf Minuten
QUELLEN_TAKTE_SEKUNDEN: Final = 300
_QUELLEN_TAKTE_KEY: Final = "dienste:quellen-takte:v1"


class DienstBeschaeftigtError(httpx.RequestError):
    """Innerhalb der Höchstwartezeit war für den Host kein Zeitpunkt frei; der Abruf hat nicht angefragt."""


def headers() -> dict[str, str]:
    """Kopfzeilen mit unserer Kennung (neues Wörterbuch, der Aufrufer darf es ergänzen)."""
    return {"User-Agent": USER_AGENT}


def quellen_takte() -> Mapping[str, float]:
    """
    Größter ``request_interval`` registrierter Quellen (auch inaktiver) je Hostname
    (:func:`mandari_oparl.pacing.host_intervals`), :data:`QUELLEN_TAKTE_SEKUNDEN` lang im gemeinsamen Cache. Ist
    der Cache gestört, liest jeder Aufruf die Quellen neu, der Abruf bleibt im Takt.
    """
    try:
        takte = cache.get(_QUELLEN_TAKTE_KEY)
    except Exception:  # noqa: BLE001 - Cache gestört (Redis): ohne Cache weiter, wie host_pacing
        takte = None
    if isinstance(takte, dict):
        return takte
    from insight_core.models import OParlSource

    takte = host_intervals(OParlSource.objects.values_list("url", "sync_config"))
    with contextlib.suppress(Exception):
        cache.set(_QUELLEN_TAKTE_KEY, takte, QUELLEN_TAKTE_SEKUNDEN)
    return takte


def abstand(dienst: str, url: str | httpx.URL | None = None) -> float:
    """
    Abstand je Host für ``dienst`` in Sekunden; ``0`` mit ``DIENSTE_DROSSEL = False`` (nur Tests). Für Dienste aus
    :data:`NEBEN_RIS` mit ``url``: der größere Abstand einer registrierten Quelle auf demselben Hostnamen.
    """
    if not getattr(settings, "DIENSTE_DROSSEL", True):
        return 0.0
    eigener = ABSTAND[dienst]
    if url is None or dienst not in NEBEN_RIS:
        return eigener
    return interval_for_host(str(url), quellen_takte(), eigener)


def warten(url: str | httpx.URL, dienst: str, *, max_wait: float | None = None) -> bool:
    """
    Bis zum Zeitpunkt für den Host von ``url`` warten (Abstand des Dienstes, siehe :func:`abstand`). ``max_wait``:
    höchstens so lange, sonst nichts reservieren und ``False`` (Web-Anfragen).
    """
    return host_pacing.wait_seconds(str(url), abstand(dienst, url), max_wait=max_wait)


def vor_weiterleitung(dienst: str) -> Callable[[httpx.Request], None]:
    """
    ``before_redirect`` für :func:`insight_core.services.safe_fetch.download_to`: vor jeder Weiterleitung einen
    Zeitpunkt beim Ziel-Host reservieren (ohne robots.txt, siehe Moduldokumentation).
    """

    def before_redirect(request: httpx.Request) -> None:
        if not warten(request.url, dienst):
            raise DienstBeschaeftigtError("Takt je Host: kein freier Zeitpunkt", request=request)

    return before_redirect
