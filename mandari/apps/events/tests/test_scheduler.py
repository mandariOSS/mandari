# SPDX-License-Identifier: AGPL-3.0-or-later
"""Leader-Rolle ``scheduler`` (Issue #507): genau ein Auftrag je Termin, Nachholen und Auslassen."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.db import OperationalError, connection
from django.utils import timezone

from apps.events import leases, schedule
from apps.events.models import Lease, ScheduleState, TaskStatus
from apps.events.models import Task as TaskRow
from apps.events.schedule import Catchup, Schedule, ScheduleRegistry, Trigger, cron, every
from apps.events.scheduler import LEASE_NAME, Scheduler
from apps.events.task_runner import run_pending
from apps.events.tests import auftraege
from apps.events.tests.hilfen import nur_postgres

T = cast(Any, auftraege)
PFAD = "apps.events.tests.auftraege"
BEGINN = datetime(2026, 9, 30, 10, 7, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _zuruecksetzen() -> None:
    auftraege.zuruecksetzen()


@pytest.fixture
def register() -> ScheduleRegistry:
    register = ScheduleRegistry()
    every(minutes=15, name="viertelstunde", args=("v",), registry=register)(T.merken)
    return register


def _planer(register: ScheduleRegistry, holder: str = "a") -> Scheduler:
    return Scheduler(registry=register, holder=holder)


def _auftraege() -> list[tuple[str, dict[str, Any]]]:
    return [(zeile.idempotency_key or "", zeile.args) for zeile in TaskRow.objects.order_by("created_at", "id")]


@pytest.mark.django_db
def test_neuer_zeitplan_beginnt_mit_dem_naechsten_termin(register: ScheduleRegistry) -> None:
    planer = _planer(register)
    assert planer.tick(BEGINN) == []
    assert not TaskRow.objects.exists(), "kein Lauf beim Deploy"
    assert ScheduleState.objects.get(name="viertelstunde").last_slot == datetime(2026, 9, 30, 10, 0, tzinfo=UTC)

    assert planer.tick(BEGINN + timedelta(minutes=5)) == []
    assert planer.tick(BEGINN + timedelta(minutes=8)) == ["viertelstunde"]
    zeile = TaskRow.objects.get()
    assert zeile.task_path == f"{PFAD}.merken"
    assert zeile.args == {"args": ["v"], "kwargs": {}}
    assert zeile.idempotency_key == f"{PFAD}.merken:zeitplan:viertelstunde:2026-09-30T10:15:00+00:00"
    stand = ScheduleState.objects.get(name="viertelstunde")
    assert stand.last_slot == datetime(2026, 9, 30, 10, 15, tzinfo=UTC)
    assert str(stand.last_task_id) == str(zeile.pk)


@pytest.mark.django_db
def test_je_termin_genau_ein_auftrag(register: ScheduleRegistry) -> None:
    planer = _planer(register)
    planer.tick(BEGINN)
    for minuten in (8, 9, 10, 20, 22):
        planer.tick(BEGINN + timedelta(minutes=minuten))
    assert TaskRow.objects.count() == 1
    planer.tick(BEGINN + timedelta(minutes=23))
    assert TaskRow.objects.count() == 2, "10:30"


@pytest.mark.django_db
def test_verpasste_termine_werden_einmal_nachgeholt(register: ScheduleRegistry) -> None:
    planer = _planer(register)
    planer.tick(BEGINN)
    assert planer.tick(BEGINN + timedelta(days=3)) == ["viertelstunde"]
    assert TaskRow.objects.count() == 1, "ein Auftrag für den jüngsten Termin, nicht einer je Termin"
    assert "2026-10-03T10:00:00" in (TaskRow.objects.get().idempotency_key or "")


@pytest.mark.django_db
def test_verpasste_termine_werden_ausgelassen() -> None:
    register = ScheduleRegistry()
    cron("0 8 * * *", name="morgens", catchup=Catchup.AUSLASSEN, registry=register)(T.merken)
    planer = _planer(register)
    planer.tick(datetime(2026, 9, 30, 5, 0, tzinfo=UTC))

    # 08:00 Berlin = 06:00 UTC; der Scheduler lief erst um 07:00 UTC wieder
    assert planer.tick(datetime(2026, 9, 30, 7, 0, tzinfo=UTC)) == []
    assert not TaskRow.objects.exists()
    assert ScheduleState.objects.get(name="morgens").last_slot == datetime(2026, 9, 30, 6, 0, tzinfo=UTC)
    # innerhalb der Toleranz (5 Minuten) läuft er
    assert planer.tick(datetime(2026, 10, 1, 6, 4, tzinfo=UTC)) == ["morgens"]


@pytest.mark.django_db
def test_nur_der_inhaber_der_lease_plant(register: ScheduleRegistry) -> None:
    erster, zweiter = _planer(register, "a"), _planer(register, "b")
    erster.tick(BEGINN)
    assert zweiter.tick(BEGINN + timedelta(minutes=8)) == []
    assert not zweiter.is_leader
    assert erster.tick(BEGINN + timedelta(minutes=8)) == ["viertelstunde"]


@pytest.mark.django_db
def test_zwei_worker_ein_auftrag_je_termin_auch_bei_uebernahme(register: ScheduleRegistry) -> None:
    """ADR A4: Ein Zeitplan läuft mit zwei Workern genau einmal – auch wenn die Lease wechselt."""
    erster, zweiter = _planer(register, "a"), _planer(register, "b")
    erster.tick(BEGINN)
    for schritt in range(1, 9):
        jetzt = BEGINN + timedelta(minutes=8 * schritt)
        reihenfolge = (erster, zweiter)
        if schritt % 3 == 0:  # Inhaber fällt aus: Lease läuft ab, der andere übernimmt
            Lease.objects.filter(name=LEASE_NAME).update(expires_at=timezone.now() - timedelta(seconds=1))
            erster.is_leader = zweiter.is_leader = False
            reihenfolge = (zweiter, erster) if schritt % 2 else (erster, zweiter)
        for planer in reihenfolge:
            planer.tick(jetzt)
            planer.tick(jetzt)

    schluessel = [schluessel for schluessel, _ in _auftraege()]
    assert len(schluessel) == len(set(schluessel))
    assert len(schluessel) == 4, "10:15, 10:30, 10:45, 11:00 bis 11:11"


@pytest.mark.django_db
def test_wer_die_lease_verloren_hat_legt_nichts_mehr_an(register: ScheduleRegistry) -> None:
    alt, neu = _planer(register, "a"), _planer(register, "b")
    alt.tick(BEGINN)
    Lease.objects.filter(name=LEASE_NAME).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert leases.acquire(LEASE_NAME, neu.holder)

    # "a" hält sich noch für den Leader (Erneuerung nicht fällig), die Abgrenzung verhindert den Auftrag
    assert alt.is_leader
    assert alt.tick(BEGINN + timedelta(minutes=8)) == []
    assert not alt.is_leader
    assert not TaskRow.objects.exists()


@pytest.mark.django_db
def test_geplanter_auftrag_wird_vom_runner_ausgefuehrt(register: ScheduleRegistry) -> None:
    planer = _planer(register)
    planer.tick(BEGINN)
    planer.tick(BEGINN + timedelta(minutes=8))
    assert run_pending() == 1
    assert auftraege.aufrufe == [("merken", ("v", 0))]
    assert TaskRow.objects.get().status == TaskStatus.ERLEDIGT


@pytest.mark.django_db
def test_schluessel_verhindert_doppelten_auftrag_auch_ohne_stand(register: ScheduleRegistry) -> None:
    """Wird der Stand von Hand gelöscht, entsteht für denselben Termin kein zweiter Auftrag."""
    planer = _planer(register)
    planer.tick(BEGINN)
    planer.tick(BEGINN + timedelta(minutes=8))
    ScheduleState.objects.update(last_slot=BEGINN - timedelta(hours=1))
    planer.tick(BEGINN + timedelta(minutes=9))
    assert TaskRow.objects.count() == 1


class _Kaputt(Trigger):
    """Terminregel, die bei jeder Berechnung scheitert."""

    def latest(self, now: datetime) -> datetime:
        raise OverflowError("date value out of range")

    def describe(self) -> str:
        return "kaputt"


@pytest.mark.django_db
def test_fehlerhafter_zeitplan_haelt_die_uebrigen_nicht_auf(register: ScheduleRegistry) -> None:
    """Scheitert ein Zeitplan (Regel oder Einreihen), planen die übrigen weiter; der Prozess läuft weiter."""
    # Namen vor "viertelstunde": Sie kommen im Durchlauf zuerst an die Reihe
    register.add(Schedule(name="a-kaputte-regel", task=T.merken, trigger=_Kaputt()))
    every(minutes=15, name="b-einreihen-scheitert", registry=register)(T.wichtig)
    planer = _planer(register)
    planer.tick(BEGINN)
    einreihen = planer.backend.enqueue_once

    def scheitert_fuer_wichtig(task: Any, *args: Any) -> Any:
        if task.module_path == f"{PFAD}.wichtig":
            raise TypeError("nicht einreihbar")
        return einreihen(task, *args)

    with mock.patch.object(planer.backend, "enqueue_once", side_effect=scheitert_fuer_wichtig):
        assert planer.tick(BEGINN + timedelta(minutes=8)) == ["viertelstunde"]
    assert planer.is_leader
    zeile = TaskRow.objects.get()
    assert "zeitplan:viertelstunde:" in (zeile.idempotency_key or "")
    stand = ScheduleState.objects.get(name="b-einreihen-scheitert")
    assert stand.last_slot == datetime(2026, 9, 30, 10, 0, tzinfo=UTC), "zurückgerollt, beim nächsten Mal erneut"

    assert planer.tick(BEGINN + timedelta(minutes=9)) == ["b-einreihen-scheitert"]


@pytest.mark.django_db
def test_datenbankfehler_bricht_den_durchlauf_ab(register: ScheduleRegistry) -> None:
    """Datenbankfehler gehen an ``run``, das die Verbindung neu aufbaut."""
    planer = _planer(register)
    planer.tick(BEGINN)
    with (
        mock.patch.object(Scheduler, "_plan", side_effect=OperationalError("Verbindung weg")),
        pytest.raises(OperationalError),
    ):
        planer.tick(BEGINN + timedelta(minutes=8))


@pytest.mark.django_db
def test_auftraege_landen_im_journal_auch_vor_dem_umschalten(register: ScheduleRegistry) -> None:
    """Die Webprozesse nutzen noch das sofort ausführende Backend; der Scheduler führt nichts selbst aus."""
    planer = _planer(register)
    planer.tick(BEGINN)
    planer.tick(BEGINN + timedelta(minutes=8))
    assert auftraege.aufrufe == []
    assert TaskRow.objects.get().status == TaskStatus.WARTEND


@pytest.mark.django_db
def test_dauerbetrieb_gibt_die_lease_frei(register: ScheduleRegistry) -> None:
    planer = _planer(register)
    stop = threading.Event()
    stop.set()
    planer.ensure_lease()
    planer.run(stop, interval=0.01)
    assert not Lease.objects.filter(name=LEASE_NAME).exists()


# -- Befehl -----------------------------------------------------------------------------------------


@pytest.fixture
def global_registriert() -> Iterator[None]:
    every(minutes=15, name="test.befehl", registry=schedule.registry)(T.merken)
    yield
    schedule.registry.remove("test.befehl")


@pytest.mark.django_db
def test_befehl_once_und_liste(global_registriert: None, capsys: pytest.CaptureFixture[str]) -> None:
    call_command("events_scheduler", "--once")
    assert ScheduleState.objects.filter(name="test.befehl").exists()
    assert not Lease.objects.filter(name=LEASE_NAME).exists(), "Lease nach --once freigegeben"

    call_command("events_scheduler", "--list")
    ausgabe = capsys.readouterr().out
    assert "test.befehl" in ausgabe
    assert "alle 0:15:00" in ausgabe


@pytest.mark.django_db
def test_befehl_once_bricht_ab_wenn_ein_anderer_die_lease_haelt(global_registriert: None) -> None:
    assert leases.acquire(LEASE_NAME, "fremd")
    with pytest.raises(CommandError):
        call_command("events_scheduler", "--once")


# -- Nur PostgreSQL: gleichzeitig -------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_gleichzeitige_scheduler_ein_auftrag_je_termin(register: ScheduleRegistry) -> None:
    """Zwei Prozesse planen gleichzeitig dieselben Termine; die Lease wechselt zwischendurch."""
    nur_postgres()
    _planer(register, "vorlauf").tick(BEGINN)
    leases.release(LEASE_NAME, "vorlauf")
    termine = [BEGINN + timedelta(minutes=4 * schritt) for schritt in range(1, 16)]
    fehler: list[BaseException] = []

    def planen(holder: str) -> None:
        planer = _planer(register, holder)
        try:
            for nummer, jetzt in enumerate(termine):
                if nummer % 5 == 4:
                    Lease.objects.filter(name=LEASE_NAME).update(expires_at=timezone.now() - timedelta(seconds=1))
                    planer.is_leader = False
                planer.tick(jetzt)
        except BaseException as exc:  # noqa: BLE001 – im Test an den Hauptthread melden
            fehler.append(exc)
        finally:
            connection.close()

    faeden = [threading.Thread(target=planen, args=(name,)) for name in ("a", "b")]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join(60)

    assert not fehler
    schluessel = [schluessel for schluessel, _ in _auftraege()]
    assert len(schluessel) == len(set(schluessel)), "kein Termin doppelt"
