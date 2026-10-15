# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufoptionen je OParl-Quelle aus ``OParlSource.sync_config`` (additiv, keine Migration).

Die Standardwerte des Ingestors passen für die meisten Ratsinformationssysteme. Einzelne Quellen brauchen
mehr Schonung oder haben Eigenheiten, die sich nicht automatisch erkennen lassen:

``request_interval``
    Mindestabstand in Sekunden zwischen dem Beginn zweier Anfragen an den Host dieser Quelle. Ohne Wert gilt
    der Standard des Ingestors (``INGESTOR_REQUEST_INTERVAL``, eine Anfrage je Sekunde). Der Abstand gilt über
    alle Abrufplätze, Quellen auf demselben Host und Prozesse hinweg (Daemon, Einzelabgleich, Django), weil
    alle denselben Zeitstempel in Redis nutzen (``src/client/host_pacing.py``). ``0`` schaltet die Drossel
    für die Quelle ab.
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
``full_sync_window``
    Zeitfenster für Vollabgleiche, z. B. ``{"start": "00:00", "end": "06:00"}`` (Ortszeit, Standard
    ``Europe/Berlin``, abweichend über ``"tz"``; über Mitternacht erlaubt). Ein Vollabgleich außerhalb des
    Fensters, ob geplant, aus dem Admin oder per ``sync --full``, läuft als inkrementeller Abgleich. Ein
    laufender Vollabgleich holt nach dem Ende des Fensters keine weitere Seite mehr; die offenen Listen gelten
    als unvollständig und kommen beim nächsten Vollabgleich wieder dran.
``robots_override``
    Ausnahme von der robots.txt, nur mit Freigabe der Stelle und Pflicht-Vermerk, z. B.
    ``{"scope": "files", "note": "Freigabe per E-Mail vom …, offizielle Anfrage läuft"}``. ``scope``: ``api``,
    ``files`` oder ``all``. Ohne Vermerk gilt die robots.txt (``mandari_oparl.robots.robots_override``).

Ungültige Werte gelten als nicht gesetzt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mandari_oparl.pacing import MAX_REQUEST_INTERVAL as MAX_REQUEST_INTERVAL  # Bestandsname
from mandari_oparl.pacing import REQUEST_INTERVAL_KEY
from mandari_oparl.pacing import request_interval as _request_interval
from mandari_oparl.robots import RobotsOverride, robots_override

LIST_PARAMS_KEY = "list_params"
CARRY_MODIFIED_SINCE_KEY = "carry_modified_since"
FILE_DOWNLOADS_KEY = "file_downloads"
FULL_SYNC_WINDOW_KEY = "full_sync_window"
DEFAULT_WINDOW_TZ = "Europe/Berlin"


_PARAM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,39}$")


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


_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _clock(value: Any) -> time | None:
    match = _CLOCK.match(value.strip()) if isinstance(value, str) else None
    return time(int(match.group(1)), int(match.group(2))) if match else None


@dataclass(frozen=True)
class FullSyncWindow:
    """Zeitfenster für Vollabgleiche einer Quelle (``sync_config["full_sync_window"]``)."""

    start: time
    end: time
    tz: ZoneInfo

    @classmethod
    def parse(cls, value: Any) -> FullSyncWindow | None:
        if not isinstance(value, Mapping):
            return None
        start, end = _clock(value.get("start")), _clock(value.get("end"))
        if start is None or end is None or start == end:
            return None
        try:
            tz = ZoneInfo(str(value.get("tz") or DEFAULT_WINDOW_TZ))
        except (ZoneInfoNotFoundError, ValueError):
            return None
        return cls(start=start, end=end, tz=tz)

    def closes_at(self, now: datetime | None = None) -> datetime | None:
        """Ende des Fensters (UTC), wenn ``now`` darin liegt, sonst ``None``."""
        now = (now or datetime.now(UTC)).astimezone(self.tz)
        clock = now.time().replace(tzinfo=None)
        if self.start < self.end:
            inside = self.start <= clock < self.end
            end_day = now.date()
        else:  # über Mitternacht
            inside = clock >= self.start or clock < self.end
            end_day = now.date() + timedelta(days=1) if clock >= self.start else now.date()
        if not inside:
            return None
        return datetime.combine(end_day, self.end, tzinfo=self.tz).astimezone(UTC)


@dataclass(frozen=True)
class SourceFetchOptions:
    """Abrufoptionen einer Quelle (siehe Moduldokumentation)."""

    request_interval: float | None = None
    list_params: dict[str, str] = field(default_factory=dict)
    carry_modified_since: bool = False
    file_downloads: bool = True
    robots_override: RobotsOverride | None = None
    full_sync_window: FullSyncWindow | None = None

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
            robots_override=robots_override(sync_config),
            full_sync_window=FullSyncWindow.parse(sync_config.get(FULL_SYNC_WINDOW_KEY)),
        )

    def full_sync_plan(self, full: bool, now: datetime | None = None) -> tuple[bool, datetime | None]:
        """
        Vollabgleich erlaubt? Liefert ``(full, Abrufende)``: außerhalb des Fensters ``(False, None)``, im
        Fenster ``(True, Fensterende)``. Ohne Fenster oder für inkrementelle Läufe bleibt alles, wie es ist.
        """
        if not full or self.full_sync_window is None:
            return full, None
        closes_at = self.full_sync_window.closes_at(now)
        return (closes_at is not None), closes_at

    def client_kwargs(self) -> dict[str, Any]:
        """Argumente für ``OParlClient`` (nur gesetzte Werte, sonst gelten die Standards)."""
        kwargs: dict[str, Any] = {
            "list_params": dict(self.list_params),
            "carry_modified_since": self.carry_modified_since,
            "robots_override": self.robots_override,
        }
        if self.request_interval is not None:
            kwargs["request_interval"] = self.request_interval
        return kwargs
