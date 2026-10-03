# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wiederkehrende Arbeit als Zeitpläne im Worker statt in einem Faden im Webprozess (Issue #515).

Bis Issue #515 startete ``insight_sync`` beim Laden der Anwendung einen Faden je Webprozess, der
Sync-Protokolle aufräumte, verortete und Fraktionssitzungen erinnerte, einlud und erzeugte. Jetzt
sind das Zeitpläne (``schedules.py`` der Apps), die der Scheduler im Worker genau einmal je Termin
anlegt. Dazu die Aufräumregel für beendete Aufträge, ohne die ``events_task`` mit jedem Lauf wüchse.
"""

from __future__ import annotations

import ast
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from apps.events.models import Task, TaskStatus
from apps.events.schedule import Catchup, Cron, Every, autodiscover, registry
from apps.events.tasks_backend import KEEP_DONE, KEEP_FAILED, purge_finished

#: Zeitpläne, die den Faden ersetzen: Name → Abstand in Minuten (Standardwerte der Einstellungen)
ERSATZ_FUER_DEN_FADEN = {
    "insight_sync.schedules.haengende_syncs_bereinigen": 5,
    "insight_core.schedules.verortung_automatisch": 15,
    "apps.work.schedules.fraktionserinnerungen_senden": 15,
    "apps.work.schedules.fraktionseinladungen_senden": 15,
    "apps.work.schedules.fraktionssitzungen_erzeugen": 60,
}


def test_der_scheduler_findet_alle_laeufe_des_frueheren_fadens() -> None:
    autodiscover()
    for name, minuten in ERSATZ_FUER_DEN_FADEN.items():
        eintrag = registry.get(name)
        assert eintrag is not None, name
        assert eintrag.trigger == Every(timedelta(minutes=minuten)), name
        assert eintrag.catchup == Catchup.NACHHOLEN, "nach einem Ausfall des Workers einmal nachholen"
        assert eintrag.task.queue_name == "default", "der Worker der Compose-Vorlage bedient default"


@pytest.mark.parametrize(
    ("aufgabe", "ziel"),
    [
        ("insight_core.schedules.verortung_automatisch", "insight_core.schedules.run_auto_georef_pass"),
        ("apps.work.schedules.fraktionserinnerungen_senden", "apps.work.schedules.run_faction_reminder_pass"),
        ("apps.work.schedules.fraktionseinladungen_senden", "apps.work.schedules.run_faction_invitation_pass"),
        ("apps.work.schedules.fraktionssitzungen_erzeugen", "apps.work.schedules.run_faction_schedule_pass"),
    ],
)
def test_jeder_zeitplan_ruft_seinen_lauf(monkeypatch: pytest.MonkeyPatch, aufgabe: str, ziel: str) -> None:
    autodiscover()
    aufrufe: list[str] = []
    funktion = ziel.rsplit(".", 1)[1]

    def lauf(*args: object, **kwargs: object) -> dict[str, int]:
        aufrufe.append(funktion)
        return {}

    monkeypatch.setattr(ziel, lauf)
    eintrag = registry.get(aufgabe)
    assert eintrag is not None
    eintrag.task.call()
    assert aufrufe == [funktion]


@pytest.mark.django_db
def test_haengende_syncs_werden_vom_zeitplan_bereinigt() -> None:
    from insight_sync.models import SyncLog
    from insight_sync.schedules import haengende_syncs_bereinigen

    haengt = SyncLog.objects.create(sync_type=SyncLog.SyncType.INCREMENTAL, triggered_by="daemon")
    laeuft = SyncLog.objects.create(sync_type=SyncLog.SyncType.INCREMENTAL, triggered_by="daemon")
    SyncLog.objects.filter(pk=haengt.pk).update(started_at=timezone.now() - timedelta(minutes=16))

    assert haengende_syncs_bereinigen.call() == 1

    haengt.refresh_from_db()
    laeuft.refresh_from_db()
    assert haengt.status == "failed"
    assert haengt.finished_at is not None
    assert laeuft.status == "running"


def test_kein_faden_beim_laden_der_anwendung() -> None:
    """Der Webprozess startet keinen Hintergrund-Faden mehr (früher ``sync-watchdog``)."""
    namen = {faden.name for faden in threading.enumerate()}
    assert "sync-watchdog" not in namen
    from insight_sync import daemon

    assert not hasattr(daemon, "start")


# --- Fitnessfunktion (ADR Aufträge und Zeitpläne, „Prüfung“) -------------------------------------

#: Code, der Fäden starten darf: der Worker selbst und sein Metrik-Server. Alles andere läuft als
#: Auftrag oder Zeitplan im Worker.
ERLAUBT = (
    "apps/events/",
    "apps/common/metrics_server.py",
)
WURZEL = Path(__file__).resolve().parents[3]
PAKETE = ("apps", "hub", "insight_core", "insight_ai", "insight_search", "insight_sync", "mandari")


def _startet_faden(knoten: ast.AST) -> bool:
    if not isinstance(knoten, ast.Call):
        return False
    ziel = knoten.func
    name = ziel.attr if isinstance(ziel, ast.Attribute) else ziel.id if isinstance(ziel, ast.Name) else ""
    return name in ("Thread", "Timer")


def _fadenstarts(baum: ast.AST) -> list[int]:
    return [k.lineno for k in ast.walk(baum) if isinstance(k, ast.Call) and _startet_faden(k)]


def test_kein_code_ausserhalb_des_workers_startet_faeden() -> None:
    funde: list[str] = []
    for paket in PAKETE:
        for datei in (WURZEL / paket).rglob("*.py"):
            relativ = datei.relative_to(WURZEL).as_posix()
            if "/tests/" in relativ or "/migrations/" in relativ or relativ.startswith(ERLAUBT):
                continue
            baum = ast.parse(datei.read_text(encoding="utf-8"), filename=relativ)
            funde.extend(f"{relativ}:{zeile}" for zeile in _fadenstarts(baum))
    assert funde == [], "Hintergrundarbeit gehört als Auftrag oder Zeitplan in den Worker"


# --- Aufbewahrung beendeter Aufträge --------------------------------------------------------------


def _auftrag(status: str, alter: timedelta | None) -> Task:
    beendet = None if alter is None else timezone.now() - alter
    return Task.objects.create(queue="default", task_path="x.y", status=status, finished_at=beendet)


@pytest.mark.django_db
def test_beendete_auftraege_werden_nach_der_frist_geloescht() -> None:
    tag = timedelta(days=1)
    weg = [
        _auftrag(TaskStatus.ERLEDIGT, KEEP_DONE + tag),
        _auftrag(TaskStatus.FEHLGESCHLAGEN, KEEP_FAILED + tag),
        _auftrag(TaskStatus.TOT, KEEP_FAILED + tag),
    ]
    bleibt = [
        _auftrag(TaskStatus.ERLEDIGT, KEEP_DONE - tag),
        _auftrag(TaskStatus.TOT, KEEP_DONE + tag),
        _auftrag(TaskStatus.WARTEND, None),
        _auftrag(TaskStatus.LAEUFT, None),
    ]

    assert purge_finished(batch=1) == len(weg)

    assert set(Task.objects.values_list("pk", flat=True)) == {a.pk for a in bleibt}


@pytest.mark.django_db
def test_zeitplan_raeumt_auftraege_taeglich_auf() -> None:
    from apps.events import schedules

    eintrag = registry.get("apps.events.schedules.auftraege_aufraeumen")
    assert eintrag is not None
    assert eintrag.trigger == Cron("50 3 * * *")
    _auftrag(TaskStatus.ERLEDIGT, KEEP_DONE + timedelta(days=1))
    assert schedules.auftraege_aufraeumen.call() == 1
