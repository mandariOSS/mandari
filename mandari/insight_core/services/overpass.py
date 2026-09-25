# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Overpass-Abfragen an OpenStreetMap mit Endpoint-Fallback und Retry (Issues #54, #351).

Gemeinsam genutzt von ``import_streets`` (Straßen, Adressen) und ``resolve_body_geodata``
(Grenzen per Gemeindeschlüssel oder Name). Freundlich zu OSM: eigener User-Agent mit
Kontaktadresse, Wartezeit bei 429/504, Wechsel auf den Ersatz-Endpoint erst nach drei
Versuchen. Scheitern alle Endpoints, kommt ``None`` zurück – nie eine leere Trefferliste,
damit Aufrufer „nicht erreichbar“ und „kein Treffer“ unterscheiden können.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# User-Agent mit Kontakt, wie es die Nutzungsregeln von Overpass und Nominatim verlangen
OSM_USER_AGENT = "Mandari/1.0 (https://mandari.de; support@mandari.de)"

# Wartezeit je Versuch bei 429 (zu viele Anfragen) und 504 (Server ausgelastet)
RETRY_WAIT_SECONDS = 15
ATTEMPTS_PER_ENDPOINT = 3


def run_overpass(
    query: str,
    timeout: int,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]] | None:
    """Overpass-Abfrage ausführen; liefert die ``elements`` oder ``None``, wenn kein Endpoint antwortet.

    ``log`` nimmt Warnungen entgegen (etwa ``self.stdout.write`` eines Commands).
    """

    def _log(message: str) -> None:
        if log is not None:
            log(message)

    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(ATTEMPTS_PER_ENDPOINT):
            try:
                response = httpx.post(
                    endpoint,
                    data={"data": query},
                    timeout=float(timeout + 30),
                    headers={"User-Agent": OSM_USER_AGENT},
                )
                if response.status_code == 200:
                    elements = response.json().get("elements", [])
                    return list(elements) if isinstance(elements, list) else []
                if response.status_code in (429, 504):
                    wait = RETRY_WAIT_SECONDS * (attempt + 1)
                    _log(
                        f"  Overpass {response.status_code} — warte {wait}s "
                        f"(Versuch {attempt + 1}/{ATTEMPTS_PER_ENDPOINT})..."
                    )
                    time.sleep(wait)
                    continue
                _log(f"  Overpass HTTP {response.status_code} ({endpoint})")
                break
            except (httpx.HTTPError, ValueError) as e:
                # ValueError: Antwort ohne gültiges JSON (z. B. HTML-Fehlerseite eines Endpoints)
                _log(f"  Overpass-Fehler ({endpoint}): {e}")
                time.sleep(5)
    return None


def overpass_string(value: str) -> str:
    """Wert für einen Overpass-Tagfilter (``["name"="…"]``) maskieren."""
    return value.replace("\\", "\\\\").replace('"', '\\"')
