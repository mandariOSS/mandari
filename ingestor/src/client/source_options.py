# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrufoptionen je OParl-Quelle aus ``OParlSource.sync_config`` (additiv, keine Migration).

Die Standardwerte des Ingestors passen für die meisten Ratsinformationssysteme. Einzelne Quellen brauchen
mehr Schonung oder haben Eigenheiten, die sich nicht automatisch erkennen lassen:

``request_interval``
    Mindestabstand in Sekunden zwischen dem Beginn zweier Anfragen an den Host dieser Quelle. Ohne Wert gilt
    der Standard des Ingestors (``INGESTOR_REQUEST_INTERVAL``, eine Anfrage je Sekunde). Der Abstand gilt über
    alle Abrufplätze, Quellen auf demselben Host und Prozesse hinweg (Daemon, Einzelabgleich, Django), weil
    alle denselben Zeitstempel in Redis nutzen (``src/client/host_pacing.py``). Er darf nur verlangsamen:
    Werte unter einer Sekunde, auch ``0``, gelten als eine Sekunde (Warnung im Log,
    ``mandari_oparl.pacing.MIN_REQUEST_INTERVAL``, Issue #1025).
``modified_since_margin_minutes``
    Sicherheitsabstand des Änderungsfensters in Minuten (0 bis 10080). Inkrementelle Läufe fragen je Kommune
    nach allem, was seit dem Beginn ihres letzten vollständig erfolgreichen Abgleichs abzüglich dieses Abstands
    geändert wurde, nie weiter zurück als sieben Tage. Ohne Wert gilt der Standard des Ingestors
    (``INGESTOR_MODIFIED_SINCE_MARGIN_MINUTES``, 120 Minuten). Werden Objekte erst Stunden nach ihrem
    ``modified`` sichtbar, braucht die Quelle mehr. Listen, deren ``modified`` keine Uhrzeit trägt, erkennt der
    Ingestor selbst und fragt sie ab 00:00 Uhr an (``sync_config["timed_lists"]``).
``list_params``
    Zusätzliche Parameter für die erste Seite jeder Liste, z. B. ``{"size": 100}`` bei ALLRIS: weniger,
    dafür größere Seiten. Die Folgeseiten kommen aus ``links.next`` der Quelle.
``carry_modified_since``
    Die Quelle filtert mit ``modified_since``, lässt den Parameter aber in ``links.next`` weg (ALLRIS). Der
    Client hängt ihn dann an jede Folgeseite an, statt die Quelle als „ohne Filter“ zu behandeln. Ohne den
    Schalter liefe der inkrementelle Abgleich über die ungefilterte, aufsteigend sortierte Liste und bräche
    nach fünf unveränderten Seiten ab, bevor er neue Einträge erreicht. Der Schalter hebt einen Vermerk
    „filtert nicht“ für den Server auf (``sync_config["unfiltered_hosts"]``), nicht den für einzelne Listen:
    Lehnt eine Liste den Filter ab (Münster: HTTP 401 bei ``organization``), läuft nur sie ohne Filter
    (``sync_config["unfiltered_lists"]``, ``src/client/filter_notes.py``, Issue #1041).
``file_downloads``
    ``false``: Dateien dieser Quelle nicht automatisch abrufen (Textextraktion im Ingestor, Dateicache und
    Vorschau in Django). Für Quellen, die Dokumente nur hinter einer Zugangsprüfung für Menschen ausliefern;
    die Dateien bleiben in der Warteschlange und werden nachgeholt, sobald der Schalter fällt.
``sync_window``
    Abruffenster für alle Abgleiche der Quelle (Issue #1186), z. B. ``{"start": "23:00", "end": "01:00"}``
    (Ortszeit, Standard ``Europe/Berlin``, abweichend über ``"tz"``; über Mitternacht erlaubt; Format wie
    ``full_sync_window``). Außerhalb des Fensters ruft der Ingestor bei der Quelle nichts ab: Der Daemon startet sie
    erst zu Beginn des nächsten Fensters (``src/scheduler/takt.py``), Auslöser aus dem Admin verwirft er, und
    ``sync`` auf der Kommandozeile endet ohne Abruf (``SyncResult.outside_window``). Eine Übersteuerung gibt es
    nicht. Ein laufender Abgleich, ob Änderungs- oder Vollabgleich, in Zeitscheiben oder als Scraper, holt nach dem
    Ende des Fensters keine weitere Seite mehr; offene Listen gelten als unvollständig. Die Texterkennung des
    Ingestors lädt außerhalb des Fensters keine Datei der Quelle. Mit ``sync_window`` gilt für den Vollabgleich
    nie das Standardfenster des Daemons, sondern das eigene ``full_sync_window``, begrenzt auf die Schnittmenge mit
    dem Abruffenster (``FullSyncWindow.within``), sonst das Abruffenster selbst.
``full_sync_window``
    Zeitfenster für Vollabgleiche, z. B. ``{"start": "00:00", "end": "06:00"}`` (Ortszeit, Standard
    ``Europe/Berlin``, abweichend über ``"tz"``; über Mitternacht erlaubt). Ein Vollabgleich außerhalb des
    Fensters, ob geplant, aus dem Admin oder per ``sync --full``, läuft als inkrementeller Abgleich. Ein
    laufender Vollabgleich holt nach dem Ende des Fensters keine weitere Seite mehr; die offenen Listen gelten
    als unvollständig und kommen beim nächsten Vollabgleich wieder dran. Mit ``sync_window`` gilt nur der Teil
    innerhalb des Abruffensters (siehe dort); liegt es ganz außerhalb oder in einer anderen Zeitzone, gilt es als
    nicht gesetzt.
``full_sync_every_days``
    Mindestabstand in Tagen (1 bis 365) zwischen zwei vollständig erfolgreichen Vollabgleichen
    (``last_successful_full_sync``). Für Quellen, die zuverlässig mit ``modified_since`` filtern: Der
    nächtliche Vollabgleich läuft dann nur alle paar Wochen, sonst inkrementell. Ein abgebrochener
    Vollabgleich zählt nicht, er wird in der nächsten Nacht wiederholt.
``full_sync_slices``
    Vollabgleich in Zeitscheiben (Issue #1033), z. B. ``{"days": 30, "since": "2010-01-01"}``: für Quellen, deren
    ungefilterte Listen abbrechen (Stadt Hamm, SD.NET RIM: HTTP 504 nach 60 Sekunden). Der Vollabgleich fragt dann
    jede Liste nur mit ``modified_since``/``modified_until`` an, nie ohne Filter: zuerst alles bis ``since``, dann
    Scheiben zu ``days`` Tagen (1 bis 366, Standard 30) von alt nach neu, zuletzt alles ab der letzten Grenze.
    ``since`` ist ein Datum (00:00 UTC) oder ein Zeitpunkt in ISO 8601 vor jetzt. ``lists`` beschränkt die Scheiben
    auf einzelne Typen (``organization``, ``person``, ``membership``, ``meeting``, ``paper``, ``location``,
    ``agendaitem``, ``file``, ``consultation``); die übrigen liest der Vollabgleich wie bisher ohne Filter.
    Unbekannte Namen zählen nicht, ohne gültigen Namen gelten alle. Fortschritt und Befunde pflegt der Ingestor
    (``src/sync/slices.py``). Objekte ohne ``modified`` erfasst ein Abgleich in Zeitscheiben nicht. Ein Vollabgleich
    in Zeitscheiben endet immer mit dem Zeitfenster: dem eigenen ``full_sync_window``, sonst dem Standardfenster des
    Daemons (``--full-sync-hour``, 5 Stunden); ohne beides läuft statt seiner ein inkrementeller Abgleich.
``robots_override``
    Ausnahme von der robots.txt, nur mit Freigabe der Stelle und Pflicht-Vermerk, z. B.
    ``{"scope": "files", "note": "Freigabe per E-Mail vom …, offizielle Anfrage läuft"}``. ``scope``: ``api``,
    ``files`` oder ``all``. Ohne Vermerk gilt die robots.txt (``mandari_oparl.robots.robots_override``).
``sync_schedule``
    Takt der Quelle im Daemon (Arbeitszeit, Sitzungstage, Ruhephasen; Issue #1026). Bedeutung und Standardwerte
    stehen in ``src/scheduler/takt.py``; den Zustand pflegt der Daemon unter ``sync_state``.

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

from src.config import settings

LIST_PARAMS_KEY = "list_params"
CARRY_MODIFIED_SINCE_KEY = "carry_modified_since"
FILE_DOWNLOADS_KEY = "file_downloads"
SYNC_WINDOW_KEY = "sync_window"
FULL_SYNC_WINDOW_KEY = "full_sync_window"
FULL_SYNC_EVERY_DAYS_KEY = "full_sync_every_days"
MAX_FULL_SYNC_EVERY_DAYS = 365
FULL_SYNC_SLICES_KEY = "full_sync_slices"
#: Länge einer Zeitscheibe im Vollabgleich in Tagen: Standard und Obergrenze (Issue #1033)
DEFAULT_SLICE_DAYS = 30
MAX_SLICE_DAYS = 366
#: Listen am Body, die der Vollabgleich in Zeitscheiben lesen kann (Typnamen wie in ``_sync_entity_type``)
SLICE_LIST_TYPES = frozenset(
    {"organization", "person", "membership", "meeting", "paper", "location", "agendaitem", "file", "consultation"}
)
MODIFIED_SINCE_MARGIN_KEY = "modified_since_margin_minutes"
MAX_MODIFIED_SINCE_MARGIN_MINUTES = 7 * 24 * 60
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


def _every_days(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= MAX_FULL_SYNC_EVERY_DAYS else None


def _margin_minutes(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_MODIFIED_SINCE_MARGIN_MINUTES else None


#: Minuten eines Tages (Schnittmenge zweier Fenster, ``FullSyncWindow.within``)
_DAY_MINUTES = 24 * 60


def _minute(clock: time) -> int:
    return clock.hour * 60 + clock.minute


def _clock_of(minute: int) -> time:
    minute %= _DAY_MINUTES
    return time(minute // 60, minute % 60)


@dataclass(frozen=True)
class FullSyncWindow:
    """
    Zeitfenster einer Quelle in Ortszeit: für Vollabgleiche (``sync_config["full_sync_window"]``) und für alle Abrufe
    (``sync_config["sync_window"]``, Issue #1186). Der Beginn gehört zum Fenster, das Ende nicht; liegt das Ende vor
    dem Beginn, reicht das Fenster über Mitternacht.
    """

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

    def opened_at(self, now: datetime | None = None) -> datetime | None:
        """Beginn des Fensters (UTC), in dem ``now`` liegt, sonst ``None`` (Takt je Quelle, Issue #1026)."""
        now = (now or datetime.now(UTC)).astimezone(self.tz)
        if self.closes_at(now) is None:
            return None
        clock = now.time().replace(tzinfo=None)
        # Über Mitternacht und nach Mitternacht: Das Fenster hat am Vortag begonnen
        start_day = now.date() - timedelta(days=1) if self.start > self.end and clock < self.end else now.date()
        return datetime.combine(start_day, self.start, tzinfo=self.tz).astimezone(UTC)

    def next_open(self, at: datetime | None = None) -> datetime:
        """
        ``at`` (UTC), wenn es im Fenster liegt, sonst der Beginn des nächsten Fensters (UTC, Issue #1186). Ein Beginn,
        den es wegen der Zeitumstellung nicht gibt, rechnet wie ``opened_at`` mit der Zeitzone vor der Umstellung.
        """
        at = (at or datetime.now(UTC)).astimezone(UTC)
        if self.closes_at(at) is not None:
            return at
        day = at.astimezone(self.tz).date()
        # Heute oder morgen; der dritte Tag ist nur Reserve für die Zeitumstellung
        starts = (
            datetime.combine(day + timedelta(days=offset), self.start, tzinfo=self.tz).astimezone(UTC)
            for offset in range(3)
        )
        return next(start for start in starts if start > at)

    def within(self, outer: FullSyncWindow) -> FullSyncWindow | None:
        """
        Dieses Fenster, begrenzt auf ``outer`` (Schnittmenge der Uhrzeiten, Issue #1186). ``None``, wenn die
        Schnittmenge leer ist oder die Zeitzonen verschieden sind. Zerfällt sie in zwei Stücke (etwa 22:00 bis 06:00
        Uhr in 05:00 bis 23:00 Uhr), gilt das längere, bei gleicher Länge das, das nach dem Beginn von ``outer`` zuerst
        beginnt.
        """
        if str(self.tz) != str(outer.tz):
            return None
        pieces = [
            (max(lo, outer_lo), min(hi, outer_hi))
            for lo, hi in self._spans()
            for outer_lo, outer_hi in outer._spans()
            if max(lo, outer_lo) < min(hi, outer_hi)
        ]
        # Ein Stück bis Mitternacht und eines ab Mitternacht ergeben ein Fenster über Mitternacht
        late = next((piece for piece in pieces if piece[1] == _DAY_MINUTES), None)
        early = next((piece for piece in pieces if piece[0] == 0), None)
        if late is not None and early is not None:
            pieces = [piece for piece in pieces if piece not in (late, early)] + [(late[0], early[1] + _DAY_MINUTES)]
        if not pieces:
            return None
        origin = _minute(outer.start)
        lo, hi = min(pieces, key=lambda piece: (piece[0] - piece[1], (piece[0] - origin) % _DAY_MINUTES))
        return FullSyncWindow(start=_clock_of(lo), end=_clock_of(hi), tz=outer.tz)

    def _spans(self) -> list[tuple[int, int]]:
        """Minuten des Tages im Fenster als halboffene Strecken ``[von, bis)``."""
        start, end = _minute(self.start), _minute(self.end)
        if start < end:
            return [(start, end)]
        return [(start, _DAY_MINUTES)] + ([(0, end)] if end > 0 else [])

    def label(self) -> str:
        """Für Meldungen, z. B. ``23:00–01:00 Uhr (Europe/Berlin)``."""
        return f"{self.start:%H:%M}–{self.end:%H:%M} Uhr ({self.tz})"


def _slice_since(value: Any) -> datetime | None:
    """Beginn der Zeitscheiben: Datum (00:00 UTC) oder Zeitpunkt in ISO 8601, ohne Zone als UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        stamp = datetime.fromisoformat(text) if "T" in text else datetime.fromisoformat(f"{text}T00:00:00")
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


@dataclass(frozen=True)
class FullSyncSlices:
    """Vollabgleich in Zeitscheiben (``sync_config["full_sync_slices"]``, Issue #1033)."""

    since: datetime
    days: int = DEFAULT_SLICE_DAYS
    #: Typnamen der Listen in Scheiben; ``None``: alle
    lists: frozenset[str] | None = None

    @classmethod
    def parse(cls, value: Any, now: datetime | None = None) -> FullSyncSlices | None:
        if not isinstance(value, Mapping):
            return None
        days = value.get("days", DEFAULT_SLICE_DAYS)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_SLICE_DAYS:
            return None
        since = _slice_since(value.get("since"))
        if since is None or since >= (now or datetime.now(UTC)):
            return None
        names = value.get("lists")
        lists = (
            frozenset(name for name in names if isinstance(name, str) and name in SLICE_LIST_TYPES)
            if isinstance(names, list)
            else frozenset()
        )
        return cls(since=since, days=days, lists=lists or None)

    def covers(self, entity_type: str) -> bool:
        """Liest der Vollabgleich diese Liste in Zeitscheiben?"""
        return entity_type in SLICE_LIST_TYPES and (self.lists is None or entity_type in self.lists)


@dataclass(frozen=True)
class SourceFetchOptions:
    """Abrufoptionen einer Quelle (siehe Moduldokumentation)."""

    request_interval: float | None = None
    list_params: dict[str, str] = field(default_factory=dict)
    carry_modified_since: bool = False
    file_downloads: bool = True
    robots_override: RobotsOverride | None = None
    #: Abruffenster für alle Abgleiche (``sync_window``, Issue #1186)
    sync_window: FullSyncWindow | None = None
    #: Fenster des Vollabgleichs, wie es gilt: mit ``sync_window`` auf die Schnittmenge begrenzt, sonst das Abruffenster
    full_sync_window: FullSyncWindow | None = None
    full_sync_every_days: int | None = None
    modified_since_margin_minutes: int | None = None
    full_sync_slices: FullSyncSlices | None = None

    @classmethod
    def from_sync_config(cls, sync_config: Any) -> SourceFetchOptions:
        if not isinstance(sync_config, Mapping):
            return cls()
        sync_window = FullSyncWindow.parse(sync_config.get(SYNC_WINDOW_KEY))
        full_sync_window = FullSyncWindow.parse(sync_config.get(FULL_SYNC_WINDOW_KEY))
        if sync_window is not None:
            # Der Vollabgleich bleibt im Abruffenster: eigenes Fenster nur, soweit es darin liegt, sonst das
            # Abruffenster selbst, nie das Standardfenster des Daemons
            full_sync_window = (full_sync_window.within(sync_window) if full_sync_window else None) or sync_window
        return cls(
            request_interval=_request_interval(sync_config.get(REQUEST_INTERVAL_KEY)),
            list_params=_list_params(sync_config.get(LIST_PARAMS_KEY)),
            carry_modified_since=sync_config.get(CARRY_MODIFIED_SINCE_KEY) is True,
            # Nur ein ausdrückliches false schaltet ab
            file_downloads=sync_config.get(FILE_DOWNLOADS_KEY) is not False,
            robots_override=robots_override(sync_config),
            sync_window=sync_window,
            full_sync_window=full_sync_window,
            full_sync_every_days=_every_days(sync_config.get(FULL_SYNC_EVERY_DAYS_KEY)),
            modified_since_margin_minutes=_margin_minutes(sync_config.get(MODIFIED_SINCE_MARGIN_KEY)),
            full_sync_slices=FullSyncSlices.parse(sync_config.get(FULL_SYNC_SLICES_KEY)),
        )

    def outside_sync_window(self, now: datetime | None = None) -> bool:
        """Liegt ``now`` außerhalb des Abruffensters der Quelle (Issue #1186)? Ohne ``sync_window`` nie."""
        return self.sync_window is not None and self.sync_window.closes_at(now) is None

    def fetch_deadline(self, now: datetime | None = None, full_deadline: datetime | None = None) -> datetime | None:
        """
        Ende des Abrufs für einen Lauf, der ``now`` beginnt: das frühere von ``full_deadline`` (Ende des Fensters des
        Vollabgleichs, ``full_sync_plan``) und dem Ende des Abruffensters (``sync_window``). ``None``: ohne Ende.
        """
        window_end = self.sync_window.closes_at(now) if self.sync_window is not None else None
        ends = [end for end in (full_deadline, window_end) if end is not None]
        return min(ends) if ends else None

    def modified_since_margin(self) -> timedelta:
        """Sicherheitsabstand des Änderungsfensters: Wert der Quelle, sonst der Standard des Ingestors."""
        minutes = self.modified_since_margin_minutes
        if minutes is None:
            minutes = max(0, settings.modified_since_margin_minutes)
        return timedelta(minutes=minutes)

    def full_window(self, default_window: FullSyncWindow | None = None) -> FullSyncWindow | None:
        """
        Zeitfenster, das den Vollabgleich begrenzt: das eigene ``full_sync_window`` (mit ``sync_window`` immer gesetzt,
        siehe ``from_sync_config``). Ein Vollabgleich in Zeitscheiben läuft über Stunden und braucht immer ein Ende;
        ohne eigenes Fenster gilt für ihn das Standardfenster des Daemons (``default_window``, ab
        ``--full-sync-hour``, Issue #1033).
        """
        if self.full_sync_window is not None:
            return self.full_sync_window
        return default_window if self.full_sync_slices is not None else None

    def full_sync_plan(
        self,
        full: bool,
        now: datetime | None = None,
        last_full_sync: datetime | None = None,
        default_window: FullSyncWindow | None = None,
    ) -> tuple[bool, datetime | None]:
        """
        Vollabgleich jetzt? Liefert ``(full, Abrufende)``. Inkrementell (``(False, None)``), wenn der letzte
        vollständige Vollabgleich jünger als ``full_sync_every_days`` ist oder ``now`` außerhalb des Fensters
        liegt; im Fenster ``(True, Fensterende)``. Ohne Optionen oder für inkrementelle Läufe bleibt es dabei.

        Mit ``full_sync_slices`` gilt ohne eigenes Fenster das Standardfenster ``default_window`` (``full_window``).
        Das Ende des Abruffensters (``sync_window``) für jeden Lauf liefert ``fetch_deadline``.
        Ist auch das nicht bekannt (Aufruf ohne Daemon, geplanter Vollabgleich abgeschaltet), läuft kein Vollabgleich
        in Zeitscheiben, sondern ein inkrementeller: ohne Fensterende liefe er bis in den Tag (Issue #1033).
        """
        if not full:
            return False, None
        now = now or datetime.now(UTC)
        if self.full_sync_every_days is not None and last_full_sync is not None:
            if last_full_sync.tzinfo is None:
                last_full_sync = last_full_sync.replace(tzinfo=UTC)
            # Eine Stunde Spielraum, damit ein nächtlicher Lauf nicht an Minuten Laufzeitunterschied scheitert
            if now - last_full_sync < timedelta(days=self.full_sync_every_days) - timedelta(hours=1):
                return False, None
        window = self.full_window(default_window)
        if window is None:
            return self.full_sync_slices is None, None
        closes_at = window.closes_at(now)
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
