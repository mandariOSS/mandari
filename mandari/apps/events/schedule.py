# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne im Code (``docs/adr/20260929-auftraege-und-zeitplaene.md``).

Ein Zeitplan legt zu festen Terminen einen Auftrag an; ausgeführt wird er vom Runner wie jeder
andere Auftrag (``apps.events.task_runner``). Angelegt werden die Aufträge vom Leader
``scheduler`` (``apps.events.scheduler``), bei mehreren Workern also genau einmal je Termin.

Registriert wird im Code, üblich in einem Modul ``schedules.py`` einer App (wird beim Start des
Schedulers geladen) oder als Dekorator über ``@task``::

    from apps.events.schedule import Catchup, cron, every

    every(minutes=15)(erinnerungen_senden)
    cron("30 3 * * *", catchup=Catchup.AUSLASSEN)(bericht_senden)

    @cron("0 4 * * *")
    @task
    def aufraeumen() -> None: ...

- ``every(...)``: feste Abstände ab 1970-01-01 UTC, ``every(minutes=15)`` also :00, :15, :30, :45.
  Mindestens eine Minute.
- ``cron("min std tag monat wochentag")``: fünf Felder wie in crontab (``*``, Listen, Bereiche,
  Schritte; Wochentag 0 oder 7 = Sonntag; sind Tag und Wochentag beide eingeschränkt, genügt einer).
  Gerechnet wird in ``TIME_ZONE``. Fällt ein Termin in die ausgelassene Stunde der Zeitumstellung,
  läuft er eine Stunde später; in der doppelten Stunde läuft er einmal (Termine darin pausieren
  also eine Stunde). Häufige Abstände deshalb mit ``every()``, das in UTC rechnet.
- Verpasste Termine (kein Scheduler lief): ``Catchup.NACHHOLEN`` (Standard) legt **einen** Auftrag
  für den jüngsten verpassten Termin an; ``Catchup.AUSLASSEN`` nur, wenn der Termin höchstens
  ``grace`` zurückliegt, sonst geht es mit dem nächsten Termin weiter.
- Ein neu registrierter Zeitplan beginnt mit seinem nächsten Termin, nicht sofort beim Deploy.
- ``EVENTS_SCHEDULES_DISABLED`` (Namen, kommagetrennt) schaltet einzelne Zeitpläne ab: Ihre Termine
  verstreichen ohne Auftrag (``disabled_schedules``). Wieder eingeschaltet, geht es mit dem nächsten
  Termin weiter; verstrichene werden nicht nachgeholt.

Argumente müssen JSON sein und dürfen wie bei jedem Auftrag nur Kennungen enthalten.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.tasks import Task
from django.utils.json import normalize_json
from django.utils.module_loading import autodiscover_modules

#: Name des Moduls, das je App nach Zeitplänen durchsucht wird
MODULE_NAME: Final = "schedules"
#: Toleranz für ``Catchup.AUSLASSEN``, wenn nichts angegeben ist
DEFAULT_GRACE: Final = timedelta(minutes=5)
_EPOCHE: Final = datetime(1970, 1, 1, tzinfo=UTC)
_EINE_MINUTE: Final = timedelta(minutes=1)
#: Obergrenze der Suche nach dem jüngsten Cron-Termin (Schritte); reicht für Jahre zurück
_MAX_SCHRITTE: Final = 200_000
#: So weit sucht ``Cron.latest`` höchstens zurück. Jeder mögliche Ausdruck hat darin einen Termin
#: (Tag und Wochentag wiederholen sich spätestens nach 28 Jahren, der 29. Februar nach 8 Jahren).
_SUCHFENSTER_JAHRE: Final = 60
#: längster Monat (Februar mit Schalttag), um unmögliche Tage wie ``30 2`` zu erkennen
_MONATSLAENGE: Final[Mapping[int, int]] = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}


class Catchup(enum.StrEnum):
    """Was mit Terminen geschieht, die verpasst wurden, weil kein Scheduler lief."""

    NACHHOLEN = "nachholen"
    AUSLASSEN = "auslassen"


class Trigger:
    """Terminregel: liefert den jüngsten Termin bis einschließlich ``now``."""

    def latest(self, now: datetime) -> datetime:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class Every(Trigger):
    """Feste Abstände, ausgerichtet an Vielfachen des Abstands ab 1970-01-01 UTC."""

    interval: timedelta

    def __post_init__(self) -> None:
        if self.interval < _EINE_MINUTE:
            raise ImproperlyConfigured("every(): Abstand mindestens eine Minute")

    def latest(self, now: datetime) -> datetime:
        return _EPOCHE + ((now - _EPOCHE) // self.interval) * self.interval

    def describe(self) -> str:
        return f"alle {self.interval}"


def _feld(text: str, unten: int, oben: int, ausdruck: str) -> frozenset[int]:
    werte: set[int] = set()
    for teil in text.split(","):
        bereich, _, schritt = teil.partition("/")
        try:
            weite = int(schritt) if schritt else 1
            if bereich == "*":
                anfang, ende = unten, oben
            elif "-" in bereich:
                von, _, bis = bereich.partition("-")
                anfang, ende = int(von), int(bis)
            else:
                anfang = int(bereich)
                ende = oben if schritt else anfang
        except ValueError:
            raise ImproperlyConfigured(
                f"cron({ausdruck!r}): {teil!r} ist keine Zahl, kein Bereich und kein *"
            ) from None
        if weite < 1 or not unten <= anfang <= ende <= oben:
            raise ImproperlyConfigured(f"cron({ausdruck!r}): {teil!r} liegt nicht in {unten}–{oben}")
        werte.update(range(anfang, ende + 1, weite))
    return frozenset(werte)


@dataclass(frozen=True)
class Cron(Trigger):
    """Crontab-Ausdruck mit fünf Feldern in der Zeitzone ``TIME_ZONE`` (oder ``timezone``)."""

    expression: str
    timezone: str | None = None
    minutes: frozenset[int] = field(init=False)
    hours: frozenset[int] = field(init=False)
    days: frozenset[int] = field(init=False)
    months: frozenset[int] = field(init=False)
    weekdays: frozenset[int] = field(init=False)
    #: Tag bzw. Wochentag eingeschränkt (Feld beginnt nicht mit ``*``), wie in Vixie-Cron
    days_restricted: bool = field(init=False)
    weekdays_restricted: bool = field(init=False)

    def __post_init__(self) -> None:
        felder = self.expression.split()
        if len(felder) != 5:
            raise ImproperlyConfigured(f"cron({self.expression!r}): fünf Felder erwartet (min std tag monat wochentag)")
        minute, stunde, tag, monat, wochentag = felder
        setzen = object.__setattr__
        setzen(self, "minutes", _feld(minute, 0, 59, self.expression))
        setzen(self, "hours", _feld(stunde, 0, 23, self.expression))
        setzen(self, "days", _feld(tag, 1, 31, self.expression))
        setzen(self, "months", _feld(monat, 1, 12, self.expression))
        # 7 ist wie 0 Sonntag
        setzen(self, "weekdays", frozenset(w % 7 for w in _feld(wochentag, 0, 7, self.expression)))
        setzen(self, "days_restricted", not tag.startswith("*"))
        setzen(self, "weekdays_restricted", not wochentag.startswith("*"))
        # Nur der Tag entscheidet (Wochentag ``*``): Es muss ihn in einem der Monate geben, sonst gibt es
        # nie einen Termin (z. B. ``0 0 30 2 *``). In allen anderen Fällen passt jeder Monat irgendwann.
        if (
            self.days_restricted
            and not self.weekdays_restricted
            and not any(d <= _MONATSLAENGE[m] for m in self.months for d in self.days)
        ):
            raise ImproperlyConfigured(f"cron({self.expression!r}): Tag und Monat ergeben nie einen Termin")
        self._zone()  # unbekannte Zeitzone früh melden

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone or settings.TIME_ZONE)
        except (ZoneInfoNotFoundError, ValueError):
            raise ImproperlyConfigured(f"cron({self.expression!r}): unbekannte Zeitzone") from None

    def _tag_passt(self, tag: datetime) -> bool:
        am_tag = tag.day in self.days
        am_wochentag = (tag.weekday() + 1) % 7 in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return am_tag or am_wochentag
        return am_tag and am_wochentag

    def latest(self, now: datetime) -> datetime:
        zone = self._zone()
        # Gesucht wird in Wanduhrzeit; ein Termin in der ausgelassenen Stunde liegt danach in UTC
        # hinter ``now`` und wird dann übersprungen, bis ``now`` ihn erreicht.
        wand = now.astimezone(zone).replace(second=0, microsecond=0, tzinfo=None)
        # Untergrenze statt Suche bis ins Jahr 1 (dort ``OverflowError`` statt einer klaren Meldung)
        untergrenze = wand.replace(year=max(wand.year - _SUCHFENSTER_JAHRE, 2), month=1, day=1, hour=0, minute=0)
        for _ in range(_MAX_SCHRITTE):
            if wand < untergrenze:
                break
            if wand.month not in self.months:
                wand = wand.replace(day=1, hour=0, minute=0) - _EINE_MINUTE
            elif not self._tag_passt(wand):
                wand = wand.replace(hour=0, minute=0) - _EINE_MINUTE
            elif wand.hour not in self.hours:
                wand = wand.replace(minute=0) - _EINE_MINUTE
            elif wand.minute not in self.minutes:
                wand -= _EINE_MINUTE
            else:
                # fold=0: in der doppelten Stunde gilt die erste Uhrzeit, der Termin läuft einmal
                termin = wand.replace(tzinfo=zone, fold=0).astimezone(UTC)
                if termin <= now:
                    return termin
                wand -= _EINE_MINUTE
        raise ImproperlyConfigured(f"cron({self.expression!r}): kein Termin gefunden")

    def describe(self) -> str:
        return f"cron {self.expression}"


@dataclass(frozen=True)
class Schedule:
    """Ein registrierter Zeitplan."""

    name: str
    task: Task[Any, Any]
    trigger: Trigger
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    catchup: Catchup = Catchup.NACHHOLEN
    grace: timedelta = DEFAULT_GRACE

    def is_due(self, slot: datetime, now: datetime) -> bool:
        """Soll für den (noch nicht geplanten) Termin ``slot`` jetzt ein Auftrag entstehen?"""
        return self.catchup == Catchup.NACHHOLEN or now - slot <= self.grace


class ScheduleRegistry:
    """Alle Zeitpläne eines Prozesses; Namen sind eindeutig."""

    def __init__(self) -> None:
        self._eintraege: dict[str, Schedule] = {}

    def add(self, eintrag: Schedule) -> None:
        vorhanden = self._eintraege.get(eintrag.name)
        if vorhanden is not None and vorhanden != eintrag:
            raise ImproperlyConfigured(f"Zeitplan {eintrag.name!r} ist bereits anders registriert")
        self._eintraege[eintrag.name] = eintrag

    def remove(self, name: str) -> None:
        self._eintraege.pop(name, None)

    def get(self, name: str) -> Schedule | None:
        return self._eintraege.get(name)

    def __iter__(self) -> Iterator[Schedule]:
        return iter(sorted(self._eintraege.values(), key=lambda e: e.name))

    def __len__(self) -> int:
        return len(self._eintraege)


#: Register des Prozesses
registry = ScheduleRegistry()


def _registrieren[T: Task[Any, Any]](
    trigger: Trigger,
    *,
    name: str | None,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any] | None,
    catchup: Catchup,
    grace: timedelta,
    ziel: ScheduleRegistry | None,
) -> Callable[[T], T]:
    def dekorator(task: T) -> T:
        if not isinstance(task, Task):
            raise ImproperlyConfigured("Zeitpläne brauchen eine @task-Funktion")
        try:
            normalize_json([list(args), dict(kwargs or {})])
        except TypeError:
            raise ImproperlyConfigured(f"Zeitplan für {task.module_path}: Argumente müssen JSON sein") from None
        (ziel if ziel is not None else registry).add(
            Schedule(
                name=name or task.module_path,
                task=task,
                trigger=trigger,
                args=tuple(args),
                kwargs=dict(kwargs or {}),
                catchup=Catchup(catchup),
                grace=grace,
            )
        )
        return task

    return dekorator


def every[T: Task[Any, Any]](
    *,
    days: int = 0,
    hours: int = 0,
    minutes: int = 0,
    name: str | None = None,
    args: tuple[Any, ...] = (),
    kwargs: Mapping[str, Any] | None = None,
    catchup: Catchup = Catchup.NACHHOLEN,
    grace: timedelta = DEFAULT_GRACE,
    registry: ScheduleRegistry | None = None,
) -> Callable[[T], T]:
    """Registriert einen Zeitplan mit festem Abstand (mindestens eine Minute)."""
    return _registrieren(
        Every(timedelta(days=days, hours=hours, minutes=minutes)),
        name=name,
        args=args,
        kwargs=kwargs,
        catchup=catchup,
        grace=grace,
        ziel=registry,
    )


def cron[T: Task[Any, Any]](
    expression: str,
    *,
    timezone: str | None = None,
    name: str | None = None,
    args: tuple[Any, ...] = (),
    kwargs: Mapping[str, Any] | None = None,
    catchup: Catchup = Catchup.NACHHOLEN,
    grace: timedelta = DEFAULT_GRACE,
    registry: ScheduleRegistry | None = None,
) -> Callable[[T], T]:
    """Registriert einen Zeitplan mit Crontab-Ausdruck (fünf Felder, Zeitzone ``TIME_ZONE``)."""
    return _registrieren(
        Cron(expression, timezone),
        name=name,
        args=args,
        kwargs=kwargs,
        catchup=catchup,
        grace=grace,
        ziel=registry,
    )


def disabled_schedules() -> frozenset[str]:
    """Namen der abgeschalteten Zeitpläne (``EVENTS_SCHEDULES_DISABLED``)."""
    roh = getattr(settings, "EVENTS_SCHEDULES_DISABLED", ()) or ()
    teile = roh.split(",") if isinstance(roh, str) else roh
    return frozenset(str(teil).strip() for teil in teile if str(teil).strip())


def autodiscover() -> None:
    """Lädt ``schedules.py`` aller installierten Apps, damit sich deren Zeitpläne registrieren."""
    autodiscover_modules(MODULE_NAME)
