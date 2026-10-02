# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufoptionen je OParl-Quelle aus ``OParlSource.sync_config`` (additiv, keine Migration).

Die Standardwerte des Ingestors passen für die meisten Ratsinformationssysteme. Einzelne Quellen brauchen
mehr Schonung oder haben Eigenheiten, die sich nicht automatisch erkennen lassen:

``request_interval``
    Mindestabstand in Sekunden vor jeder Anfrage an diese Quelle (statt ``OPARL_WAIT_TIME``).
``list_params``
    Zusätzliche Parameter für die erste Seite jeder Liste, z. B. ``{"size": 100}`` bei ALLRIS: weniger,
    dafür größere Seiten. Die Folgeseiten kommen aus ``links.next`` der Quelle.
``carry_modified_since``
    Die Quelle filtert mit ``modified_since``, lässt den Parameter aber in ``links.next`` weg (ALLRIS). Der
    Client hängt ihn dann an jede Folgeseite an, statt die Quelle als „ohne Filter“ zu behandeln. Ohne den
    Schalter liefe der inkrementelle Abgleich über die ungefilterte, aufsteigend sortierte Liste und bräche
    nach fünf unveränderten Seiten ab, bevor er neue Einträge erreicht.
``file_downloads``
    ``false``: Dateien dieser Quelle nicht automatisch abrufen (Textextraktion im Ingestor, Dateicache und
    Vorschau in Django). Für Quellen, die Dokumente nur hinter einer Zugangsprüfung für Menschen ausliefern;
    die Dateien bleiben in der Warteschlange und werden nachgeholt, sobald der Schalter fällt.

Ungültige Werte gelten als nicht gesetzt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

REQUEST_INTERVAL_KEY = "request_interval"
LIST_PARAMS_KEY = "list_params"
CARRY_MODIFIED_SINCE_KEY = "carry_modified_since"
FILE_DOWNLOADS_KEY = "file_downloads"

#: Obergrenze für den Abstand zwischen zwei Anfragen (Tippfehler wie 600 statt 0.6 bremsen sonst alles aus)
MAX_REQUEST_INTERVAL = 30.0

_PARAM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,39}$")


def _request_interval(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 0 or value > MAX_REQUEST_INTERVAL:
        return None
    return float(value)


def _list_params(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    params: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not _PARAM_NAME.match(key) or key == "modified_since":
            continue
        if isinstance(raw, bool) or not isinstance(raw, str | int):
            continue
        text = str(raw).strip()
        if text and len(text) <= 100:
            params[key] = text
    return params


@dataclass(frozen=True)
class SourceFetchOptions:
    """Abrufoptionen einer Quelle (siehe Moduldokumentation)."""

    request_interval: float | None = None
    list_params: dict[str, str] = field(default_factory=dict)
    carry_modified_since: bool = False
    file_downloads: bool = True

    @classmethod
    def from_sync_config(cls, sync_config: Any) -> SourceFetchOptions:
        if not isinstance(sync_config, Mapping):
            return cls()
        return cls(
            request_interval=_request_interval(sync_config.get(REQUEST_INTERVAL_KEY)),
            list_params=_list_params(sync_config.get(LIST_PARAMS_KEY)),
            carry_modified_since=sync_config.get(CARRY_MODIFIED_SINCE_KEY) is True,
            # Nur ein ausdrückliches false schaltet ab
            file_downloads=sync_config.get(FILE_DOWNLOADS_KEY) is not False,
        )

    def client_kwargs(self) -> dict[str, Any]:
        """Argumente für ``OParlClient`` (nur gesetzte Werte, sonst gelten die Standards)."""
        kwargs: dict[str, Any] = {
            "list_params": dict(self.list_params),
            "carry_modified_since": self.carry_modified_since,
        }
        if self.request_interval is not None:
            kwargs["wait_time"] = self.request_interval
        return kwargs
