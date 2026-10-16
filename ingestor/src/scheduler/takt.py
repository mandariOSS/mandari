# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Takt je Quelle und Abruf nach Bedarf (Issue #1026).

Jede Quelle hat ihren eigenen Zeitpunkt „nächster Abgleich fällig“ (``sync_config["sync_state"]["next_due"]``).
Der Abstand bis dahin richtet sich nach der Uhrzeit (Ortszeit ``Europe/Berlin``), nach Sitzungen der Quelle und
danach, ob die letzten Läufe etwas geändert haben. Die Werte gelten für alle Quellen und lassen sich je Quelle über
``sync_config["sync_schedule"]`` ändern (ungültige Werte gelten als nicht gesetzt):

``workday_minutes`` (Standard: ``daemon --interval``, sonst 10)
    Grundtakt werktags (Montag bis Freitag) in der Arbeitszeit ``workday_hours`` (Standard 07:00 bis 19:00).
``off_hours_minutes`` (60)
    Werktags außerhalb der Arbeitszeit.
``weekend_minutes`` (120)
    Samstag und Sonntag.
``meeting_minutes`` (5), ``meeting_hours`` (07:00 bis 22:00), ``meeting_days_before`` (1)
    An Tagen, an denen eine Sitzung der Quelle beginnt (``oparl_meetings``, nicht abgesagt, nicht gelöscht), und an
    so vielen Tagen davor, jeweils nur innerhalb von ``meeting_hours``. Gilt vor den übrigen Regeln, auch am
    Wochenende.
``idle_runs`` (3), ``idle_max_minutes`` (120), ``meeting_idle_max_minutes`` (10)
    Nach je ``idle_runs`` Läufen in Folge ohne Änderung verdoppelt sich der Abstand, höchstens bis
    ``idle_max_minutes``, im Sitzungstakt höchstens bis ``meeting_idle_max_minutes`` (ein längerer Grundtakt bleibt,
    wie er ist). Ein Lauf mit Änderung setzt den Zähler zurück, ebenso der Wechsel in einen anderen Grundtakt (etwa um
    07:00 Uhr in die Arbeitszeit). ``0`` schaltet das Verlängern ab.
``tz`` (``Europe/Berlin``)
    Zeitzone für Arbeitszeit, Wochenende und Sitzungstage.

Alle Abstände sind ganze Minuten von 5 bis 1440; kleinere Werte gelten als 5 Minuten. Der nächste Lauf ist einen
Abstand nach dem Ende des letzten fällig, früher nur, wenn vorher ein kürzerer Grundtakt beginnt: Dann ist er zu
dessen Beginn fällig (etwa um 07:00 Uhr statt um 07:40 Uhr). Die Schonung bei Fehlern
(``src/sync/orchestrator.py``, ``source_backoff_until``) geht allem vor.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.client.source_options import FullSyncWindow, SourceFetchOptions

SCHEDULE_KEY = "sync_schedule"
STATE_KEY = "sync_state"

DEFAULT_TZ = "Europe/Berlin"
WORKDAY_MINUTES = 10
OFF_HOURS_MINUTES = 60
WEEKEND_MINUTES = 120
MEETING_MINUTES = 5
WORKDAY_HOURS = (time(7, 0), time(19, 0))
MEETING_HOURS = (time(7, 0), time(22, 0))
MEETING_DAYS_BEFORE = 1
MAX_MEETING_DAYS_BEFORE = 7
IDLE_RUNS = 3
MAX_IDLE_RUNS = 100
IDLE_MAX_MINUTES = 120
#: An Sitzungstagen verlängert die Ruhe den Abstand nur bis hierher: Der kurze Takt gilt den ganzen Tag
MEETING_IDLE_MAX_MINUTES = 10
#: Kürzester und längster Abstand je Quelle in Minuten
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 24 * 60
#: Ohne eigenes ``full_sync_window`` der Quelle beginnt ihr Vollabgleich in diesem Fenster ab ``--full-sync-hour``
FULL_SYNC_DEFAULT_HOURS = 5

#: Grundtakte (für Protokoll und Metriken)
PERIOD_MEETING = "sitzungstag"
PERIOD_WORKDAY = "arbeitszeit"
PERIOD_OFF_HOURS = "ausserhalb"
PERIOD_WEEKEND = "wochenende"


def _minutes(value: Any) -> int | None:
    """Ganze Minuten: unter 5 gelten als 5, über 1440 oder keine ganze Zahl als nicht gesetzt."""
    if isinstance(value, bool) or not isinstance(value, int) or value > MAX_INTERVAL_MINUTES:
        return None
    return max(value, MIN_INTERVAL_MINUTES)


def _count(value: Any, upper: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= upper else None


def _nonneg(value: Any, upper: int, default: int) -> int:
    counted = _count(value, upper)
    return default if counted is None else counted


def _clock(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    return time(hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _hours(value: Any, default: tuple[time, time]) -> tuple[time, time]:
    """``{"start": "07:00", "end": "19:00"}``; Beginn vor Ende am selben Tag, sonst der Standard."""
    if not isinstance(value, Mapping):
        return default
    start, end = _clock(value.get("start")), _clock(value.get("end"))
    if start is None or end is None or not start < end:
        return default
    return start, end


def _stamp(value: Any) -> datetime | None:
    """Zeitpunkt aus ``sync_state`` (ISO 8601); ohne Zeitzone als UTC."""
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


@dataclass(frozen=True)
class SyncSchedule:
    """Takt einer Quelle (siehe Moduldokumentation)."""

    workday_minutes: int = WORKDAY_MINUTES
    off_hours_minutes: int = OFF_HOURS_MINUTES
    weekend_minutes: int = WEEKEND_MINUTES
    meeting_minutes: int = MEETING_MINUTES
    workday_hours: tuple[time, time] = WORKDAY_HOURS
    meeting_hours: tuple[time, time] = MEETING_HOURS
    meeting_days_before: int = MEETING_DAYS_BEFORE
    idle_runs: int = IDLE_RUNS
    idle_max_minutes: int = IDLE_MAX_MINUTES
    meeting_idle_max_minutes: int = MEETING_IDLE_MAX_MINUTES
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(DEFAULT_TZ))

    @classmethod
    def from_sync_config(cls, sync_config: Any, default_workday_minutes: int | None = None) -> SyncSchedule:
        """Takt aus ``sync_config["sync_schedule"]``; ``default_workday_minutes`` ist ``daemon --interval``."""
        base = cls()
        if default_workday_minutes is not None:
            base = replace(base, workday_minutes=_minutes(default_workday_minutes) or WORKDAY_MINUTES)
        value = sync_config.get(SCHEDULE_KEY) if isinstance(sync_config, Mapping) else None
        if not isinstance(value, Mapping):
            return base
        try:
            tz = ZoneInfo(str(value.get("tz") or DEFAULT_TZ))
        except (ZoneInfoNotFoundError, ValueError):
            tz = base.tz
        return cls(
            workday_minutes=_minutes(value.get("workday_minutes")) or base.workday_minutes,
            off_hours_minutes=_minutes(value.get("off_hours_minutes")) or base.off_hours_minutes,
            weekend_minutes=_minutes(value.get("weekend_minutes")) or base.weekend_minutes,
            meeting_minutes=_minutes(value.get("meeting_minutes")) or base.meeting_minutes,
            workday_hours=_hours(value.get("workday_hours"), base.workday_hours),
            meeting_hours=_hours(value.get("meeting_hours"), base.meeting_hours),
            meeting_days_before=_nonneg(value.get("meeting_days_before"), MAX_MEETING_DAYS_BEFORE, MEETING_DAYS_BEFORE),
            idle_runs=_nonneg(value.get("idle_runs"), MAX_IDLE_RUNS, IDLE_RUNS),
            idle_max_minutes=_minutes(value.get("idle_max_minutes")) or base.idle_max_minutes,
            meeting_idle_max_minutes=_minutes(value.get("meeting_idle_max_minutes")) or base.meeting_idle_max_minutes,
            tz=tz,
        )

    def meeting_days(self, meeting_starts: Iterable[datetime]) -> frozenset[date]:
        """Tage mit kurzem Takt: Tage mit Sitzungsbeginn (Ortszeit) und ``meeting_days_before`` Tage davor."""
        days: set[date] = set()
        for start in meeting_starts:
            stamp = start.replace(tzinfo=UTC) if start.tzinfo is None else start
            day = stamp.astimezone(self.tz).date()
            days.update(day - timedelta(days=offset) for offset in range(self.meeting_days_before + 1))
        return frozenset(days)

    def base(self, at: datetime, meeting_days: frozenset[date] = frozenset()) -> tuple[int, str]:
        """Grundtakt in Minuten und seine Art zum Zeitpunkt ``at``."""
        local = at.astimezone(self.tz)
        clock = local.time().replace(tzinfo=None)
        if local.date() in meeting_days and self.meeting_hours[0] <= clock < self.meeting_hours[1]:
            return self.meeting_minutes, PERIOD_MEETING
        if local.weekday() >= 5:
            return self.weekend_minutes, PERIOD_WEEKEND
        if self.workday_hours[0] <= clock < self.workday_hours[1]:
            return self.workday_minutes, PERIOD_WORKDAY
        return self.off_hours_minutes, PERIOD_OFF_HOURS

    def interval(self, at: datetime, meeting_days: frozenset[date] = frozenset(), idle_runs: int = 0) -> int:
        """
        Abstand in Minuten: Grundtakt, nach je ``idle_runs`` Läufen ohne Änderung verdoppelt bis zur Obergrenze, im
        Sitzungstakt bis ``meeting_idle_max_minutes``.
        """
        minutes, period = self.base(at, meeting_days)
        if self.idle_runs <= 0 or idle_runs < self.idle_runs:
            return minutes
        cap = self.meeting_idle_max_minutes if period == PERIOD_MEETING else self.idle_max_minutes
        steps = min(idle_runs // self.idle_runs, 16)
        return min(minutes * (1 << steps), max(minutes, cap))

    def _boundaries(self, after: datetime, before: datetime) -> list[datetime]:
        """Wechsel des Grundtakts (Tagesbeginn, Arbeitszeit, Sitzungszeit) zwischen ``after`` und ``before`` (UTC)."""
        clocks = {time(0, 0), *self.workday_hours, *self.meeting_hours}
        day = after.astimezone(self.tz).date()
        last = before.astimezone(self.tz).date()
        found: list[datetime] = []
        while day <= last:
            for clock in clocks:
                moment = datetime.combine(day, clock, tzinfo=self.tz).astimezone(UTC)
                if after < moment < before:
                    found.append(moment)
            day += timedelta(days=1)
        return sorted(found)

    def next_due(
        self, finished: datetime, meeting_days: frozenset[date] = frozenset(), idle_runs: int = 0
    ) -> tuple[datetime, int]:
        """
        Nächster fälliger Lauf nach einem Lauf, der um ``finished`` endete, und der Abstand dafür in Minuten.

        Einen Abstand nach dem Ende; wechselt vorher der Grundtakt zu einem, der kürzer ist als dieser Abstand, zu
        Beginn des neuen Grundtakts.
        """
        minutes = self.interval(finished, meeting_days, idle_runs)
        candidate = finished + timedelta(minutes=minutes)
        current = self.base(finished, meeting_days)[0]
        for moment in self._boundaries(finished, candidate):
            changed = self.base(moment, meeting_days)[0]
            if changed != current and changed < minutes:
                return moment, minutes
            current = changed
        return candidate, minutes


@dataclass
class SyncState:
    """Zustand des Takts einer Quelle, vom Daemon gepflegt (``sync_config["sync_state"]``)."""

    next_due: datetime | None = None
    idle_runs: int = 0
    interval_minutes: int | None = None
    base_minutes: int | None = None
    period: str | None = None
    last_run: datetime | None = None
    last_changes: int | None = None
    full_attempted: datetime | None = None

    @classmethod
    def from_sync_config(cls, sync_config: Any) -> SyncState:
        value = sync_config.get(STATE_KEY) if isinstance(sync_config, Mapping) else None
        if not isinstance(value, Mapping):
            return cls()
        idle = value.get("idle_runs")
        interval = value.get("interval_minutes")
        base = value.get("base_minutes")
        changes = value.get("last_changes")
        return cls(
            next_due=_stamp(value.get("next_due")),
            idle_runs=idle if isinstance(idle, int) and not isinstance(idle, bool) and idle >= 0 else 0,
            interval_minutes=interval if isinstance(interval, int) and not isinstance(interval, bool) else None,
            base_minutes=base if isinstance(base, int) and not isinstance(base, bool) else None,
            period=value.get("period") if isinstance(value.get("period"), str) else None,
            last_run=_stamp(value.get("last_run")),
            last_changes=changes if isinstance(changes, int) and not isinstance(changes, bool) else None,
            full_attempted=_stamp(value.get("full_attempted")),
        )

    def to_dict(self) -> dict[str, Any]:
        def iso(stamp: datetime | None) -> str | None:
            return stamp.astimezone(UTC).isoformat(timespec="seconds") if stamp is not None else None

        return {
            "next_due": iso(self.next_due),
            "idle_runs": self.idle_runs,
            "interval_minutes": self.interval_minutes,
            "base_minutes": self.base_minutes,
            "period": self.period,
            "last_run": iso(self.last_run),
            "last_changes": self.last_changes,
            "full_attempted": iso(self.full_attempted),
        }

    def after_run(
        self,
        schedule: SyncSchedule,
        finished: datetime,
        *,
        success: bool,
        changes: int,
        full: bool,
        meeting_days: frozenset[date] = frozenset(),
    ) -> SyncState:
        """
        Zustand nach einem Lauf: Zähler der Läufe ohne Änderung, Abstand und nächster fälliger Lauf.

        Nur ein erfolgreicher inkrementeller Lauf zählt für die Ruhe: ohne Änderung +1, mit Änderung 0. Ein
        Vollabgleich schreibt jedes Objekt und sagt über Änderungen nichts; ein gescheiterter Lauf bleibt der
        Schonung überlassen. Wechselt der Grundtakt, beginnt der Zähler neu.
        """
        base_minutes, period = schedule.base(finished, meeting_days)
        idle = self.idle_runs if self.base_minutes == base_minutes else 0
        if success and not full:
            idle = 0 if changes > 0 else idle + 1
        next_due, interval = schedule.next_due(finished, meeting_days, idle)
        return replace(
            self,
            next_due=next_due,
            idle_runs=idle,
            interval_minutes=interval,
            base_minutes=base_minutes,
            period=period,
            last_run=finished,
            last_changes=changes,
        )


def default_full_sync_window(full_sync_hour: int | None, tz: ZoneInfo | None = None) -> FullSyncWindow | None:
    """
    Fenster, in dem der Vollabgleich einer Quelle ohne eigenes ``full_sync_window`` beginnt: ab ``--full-sync-hour``
    für ``FULL_SYNC_DEFAULT_HOURS`` Stunden (Ortszeit ``Europe/Berlin``). Stunden außerhalb 0 bis 23 schalten den
    geplanten Vollabgleich ab (``None``).
    """
    if full_sync_hour is None or not 0 <= full_sync_hour <= 23:
        return None
    end = time((full_sync_hour + FULL_SYNC_DEFAULT_HOURS) % 24, 0)
    return FullSyncWindow(start=time(full_sync_hour, 0), end=end, tz=tz or ZoneInfo(DEFAULT_TZ))


def full_sync_due(
    options: SourceFetchOptions,
    default_window: FullSyncWindow | None,
    now: datetime,
    attempted: datetime | None,
    last_successful_full_sync: datetime | None,
) -> bool:
    """
    Ist ein geplanter Vollabgleich der Quelle fällig?

    Ja, wenn ``now`` im Fenster liegt (eigenes ``full_sync_window`` der Quelle, sonst das Standardfenster), in diesem
    Fenster noch keiner begonnen hat und ``full_sync_every_days`` ihn zulässt. Ohne Standardfenster (Vollabgleich
    abgeschaltet) nie. Ein begonnener Vollabgleich zählt auch, wenn er scheitert: Er wird im nächsten Fenster
    wiederholt, nicht mehrmals in derselben Nacht.
    """
    if default_window is None:
        return False
    window = options.full_sync_window or default_window
    opened = window.opened_at(now)
    if opened is None:
        return False
    if attempted is not None and attempted >= opened:
        return False
    planned, _ = options.full_sync_plan(True, now=now, last_full_sync=last_successful_full_sync)
    return planned
