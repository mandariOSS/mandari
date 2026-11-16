# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nachbarschafts-Autocomplete aus dem eigenen Straßen- und Adressverzeichnis (Issue #54).

Sucht Straßen (``Street``) und Hausnummern-Punkte (``Address``) der aktiven Kommune über
den normalisierten Namen: Präfix-Treffer zuerst, dann Teilstring-Treffer, höchstens
``AUTOCOMPLETE_LIMIT`` Ergebnisse. Photon dient nur noch als Fallback, wenn für die
Kommune kein Straßenverzeichnis importiert ist; dann mit Kennung und Takt je Host wie alle
Abrufe bei Diensten (``dienste``, Issue #1036). Ist binnen ``PHOTON_MAX_WAIT_SECONDS`` kein
Zeitpunkt frei, gibt es keine Vorschläge statt einer Anfrage.

Der Takt bei Photon gilt für die ganze Plattform. Damit ihn nicht ein einzelner Aufrufer belegt, zählt die
Adresssuche vor jedem Abruf je Aufrufer und insgesamt (``INSIGHT_PHOTON_PER_IP_MINUTE``,
``INSIGHT_PHOTON_PER_MINUTE``); die Gesamtgrenze liegt unter dem Takt, damit die Verortung Zeitpunkte behält.
Gleiche Eingaben kommen aus dem Cache, ohne zu zählen und ohne einen Zeitpunkt zu belegen.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

import httpx
from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg, Min, Q

from insight_core import throttle
from insight_core.services import dienste
from insight_core.services.gazetteer import normalize_house_number, normalize_street_name, strip_house_number

if TYPE_CHECKING:
    from insight_core.models import OParlBody

logger = logging.getLogger(__name__)

AUTOCOMPLETE_LIMIT = 10
MIN_QUERY_LEN = 2
PHOTON_LIMIT = 5
#: Die Adresssuche wartet im Seitenaufruf höchstens so lange auf einen Zeitpunkt bei Photon
PHOTON_MAX_WAIT_SECONDS = 2.0
#: Längere Eingaben werden gekürzt: Eine Adresse braucht nicht mehr
MAX_QUERY_LEN = 100
#: So lange gelten Vorschläge von Photon für dieselbe Eingabe
PHOTON_CACHE_SECONDS = 24 * 60 * 60


def autocomplete_places(
    body: OParlBody, query: str, limit: int = AUTOCOMPLETE_LIMIT, *, client_key: str | None = None
) -> list[dict[str, Any]]:
    """Vorschläge ``{name, lat, lon}`` für die Adresssuche der Nachbarschaftsseite."""
    from insight_core.models import Street

    query = (query or "").strip()
    if len(query) < MIN_QUERY_LEN:
        return []

    if not Street.objects.filter(body=body).exists():
        return photon_autocomplete(body, query, client_key=client_key)

    base, house_number = strip_house_number(query)
    normalized = normalize_street_name(base)
    if not normalized:
        return []

    if house_number:
        addresses = _address_rows(body, normalized, normalize_house_number(house_number), limit)
        if addresses:
            return addresses

    prefix = _street_rows(body, Q(normalized_name__startswith=normalized), limit)
    if len(prefix) >= limit:
        return prefix
    contains = _street_rows(
        body,
        Q(normalized_name__contains=normalized) & ~Q(normalized_name__startswith=normalized),
        limit - len(prefix),
    )
    return prefix + contains


def _address_rows(
    body: OParlBody, normalized_street: str, normalized_house_number: str, limit: int
) -> list[dict[str, Any]]:
    from insight_core.models import Address

    rows = Address.objects.filter(
        body=body,
        normalized_street__startswith=normalized_street,
        normalized_house_number__startswith=normalized_house_number,
    ).order_by("normalized_street", "normalized_house_number")[:limit]
    return [
        {"name": f"{row.street} {row.house_number}", "lat": float(row.latitude), "lon": float(row.longitude)}
        for row in rows
    ]


def _street_rows(body: OParlBody, condition: Q, limit: int) -> list[dict[str, Any]]:
    """Eine Zeile je Straßenname; Zentroid gemittelt über alle OSM-Ways der Straße."""
    from insight_core.models import Street

    if limit <= 0:
        return []
    rows = (
        Street.objects.filter(body=body)
        .filter(condition)
        .values("normalized_name")
        .annotate(name=Min("name"), lat=Avg("latitude"), lon=Avg("longitude"))
        .order_by("normalized_name")[:limit]
    )
    return [{"name": row["name"], "lat": float(row["lat"]), "lon": float(row["lon"])} for row in rows]


def photon_autocomplete(body: OParlBody | None, query: str, *, client_key: str | None = None) -> list[dict[str, Any]]:
    """
    Fallback ohne Straßenverzeichnis: Photon-Suche mit Bias auf das Zentrum der Kommune.

    ``client_key`` ist die Adresse des Aufrufers (``throttle.client_ip``). Über seiner Grenze oder der Gesamtgrenze
    gibt es keine Vorschläge und keinen Abruf; Treffer aus dem Cache sind frei.
    """
    query = " ".join(query[:MAX_QUERY_LEN].split())
    kennung = f"{getattr(body, 'pk', '')}|{query.lower()}"
    cache_key = "insight:photon:" + hashlib.sha256(kennung.encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return list(cached)
    # Vor dem Takt zählen: Wer über seiner Grenze liegt, reserviert keinen Zeitpunkt und zählt nicht ins Gesamte
    if throttle.hit(
        "photon-ip",
        client_key or "unbekannt",
        limit=throttle.setting("INSIGHT_PHOTON_PER_IP_MINUTE"),
        window=throttle.MINUTE,
    ) or throttle.hit(
        "photon-all", "alle", limit=throttle.setting("INSIGHT_PHOTON_PER_MINUTE"), window=throttle.MINUTE
    ):
        return []

    params: dict[str, str] = {"q": query, "limit": str(PHOTON_LIMIT), "lang": "de"}
    if body and body.latitude and body.longitude:
        params["lat"] = str(body.latitude)
        params["lon"] = str(body.longitude)

    photon_url = getattr(settings, "PHOTON_API_URL", "https://photon.komoot.io/api/")
    # Warten und Anfrage ohne ausgeliehene Datenbankverbindung (Pool, Issue #1024); danach braucht es keine mehr
    from apps.common.db_connections import release_idle_thread_connections

    release_idle_thread_connections()
    if not dienste.warten(photon_url, dienste.PHOTON, max_wait=PHOTON_MAX_WAIT_SECONDS):
        logger.info("Photon-Autocomplete: kein freier Zeitpunkt, keine Anfrage")
        return []
    try:
        response = httpx.get(photon_url, params=params, timeout=5.0, headers=dienste.headers())
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning("Photon-Autocomplete fehlgeschlagen: %s", exc)
        return []

    results: list[dict[str, Any]] = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates", [])
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        if body and not _inside_bbox(body, lat, lon):
            continue
        results.append({"name": _photon_label(props, query), "lat": lat, "lon": lon})
    # Nur beantwortete Abrufe: Ein Fehlschlag oder ein fehlender Zeitpunkt soll sich nicht festschreiben
    cache.set(cache_key, results, PHOTON_CACHE_SECONDS)
    return results


def _inside_bbox(body: OParlBody, lat: float, lon: float) -> bool:
    if not (body.bbox_north and body.bbox_south and body.bbox_east and body.bbox_west):
        return True
    return float(body.bbox_south) <= lat <= float(body.bbox_north) and float(body.bbox_west) <= lon <= float(
        body.bbox_east
    )


def _photon_label(props: dict[str, Any], query: str) -> str:
    parts: list[str] = []
    if props.get("name"):
        parts.append(str(props["name"]))
    if props.get("street"):
        parts.append(str(props["street"]))
    if props.get("housenumber"):
        if parts:
            parts[-1] = f"{parts[-1]} {props['housenumber']}"
        else:
            parts.append(str(props["housenumber"]))
    if props.get("city"):
        parts.append(str(props["city"]))
    return ", ".join(parts) if parts else str(props.get("name") or query)
