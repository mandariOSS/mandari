# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OParl-1.0-Kompatibilität (Issue #122).

more! rubin liefert auf gremien.info standardmäßig OParl 1.0 mit einigen
Eigenheiten, die wir hier zentral kapseln, damit Client und Orchestrator
nur kleine, gut testbare Hooks brauchen:

- Namespace ``https://schema.oparl.org/1.0/`` (Typ-URLs und ``oparlVersion``)
- Fehlerobjekte mit HTTP 200 (``{"type": ".../1.0/Error", "message":
  "Requested class doesn't exist."}``) statt HTTP 404 bei 1.1-Pfaden
- ``modified_since`` wird stillschweigend ignoriert (die Listen-Links
  verlieren den Parameter, ``totalElements`` bleibt unverändert)
- ``created``/``modified`` sind synthetisch: alle Objekte tragen den
  Abrufdatum-Mitternachtsstempel; Organisationen haben gar keine Stempel.
  Eine Änderungserkennung über Zeitstempel ist damit unmöglich, wir
  vergleichen stattdessen den Inhalt (Content-Hash wie bei Scraper-Quellen).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

OPARL_NAMESPACE_1_0 = "https://schema.oparl.org/1.0/"
OPARL_NAMESPACE_1_1 = "https://schema.oparl.org/1.1/"
OPARL_NAMESPACES: tuple[str, ...] = (OPARL_NAMESPACE_1_0, OPARL_NAMESPACE_1_1)

# Versions-Segmente in Pfaden, z. B. /oparl/v1.1/system, /oparl/1.0/system.asp
_VERSION_SEGMENT = re.compile(r"/v?1\.[01](?=/|$)")
_NAMESPACE_VERSION = re.compile(r"^https?://schema\.oparl\.org/(1\.[01])/")


def detect_oparl_version(data: Any) -> str | None:
    """
    Liest die OParl-Version ("1.0"/"1.1") aus einem Objekt.

    Reihenfolge: ``oparlVersion`` (nur System), sonst Namespace der
    ``type``-URL. Listen mit ``data[]`` werden über das erste Element
    ausgewertet. None, wenn nichts erkennbar ist.
    """
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    for key in ("oparlVersion", "type"):
        value = data.get(key)
        if isinstance(value, str):
            match = _NAMESPACE_VERSION.match(value)
            if match:
                return match.group(1)
    items = data.get("data")
    if isinstance(items, list) and items:
        return detect_oparl_version(items[0])
    return None


def is_oparl_error(data: Any) -> bool:
    """
    Erkennt OParl-Fehlerobjekte, die mit HTTP 200 ausgeliefert werden.

    more! rubin antwortet auf unbekannte Pfade (z. B. ``/oparl/v1.1/system``)
    mit ``{"type": "https://schema.oparl.org/1.0/Error", "message":
    "Requested class doesn't exist.", "debug": "Error-Code 101"}``.
    """
    if not isinstance(data, dict):
        return False
    type_url = data.get("type")
    if isinstance(type_url, str) and type_url.rstrip("/").endswith("/Error"):
        return True
    # Manche Server liefern nur {"error": "..."} (z. B. deaktivierter Webservice).
    return "error" in data and "id" not in data and "type" not in data


def oparl_error_message(data: Any) -> str:
    """Lesbare Fehlermeldung eines OParl-Fehlerobjekts."""
    if not isinstance(data, dict):
        return "unbekannter Fehler"
    for key in ("message", "error", "debug"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return "unbekannter Fehler"


def oparl_10_fallback_urls(url: str) -> list[str]:
    """
    Kandidaten-URLs für den Rückfall auf OParl 1.0, wenn ein 1.1-Pfad
    scheitert.

    Aus ``https://x.gremien.info/oparl/v1.1/system`` werden nacheinander
    ``.../oparl/system`` (Versionssegment entfernt) und ``.../oparl``
    (Wurzel, bei more! rubin ebenfalls das System-Objekt). Die Ausgangs-URL
    ist nie enthalten; Reihenfolge = Probierreihenfolge.
    """
    parsed = urlparse(url)
    path = parsed.path or "/"
    candidates: list[str] = []

    stripped = _VERSION_SEGMENT.sub("", path, count=1)
    if stripped != path:
        candidates.append(parsed._replace(path=stripped or "/").geturl())
        # Wurzel des API-Pfads (z. B. /oparl) ohne das Objekt-Segment
        root = stripped.rsplit("/", 1)[0] if "/" in stripped.strip("/") else ""
        if root and root != "/":
            candidates.append(parsed._replace(path=root, query="").geturl())
    elif path.rstrip("/").endswith("/system"):
        # Bereits versionslos: als letzte Chance die Wurzel probieren
        root = path.rstrip("/").rsplit("/", 1)[0]
        if root:
            candidates.append(parsed._replace(path=root, query="").geturl())

    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        if candidate != url and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def modified_since_dropped(links: Any) -> bool:
    """
    True, wenn der Server den ``modified_since``-Filter aus seinen
    Paginierungs-Links entfernt hat — dann hat er ihn ignoriert (more!
    rubin) oder die Folgeseiten wären ohnehin ungefiltert. Ohne ``self``/
    ``next``-Link ist keine Aussage möglich (False).
    """
    if not isinstance(links, dict):
        return False
    checked = False
    for key in ("next", "self"):
        link = links.get(key)
        if not isinstance(link, str) or not link:
            continue
        checked = True
        if "modified_since" in parse_qs(urlparse(link).query):
            return False
    return checked


def _is_midnight_stamp(value: Any) -> bool:
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0 and parsed.microsecond == 0


def timestamps_unreliable(item: dict[str, Any]) -> bool:
    """
    Erkennt Objekte, deren ``modified`` keine Änderungsinformation trägt.

    Zwei Muster aus OParl 1.0 (more! rubin): ``modified`` fehlt ganz
    (Organisationen) oder ``created`` und ``modified`` sind identisch und
    liegen exakt auf Mitternacht — der Server stempelt jedes Objekt mit dem
    Abrufdatum. Für solche Objekte vergleicht der Sync den Inhalt statt
    der Zeitstempel.
    """
    modified = item.get("modified")
    if not modified:
        return True
    created = item.get("created")
    return created == modified and _is_midnight_stamp(modified)
