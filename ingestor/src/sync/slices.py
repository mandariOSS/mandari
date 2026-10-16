# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vollabgleich in Zeitscheiben und langsame Listen (Issue #1033).

Manche Ratsinformationssysteme liefern ihre vollständigen Listen nicht aus: Bei der Stadt Hamm (SD.NET RIM) brechen
die ungefilterten Listen ``paper`` und ``meeting`` nach 60 Sekunden mit HTTP 504 ab, mit Zeitfilter antworten sie in
3 bis 25 Sekunden. Für solche Quellen gibt es zwei Werkzeuge:

**Vollabgleich in Zeitscheiben** (``sync_config["full_sync_slices"]``, ``FullSyncSlices``): Der Vollabgleich fragt
jede Liste nur mit ``modified_since``/``modified_until`` an (``full_windows``): zuerst alles bis ``since``, dann
Scheiben zu ``days`` Tagen von alt nach neu, zuletzt alles ab der letzten Grenze ohne Obergrenze. Nach jeder ganz
gelesenen Scheibe steht der Beginn der nächsten in ``sync_config["full_sync_progress"]``; ein abgebrochener Lauf
(Zeitfenster, Fehler) macht dort weiter. Hat jede Liste der Kommune in einem Lauf ihre letzte Scheibe gelesen und
fehlt nichts, ist der Durchgang fertig, und der Fortschritt dieser Listen wird gelöscht: Der nächste Vollabgleich
beginnt wieder bei ``since``. Liefert die Quelle Objekte außerhalb der Scheibe (mehr als ``SLICE_TOLERANCE``), beachtet
sie den Filter nicht: Der Lauf bricht ab, statt dieselben Objekte in jeder Scheibe erneut zu holen, und der Befund
steht in ``sync_config["full_sync_filter_ignored"]``. Objekte ohne ``modified`` erfasst ein Abgleich in Zeitscheiben
nicht; geliefert gezählt werden sie in ``BodySlices.without_modified``.

**Langsame optionale Listen** (``sync_config["slow_lists"]``, ``SlowList``): Bricht eine optionale Liste nach einer
Zeitüberschreitung oder HTTP 504 ab, setzt der Ingestor sie aus, zuerst eine Stunde, nach jeder weiteren doppelt so
lange, höchstens 24 Stunden (``slow_backoff``); kam sie in einem Versuch voran, beginnt die Reihe wieder bei einer
Stunde. Die Liste behält ihren eigenen Fensteranfang (``since``): Nach der Aussetzung holt der inkrementelle Lauf sie
in Scheiben zu einem Tag nach (``slow_windows``), ohne die Änderungen der Zwischenzeit zu verlieren. Eine ausgesetzte
optionale Liste ist deshalb keine Lücke des Laufs: Fensteranfang der Kommune und Aktualität der Quelle rücken weiter
vor. Liest ein Lauf die Liste ganz, wird der Eintrag gelöscht.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mandari_oparl.utils import parse_datetime

from src.client.source_options import FullSyncSlices

#: Abweichung, die ein Objekt von den Grenzen seiner Scheibe haben darf, bevor der Filter als ignoriert gilt: fängt
#: falsch angegebene Zeitzonen (höchstens 14 Stunden) und Tagesstempel ab
SLICE_TOLERANCE = timedelta(days=1)
#: Aussetzung einer langsamen optionalen Liste: erste Dauer und Obergrenze; dazwischen verdoppelt sie sich
SLOW_LIST_BASE = timedelta(hours=1)
SLOW_LIST_MAX = timedelta(hours=24)
#: Scheibenlänge, in der der inkrementelle Lauf eine langsame Liste nachholt
SLOW_LIST_SLICE = timedelta(days=1)
#: So lange gilt der Befund „Server ignoriert den Filter“; danach versucht der Vollabgleich die Zeitscheiben erneut
FILTER_IGNORED_RECHECK = timedelta(days=30)
#: Ortszeit der Quellen für den Tagesbeginn langsamer Listen ohne nachgewiesene Uhrzeit
_SOURCE_TZ = ZoneInfo("Europe/Berlin")


def _iso(stamp: datetime | None) -> str | None:
    return stamp.astimezone(UTC).isoformat(timespec="seconds") if stamp is not None else None


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def modified_of(item: Any) -> datetime | None:
    """``modified`` eines Objekts als UTC-Zeitpunkt (ohne Zone als UTC), sonst ``None``."""
    value = item.get("modified") if isinstance(item, dict) else None
    stamp = parse_datetime(value) if isinstance(value, str) else None
    if stamp is None:
        return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


@dataclass(frozen=True)
class SliceWindow:
    """Eine Zeitscheibe: ``modified_since`` bis ``modified_until``; ``None`` heißt ohne diese Grenze."""

    since: datetime | None
    until: datetime | None

    def label(self) -> str:
        if self.since is None:
            return f"bis {_iso(self.until)}"
        if self.until is None:
            return f"ab {_iso(self.since)}"
        return f"{_iso(self.since)} bis {_iso(self.until)}"

    def outside(self, item: Any) -> datetime | None:
        """``modified`` eines Objekts, das mehr als ``SLICE_TOLERANCE`` außerhalb der Scheibe liegt, sonst ``None``."""
        stamp = modified_of(item)
        if stamp is None:
            return None
        if self.since is not None and stamp < self.since - SLICE_TOLERANCE:
            return stamp
        if self.until is not None and stamp > self.until + SLICE_TOLERANCE:
            return stamp
        return None


def full_windows(config: FullSyncSlices, progress: datetime | None, now: datetime) -> list[SliceWindow]:
    """
    Scheiben einer Liste im Vollabgleich, von alt nach neu.

    Ohne Fortschritt (oder mit einem vor ``since`` oder in der Zukunft): zuerst alles bis ``since``, dann je
    ``days`` Tage ab ``since``. Mit Fortschritt: je ``days`` Tage ab dort. Zuletzt alles ab der letzten Grenze, ohne
    Obergrenze; jede Scheibe davor endet spätestens jetzt.
    """
    windows: list[SliceWindow] = []
    if progress is None or progress < config.since or progress > now:
        windows.append(SliceWindow(None, config.since))
        start = config.since
    else:
        start = progress
    step = timedelta(days=config.days)
    while start + step <= now:
        windows.append(SliceWindow(start, start + step))
        start += step
    windows.append(SliceWindow(start, None))
    return windows


def slow_windows(start: datetime, now: datetime) -> list[SliceWindow]:
    """Scheiben zu einem Tag (``SLOW_LIST_SLICE``) ab ``start``, die letzte ohne Obergrenze."""
    windows: list[SliceWindow] = []
    while start + SLOW_LIST_SLICE <= now:
        windows.append(SliceWindow(start, start + SLOW_LIST_SLICE))
        start += SLOW_LIST_SLICE
    windows.append(SliceWindow(start, None))
    return windows


def day_floor(stamp: datetime) -> datetime:
    """Tagesbeginn: der frühere von 00:00 Uhr UTC und 00:00 Uhr Ortszeit (``Europe/Berlin``), als UTC-Zeitpunkt."""
    utc = stamp.astimezone(UTC)
    utc_midnight = utc.replace(hour=0, minute=0, second=0, microsecond=0)
    local_midnight = utc.astimezone(_SOURCE_TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    return min(utc_midnight, local_midnight)


def slow_backoff(failures: int) -> timedelta:
    """Aussetzung nach ``failures`` Zeitüberschreitungen in Folge: 1, 2, 4, 8, 16, dann 24 Stunden."""
    steps = max(0, min(failures - 1, 10))
    return min(SLOW_LIST_BASE * (1 << steps), SLOW_LIST_MAX)


@dataclass
class SlowList:
    """Langsame optionale Liste (``sync_config["slow_lists"]``)."""

    #: Zeitüberschreitungen in Folge
    failures: int
    #: ausgesetzt bis (UTC)
    until: datetime
    #: Fensteranfang der Liste: Änderungen ab hier hat noch kein Lauf ganz gelesen (UTC)
    since: datetime
    reason: str = ""

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> SlowList | None:
        failures = entry.get("failures")
        until, since = _utc(entry.get("until")), _utc(entry.get("since"))
        if isinstance(failures, bool) or not isinstance(failures, int) or failures < 1:
            return None
        if until is None or since is None:
            return None
        reason = entry.get("reason")
        return cls(failures=failures, until=until, since=since, reason=reason if isinstance(reason, str) else "")

    def to_entry(self) -> dict[str, Any]:
        return {
            "failures": self.failures,
            "until": _iso(self.until),
            "since": _iso(self.since),
            "reason": self.reason,
        }

    def suspended(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) < self.until


@dataclass
class SliceRun:
    """Zeitscheiben und langsame Listen einer Quelle in einem Lauf (geteilt von allen Kommunen und Listen)."""

    source_url: str
    #: Vollabgleich in Zeitscheiben; nur gesetzt, wenn dieser Lauf ein Vollabgleich ist
    config: FullSyncSlices | None = None
    #: Beginn der ersten nicht ganz gelesenen Scheibe je Liste (``sync_config["full_sync_progress"]``)
    progress: dict[str, datetime] = field(default_factory=dict)
    #: Langsame optionale Listen (``sync_config["slow_lists"]``)
    slow: dict[str, SlowList] = field(default_factory=dict)
    #: Meldung, sobald die Quelle den Filter einer Scheibe ignoriert: Jede Liste bricht vor der nächsten Scheibe ab
    filter_ignored: str | None = None

    @classmethod
    def build(
        cls,
        source_url: str,
        config: FullSyncSlices | None,
        progress: Mapping[str, datetime],
        slow_entries: Mapping[str, Mapping[str, Any]],
    ) -> SliceRun:
        slow = {url: entry for url, raw in slow_entries.items() if (entry := SlowList.from_entry(raw)) is not None}
        return cls(source_url=source_url, config=config, progress=dict(progress), slow=slow)


@dataclass
class BodySlices:
    """Befund zu Zeitscheiben und langsamen Listen einer Kommune in einem Lauf."""

    run: SliceRun
    #: Listen, deren letzte Scheibe (ohne Obergrenze) dieser Lauf ganz gelesen hat
    finished: set[str] = field(default_factory=set)
    #: Ganz gelesene Scheiben je Liste
    read: Counter[str] = field(default_factory=Counter)
    #: Typ der Liste je Adresse (für Protokoll und Zählung)
    types: dict[str, str] = field(default_factory=dict)
    #: In Scheiben gelieferte Objekte ohne lesbares ``modified`` je Typ (Kennungen)
    without_modified: dict[str, set[str]] = field(default_factory=dict)
    #: Ausgesetzte Listen dieses Laufs -> ausgesetzt bis
    suspended: dict[str, datetime] = field(default_factory=dict)
    #: Meldungen für das Sync-Protokoll (``errors``)
    notes: list[str] = field(default_factory=list)

    def note_without_modified(self, entity_type: str, page: list[dict[str, Any]]) -> None:
        for item in page:
            if modified_of(item) is not None:
                continue
            external_id = item.get("id") if isinstance(item, dict) else None
            ids = self.without_modified.setdefault(entity_type, set())
            ids.add(external_id if isinstance(external_id, str) and external_id else f"ohne Kennung {len(ids) + 1}")

    def summary(self) -> dict[str, Any]:
        """Eintrag ``full_sync_slices`` im Sync-Protokoll: Stand je Liste und Objekte ohne ``modified``."""
        lists: dict[str, Any] = {}
        for list_url in sorted(set(self.read) | self.finished | set(self.types)):
            progress = self.run.progress.get(list_url)
            lists[list_url] = {
                "type": self.types.get(list_url),
                "slices_read": self.read.get(list_url, 0),
                "finished": list_url in self.finished,
                "next_slice_from": _iso(progress),
            }
        return {
            "lists": lists,
            "without_modified": {name: len(ids) for name, ids in sorted(self.without_modified.items())},
            "note": "Objekte ohne modified erfasst der Vollabgleich in Zeitscheiben nicht",
        }
