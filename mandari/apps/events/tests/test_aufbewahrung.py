# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufräumen des Journals und der Aufträge nach Frist (``apps.events.aufbewahrung``, ``events_purge``, Issue #511).

Gelöscht wird nur, was niemand mehr braucht: nichts über dem kleinsten Cursor (auch pausiert und im
Schattenbetrieb), nie das neueste Ereignis, keine geparkten und keine unnummerierten Zeilen, nichts, was
gültige Cursor des Änderungsfeeds noch erreichen.
"""

from __future__ import annotations

import itertools
import time as zeitmodul
import uuid
from datetime import UTC, datetime, time, timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.events import aufbewahrung, pruning, wiederherstellung
from apps.events.models import Event, ParkedEvent, ParkedState, Subscription, SubscriptionState, Task, TaskStatus
from apps.events.schedule import Cron, ScheduleRegistry
from apps.events.tasks_backend import count_finished, purge_finished
from apps.events.tests.hilfen import ereignis_anlegen, nummeriert

pytestmark = pytest.mark.django_db

JETZT = datetime(2026, 10, 5, 9, 30, tzinfo=UTC)


def _ereignis(alter_tage: float, bezug: datetime = JETZT, **abweichend: Any) -> Event:
    ereignis = nummeriert(**abweichend)
    Event.objects.filter(pk=ereignis.pk).update(recorded_at=bezug - timedelta(days=alter_tage))
    ereignis.refresh_from_db()
    return ereignis


def _seqs() -> list[int]:
    """Folgenummern im Journal, aufsteigend; 0 steht für eine Zeile ohne Folgenummer."""
    return sorted(seq or 0 for seq in Event.objects.values_list("seq", flat=True))


def _abonnement(name: str, cursor: int, state: str = SubscriptionState.AKTIV) -> None:
    Subscription.objects.create(name=name, cursor_seq=cursor, state=state)


def test_stichtag_ist_tagesbeginn_und_nie_vor_der_gueltigkeit_der_feed_cursor(settings: Any) -> None:
    settings.EVENTS_JOURNAL_RETENTION_DAYS = 90
    settings.OPARL_CHANGES_RETENTION_DAYS = 90
    assert aufbewahrung.cutoff(JETZT) == datetime(2026, 7, 7, tzinfo=UTC)
    # Ein Cursor vom ältesten noch gültigen Ausgabetag sieht kein Aufräumen nach seiner Ausgabe
    aeltester_gueltiger_tag = (JETZT - timedelta(days=90)).date()
    assert aufbewahrung.cutoff(JETZT) <= datetime.combine(aeltester_gueltiger_tag, time.min, tzinfo=UTC)

    settings.EVENTS_JOURNAL_RETENTION_DAYS = 30  # kürzer als die Zusage des Feeds: der Feed gewinnt
    assert aufbewahrung.retention_days() == 90
    settings.EVENTS_JOURNAL_RETENTION_DAYS = 120
    assert aufbewahrung.retention_days() == 120
    settings.EVENTS_JOURNAL_RETENTION_DAYS = 0
    with pytest.raises(ImproperlyConfigured, match="mindestens 1"):
        aufbewahrung.retention_days()


def test_loescht_nach_frist_und_haelt_das_aufraeumen_fest() -> None:
    alt = [_ereignis(120), _ereignis(100), _ereignis(95)]
    jung = [_ereignis(80), _ereignis(1)]
    _abonnement("test.a", jung[-1].seq or 0)

    plan = aufbewahrung.plan(JETZT)
    assert (plan.boundary, plan.limited_by) == (jung[0].seq, aufbewahrung.FRIST)
    assert aufbewahrung.count(plan) == 3
    assert aufbewahrung.purge(plan, batch=2).deleted == 3

    assert _seqs() == [e.seq for e in jung]
    stand = pruning.horizon()
    assert stand is not None
    assert stand.through_seq == alt[-1].seq and stand.recorded_before == aufbewahrung.cutoff(JETZT)
    assert pruning.pruned_through(alt[0].seq or 0) == alt[-1].seq
    # Ein Eintrag je Lauf, nicht je Stapel; ein zweiter Lauf findet nichts
    assert aufbewahrung.purge(aufbewahrung.plan(JETZT)).deleted == 0
    assert pruning.horizon() == stand


@pytest.mark.parametrize("zustand", [SubscriptionState.AKTIV, SubscriptionState.PAUSIERT, SubscriptionState.SCHATTEN])
def test_nie_ueber_den_kleinsten_cursor(zustand: str) -> None:
    ereignisse = [_ereignis(200 - i) for i in range(5)]
    _abonnement("test.vorn", ereignisse[-1].seq or 0)
    _abonnement("test.zurueck", ereignisse[1].seq or 0, zustand)

    plan = aufbewahrung.plan(JETZT)
    assert (plan.boundary, plan.limited_by, plan.subscription) == (
        ereignisse[2].seq,
        aufbewahrung.ABONNEMENT,
        "test.zurueck",
    )
    aufbewahrung.purge(plan)
    assert _seqs() == [e.seq for e in ereignisse[2:]]


def test_cursor_wird_vor_jedem_stapel_neu_gelesen() -> None:
    """Nachspielen während des Aufräumens: Was das zurückgesetzte Abonnement noch nicht hat, bleibt."""
    ereignisse = [_ereignis(200 - i) for i in range(6)]
    _abonnement("test.a", ereignisse[-1].seq or 0)
    plan = aufbewahrung.plan(JETZT)
    assert plan.boundary == ereignisse[-1].seq  # das neueste bleibt
    Subscription.objects.filter(name="test.a").update(cursor_seq=ereignisse[2].seq)

    aufbewahrung.purge(plan, batch=1)
    assert _seqs() == [e.seq for e in ereignisse[3:]]


def test_neuestes_ereignis_bleibt_und_die_wiederherstellungspruefung_passt() -> None:
    ereignisse = [_ereignis(300), _ereignis(200)]
    _abonnement("test.a", ereignisse[-1].seq or 0)

    plan = aufbewahrung.plan(JETZT)
    assert plan.limited_by == aufbewahrung.ENDE
    aufbewahrung.purge(plan)
    assert _seqs() == [ereignisse[-1].seq]
    # Kein Cursor hinter dem Ende des Journals (die Prüfung nach einer Wiederherstellung schlüge sonst an)
    assert all(s.cursor_seq <= max(_seqs()) for s in Subscription.objects.all())
    assert (
        wiederherstellung.RestoreState(
            available=True,
            head_seq=max(_seqs()),
            subscriptions=[
                wiederherstellung.SubscriptionState(name=s.name, state=s.state, cursor=s.cursor_seq, external=None)
                for s in Subscription.objects.all()
            ],
        ).cursor_ahead
        == []
    )


def test_geparkte_und_unnummerierte_zeilen_bleiben() -> None:
    ereignisse = [_ereignis(200 - i) for i in range(4)]
    _abonnement("test.a", ereignisse[-1].seq or 0)
    ParkedEvent.objects.create(
        subscription="test.a",
        event_seq=ereignisse[1].seq or 0,
        aggregate_id=ereignisse[1].aggregate_id,
        state=ParkedState.TOT,
        attempts=8,
    )
    ohne_nummer = ereignis_anlegen()
    Event.objects.filter(pk=ohne_nummer.pk).update(recorded_at=JETZT - timedelta(days=365))

    aufbewahrung.purge(aufbewahrung.plan(JETZT))
    assert _seqs() == [0, ereignisse[1].seq, ereignisse[3].seq]  # 0: ohne Folgenummer


def test_leeres_journal_und_ohne_abonnements() -> None:
    plan = aufbewahrung.plan(JETZT)
    assert (plan.boundary, plan.limited_by) == (0, aufbewahrung.LEER)
    assert aufbewahrung.purge(plan).deleted == 0
    assert pruning.horizon() is None

    alt = [_ereignis(200), _ereignis(150)]  # ohne Abonnement: nur Frist und Ende
    aufbewahrung.purge(aufbewahrung.plan(JETZT))
    assert _seqs() == [alt[-1].seq]


def test_zeitgrenze_beendet_den_lauf_vorzeitig(monkeypatch: pytest.MonkeyPatch) -> None:
    [_ereignis(200 - i) for i in range(5)]
    uhr = itertools.chain([0.0, 0.0], itertools.count(5.0, 5.0))  # Ende = 4 s; zweite Prüfung bei 5 s
    monkeypatch.setattr(zeitmodul, "monotonic", lambda: next(uhr))

    ergebnis = aufbewahrung.purge(aufbewahrung.plan(JETZT), batch=1, max_seconds=4)
    assert ergebnis == aufbewahrung.PurgeResult(deleted=1, stopped_early=True)


# --- Befehl, Zeitplan, Aufträge --------------------------------------------------------------------


def test_probelauf_loescht_nichts() -> None:
    jetzt = timezone.now()
    [_ereignis(200, jetzt), _ereignis(150, jetzt), _ereignis(1, jetzt)]
    Task.objects.create(queue="default", task_path="x.y", status=TaskStatus.ERLEDIGT, finished_at=timezone.now())

    ausgabe = StringIO()
    call_command("events_purge", "--dry-run", stdout=ausgabe)
    text = ausgabe.getvalue()
    assert "Probelauf: 2 Zeilen des Journals" in text and "Grenze: Frist" in text
    assert "Probelauf: 0 beendete Aufträge" in text
    assert len(_seqs()) == 3 and pruning.horizon() is None

    ausgabe = StringIO()
    call_command("events_purge", "--nur", "journal", "--batch", "1", stdout=ausgabe)
    assert "2 Zeilen des Journals gelöscht" in ausgabe.getvalue() and "Aufträge" not in ausgabe.getvalue()
    assert len(_seqs()) == 1


def test_befehl_prueft_seine_angaben() -> None:
    with pytest.raises(CommandError, match="--batch"):
        call_command("events_purge", "--batch", "0")
    with pytest.raises(CommandError, match="nicht negativ"):
        call_command("events_purge", "--pause", "-1")


def test_befehl_mit_ungueltiger_frist(settings: Any) -> None:
    settings.EVENTS_TASKS_DONE_RETENTION_DAYS = 0
    with pytest.raises(CommandError, match="Fristen ungültig"):
        call_command("events_purge", "--nur", "auftraege")


def test_fristen_der_auftraege_aus_den_einstellungen(settings: Any) -> None:
    def auftrag(status: str, alter_tage: int) -> Task:
        return Task.objects.create(
            queue="default",
            task_path="x.y",
            status=status,
            finished_at=timezone.now() - timedelta(days=alter_tage),
        )

    settings.EVENTS_TASKS_DONE_RETENTION_DAYS = 3
    settings.EVENTS_TASKS_DEAD_RETENTION_DAYS = 30
    weg = {
        auftrag(TaskStatus.ERLEDIGT, 4).pk,
        auftrag(TaskStatus.TOT, 31).pk,
        auftrag(TaskStatus.FEHLGESCHLAGEN, 31).pk,
    }
    bleibt = {auftrag(TaskStatus.ERLEDIGT, 2).pk, auftrag(TaskStatus.TOT, 29).pk}

    assert count_finished() == 3
    assert purge_finished(batch=1) == 3
    assert set(Task.objects.values_list("pk", flat=True)) == bleibt
    assert not Task.objects.filter(pk__in=weg).exists()


def test_zeitplan_nur_mit_schalter(settings: Any) -> None:
    from apps.common.einmalig import EinmaligMixin
    from apps.events import schedules
    from apps.events.management.commands.events_purge import Command

    settings.EVENTS_JOURNAL_PURGE_ENABLED = False
    aus = ScheduleRegistry()
    schedules.registrieren(ziel=aus)
    assert aus.get("befehl:events_purge") is None

    settings.EVENTS_JOURNAL_PURGE_ENABLED = True
    an = ScheduleRegistry()
    schedules.registrieren(ziel=an)
    eintrag = an.get("befehl:events_purge")
    assert eintrag is not None and eintrag.trigger == Cron("10 4 * * *")
    befehl, argumente, zeitgrenze = eintrag.args
    assert (befehl, argumente[:2]) == ("events_purge", ["--nur", "journal"])
    # Der Befehl hört vor der Zeitgrenze des Prozesses auf; Sperre und Übergabe wie alle Zeitpläne
    assert float(argumente[argumente.index("--max-seconds") + 1]) < zeitgrenze
    assert issubclass(Command, EinmaligMixin)


def test_unbekanntes_objekt_haelt_nichts_auf() -> None:
    """Geparkte Zeilen eines anderen Abonnements bleiben ebenfalls (die Wiederholung liest sie)."""
    ereignisse = [_ereignis(200 - i, aggregate_id=uuid.uuid4()) for i in range(3)]
    ParkedEvent.objects.create(
        subscription="test.anderes",
        event_seq=ereignisse[0].seq or 0,
        aggregate_id=ereignisse[0].aggregate_id,
        state=ParkedState.WIEDERHOLEN,
    )
    aufbewahrung.purge(aufbewahrung.plan(JETZT))
    assert _seqs() == [ereignisse[0].seq, ereignisse[2].seq]
