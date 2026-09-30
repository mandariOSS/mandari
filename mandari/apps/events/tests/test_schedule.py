# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zeitpläne im Code (Issue #507): ``every()``/``cron()``, Termine, Zeitumstellung, Register."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.events.schedule import Catchup, Cron, Every, ScheduleRegistry, cron, every
from apps.events.tests import auftraege

T = cast(Any, auftraege)
BERLIN = ZoneInfo("Europe/Berlin")


def berlin(jahr: int, monat: int, tag: int, stunde: int = 0, minute: int = 0, sekunde: int = 0) -> datetime:
    return datetime(jahr, monat, tag, stunde, minute, sekunde, tzinfo=BERLIN).astimezone(UTC)


def utc(jahr: int, monat: int, tag: int, stunde: int = 0, minute: int = 0, sekunde: int = 0) -> datetime:
    return datetime(jahr, monat, tag, stunde, minute, sekunde, tzinfo=UTC)


# -- every() ---------------------------------------------------------------------------------


def test_every_richtet_sich_an_vielfachen_des_abstands_aus() -> None:
    viertelstunde = Every(timedelta(minutes=15))
    assert viertelstunde.latest(utc(2026, 9, 30, 10, 7, 30)) == utc(2026, 9, 30, 10, 0)
    assert viertelstunde.latest(utc(2026, 9, 30, 10, 15)) == utc(2026, 9, 30, 10, 15)
    assert Every(timedelta(days=1)).latest(utc(2026, 9, 30, 23, 59)) == utc(2026, 9, 30)


def test_every_mindestens_eine_minute() -> None:
    with pytest.raises(ImproperlyConfigured):
        Every(timedelta(seconds=30))


# -- cron() ----------------------------------------------------------------------------------


def test_cron_felder() -> None:
    ausdruck = Cron("*/15 8-18 1,15 * 1-5")
    assert ausdruck.minutes == {0, 15, 30, 45}
    assert ausdruck.hours == set(range(8, 19))
    assert ausdruck.days == {1, 15}
    assert ausdruck.months == set(range(1, 13))
    assert ausdruck.weekdays == {1, 2, 3, 4, 5}
    assert Cron("0 0 * * 7").weekdays == {0}, "7 ist Sonntag wie 0"
    assert Cron("5/20 * * * *").minutes == {5, 25, 45}


@pytest.mark.parametrize(
    "ausdruck",
    ["60 * * * *", "* * * *", "* * * * * *", "a * * * *", "*/0 * * * *", "5-1 * * * *", "* 24 * * *", "* * 0 * *"],
)
def test_cron_ungueltig(ausdruck: str) -> None:
    with pytest.raises(ImproperlyConfigured):
        Cron(ausdruck)


def test_cron_unbekannte_zeitzone() -> None:
    with pytest.raises(ImproperlyConfigured):
        Cron("0 0 * * *", timezone="Mond/Krater")


def test_cron_rechnet_in_der_zeitzone_der_installation() -> None:
    taeglich = Cron("30 3 * * *")
    assert taeglich.latest(berlin(2026, 9, 30, 10, 0)) == berlin(2026, 9, 30, 3, 30)
    assert taeglich.latest(berlin(2026, 9, 30, 3, 30)) == berlin(2026, 9, 30, 3, 30), "Termin selbst zählt"
    assert taeglich.latest(berlin(2026, 9, 30, 3, 29, 59)) == berlin(2026, 9, 29, 3, 30)
    assert Cron("30 3 * * *", timezone="UTC").latest(utc(2026, 9, 30, 10, 0)) == utc(2026, 9, 30, 3, 30)


def test_cron_wochentage() -> None:
    werktags = Cron("0 9 * * 1-5")
    # Sonntag, 4. Oktober 2026 → Freitag, 2. Oktober
    assert werktags.latest(berlin(2026, 10, 4, 12, 0)) == berlin(2026, 10, 2, 9, 0)
    assert werktags.latest(berlin(2026, 10, 5, 9, 1)) == berlin(2026, 10, 5, 9, 0)


def test_cron_tag_oder_wochentag_wenn_beide_eingeschraenkt() -> None:
    """Wie in crontab: 1. des Monats oder montags."""
    ausdruck = Cron("0 0 1 * 1")
    # Mittwoch, 30. September 2026 → Montag, 28. September
    assert ausdruck.latest(berlin(2026, 9, 30, 12, 0)) == berlin(2026, 9, 28, 0, 0)
    # Donnerstag, 1. Oktober 2026
    assert ausdruck.latest(berlin(2026, 10, 1, 12, 0)) == berlin(2026, 10, 1, 0, 0)


def test_cron_monate_und_schaltjahr() -> None:
    assert Cron("0 12 29 2 *").latest(berlin(2026, 9, 30)) == berlin(2024, 2, 29, 12, 0)
    assert Cron("15 6 1 1,7 *").latest(berlin(2026, 9, 30)) == berlin(2026, 7, 1, 6, 15)


def test_cron_ausgelassene_stunde_der_sommerzeit() -> None:
    """29. März 2026: 02:00 → 03:00. Der Termin 02:30 läuft eine Stunde später (01:30 UTC)."""
    nachts = Cron("30 2 * * *")
    assert nachts.latest(utc(2026, 3, 29, 1, 10)) == berlin(2026, 3, 28, 2, 30)
    assert nachts.latest(utc(2026, 3, 29, 1, 30)) == utc(2026, 3, 29, 1, 30)
    assert nachts.latest(utc(2026, 3, 30, 0, 0)) == utc(2026, 3, 29, 1, 30)


def test_cron_doppelte_stunde_der_winterzeit() -> None:
    """25. Oktober 2026: 03:00 → 02:00. Der Termin 02:30 läuft einmal (erste 02:30, 00:30 UTC)."""
    nachts = Cron("30 2 * * *")
    assert nachts.latest(utc(2026, 10, 25, 0, 45)) == utc(2026, 10, 25, 0, 30)
    assert nachts.latest(utc(2026, 10, 25, 1, 45)) == utc(2026, 10, 25, 0, 30), "zweite 02:45: kein neuer Termin"
    assert nachts.latest(utc(2026, 10, 25, 1, 30)) == utc(2026, 10, 25, 0, 30), "zweite 02:30 zählt nicht"


# -- Register --------------------------------------------------------------------------------


def test_registrieren_als_dekorator_gibt_den_auftrag_zurueck() -> None:
    register = ScheduleRegistry()
    assert every(minutes=15, registry=register)(T.merken) is T.merken
    cron("0 4 * * *", name="nachts", kwargs={"zusatz": 1}, catchup=Catchup.AUSLASSEN, registry=register)(T.merken)

    namen = [eintrag.name for eintrag in register]
    assert namen == ["apps.events.tests.auftraege.merken", "nachts"]
    nachts = register.get("nachts")
    assert nachts is not None
    assert (nachts.kwargs, nachts.catchup, nachts.trigger) == ({"zusatz": 1}, Catchup.AUSLASSEN, Cron("0 4 * * *"))


def test_gleicher_name_doppelt_registriert() -> None:
    register = ScheduleRegistry()
    every(minutes=15, registry=register)(T.merken)
    every(minutes=15, registry=register)(T.merken)  # erneuter Import desselben Moduls
    assert len(register) == 1
    with pytest.raises(ImproperlyConfigured):
        every(minutes=30, registry=register)(T.merken)


def test_nur_task_funktionen() -> None:
    with pytest.raises(ImproperlyConfigured):
        every(minutes=15, registry=ScheduleRegistry())(auftraege.keine_task)  # type: ignore[type-var]


def test_argumente_muessen_json_sein() -> None:
    with pytest.raises(ImproperlyConfigured):
        every(minutes=15, args=(object(),), registry=ScheduleRegistry())(T.merken)


def test_faelligkeit_nach_regel() -> None:
    register = ScheduleRegistry()
    every(minutes=15, name="nachholen", registry=register)(T.merken)
    every(minutes=15, name="auslassen", catchup=Catchup.AUSLASSEN, grace=timedelta(minutes=2), registry=register)(
        T.merken
    )
    termin = utc(2026, 9, 30, 10, 0)
    nachholen, auslassen = register.get("nachholen"), register.get("auslassen")
    assert nachholen is not None and auslassen is not None
    assert nachholen.is_due(termin, termin + timedelta(days=3))
    assert auslassen.is_due(termin, termin + timedelta(minutes=2))
    assert not auslassen.is_due(termin, termin + timedelta(minutes=3))
