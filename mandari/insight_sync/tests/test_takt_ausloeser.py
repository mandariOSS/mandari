# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Jetzt synchronisieren“ mit Takt je Quelle (Issue #1026): für alle Quellen oder eine, Lebenszeichen des Daemons.

Der Ingestor gleicht jede Quelle in ihrem eigenen Takt ab, nachts und am Wochenende seltener. Ob er lebt, zeigt sein
Lebenszeichen in Redis; Auslöser gehen über den Redis-Kanal ``mandari:sync:trigger`` an ihn, auch für eine einzelne
Quelle. Redis ist hier nachgebildet.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.events.models import Task
from insight_core.admin import request_source_sync
from insight_core.models import OParlSource
from insight_sync import daemon
from insight_sync.models import SyncLog


class _Redis:
    def __init__(self, empfaenger: int = 1, lebenszeichen: bool = False) -> None:
        self.empfaenger = empfaenger
        self.lebenszeichen = lebenszeichen
        self.nachrichten: list[tuple[str, dict[str, Any]]] = []

    def publish(self, kanal: str, nachricht: str) -> int:
        self.nachrichten.append((kanal, json.loads(nachricht)))
        return self.empfaenger

    def exists(self, schluessel: str) -> int:
        return int(self.lebenszeichen and schluessel == daemon.HEARTBEAT_KEY)


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> _Redis:
    nachbildung = _Redis()
    monkeypatch.setattr(daemon, "_redis", lambda: nachbildung)
    return nachbildung


@pytest.fixture
def quelle(db: Any) -> OParlSource:
    return OParlSource.objects.create(name="Beispiel-RIS", url="https://ris.example.org/oparl/system")


def test_ausloeser_fuer_eine_quelle(redis: _Redis, quelle: OParlSource) -> None:
    assert daemon.trigger_sync(full=True, source=quelle)

    kanal, nachricht = redis.nachrichten[0]
    assert kanal == "mandari:sync:trigger"
    assert nachricht["full"] is True
    assert nachricht["source_id"] == str(quelle.pk)
    assert nachricht["source_url"] == quelle.url


def test_ausloeser_fuer_alle_ohne_quelle(redis: _Redis) -> None:
    assert daemon.trigger_sync()
    assert "source_id" not in redis.nachrichten[0][1]


def test_ausloeser_ohne_empfaenger_gilt_als_nicht_zugestellt(redis: _Redis) -> None:
    redis.empfaenger = 0
    assert not daemon.trigger_sync()


@pytest.mark.django_db
def test_lebenszeichen_statt_protokoll_der_letzten_30_minuten(redis: _Redis) -> None:
    assert not daemon.is_ingestor_active(), "kein Lebenszeichen, kein Protokoll"
    redis.lebenszeichen = True
    assert daemon.is_ingestor_active(), "nachts läuft eine Quelle nur alle 1 bis 2 Stunden"


@pytest.mark.django_db
def test_ohne_redis_entscheidet_das_protokoll(monkeypatch: pytest.MonkeyPatch) -> None:
    def kaputt() -> Any:
        raise ConnectionError("Redis nicht erreichbar")

    monkeypatch.setattr(daemon, "_redis", kaputt)
    assert not daemon.is_ingestor_active()
    SyncLog.objects.create(sync_type="incremental", triggered_by="daemon", started_at=timezone.now())
    assert daemon.is_ingestor_active()
    assert not daemon.trigger_sync()


def test_quelle_ueber_den_daemon_wenn_er_lebt(redis: _Redis, quelle: OParlSource) -> None:
    redis.lebenszeichen = True

    assert request_source_sync(quelle, full=False) == "daemon"
    assert redis.nachrichten[0][1]["source_id"] == str(quelle.pk)
    assert not Task.objects.exists(), "kein zweiter Lauf im Worker"


def test_quelle_im_worker_ohne_daemon_oder_wenn_abgeschaltet(redis: _Redis, quelle: OParlSource) -> None:
    assert request_source_sync(quelle, full=True) == "worker"
    assert Task.objects.count() == 1

    redis.lebenszeichen = True
    quelle.is_active = False
    quelle.save(update_fields=["is_active"])
    assert request_source_sync(quelle) == "worker", "abgeschaltete Quellen gleicht der Daemon nicht ab"
    assert redis.nachrichten == []


def test_jetzt_synchronisieren_fuer_eine_quelle(redis: _Redis, quelle: OParlSource, admin_client: Client) -> None:
    antwort = admin_client.post("/admin/insight_sync/trigger-sync/", {"source_id": str(quelle.pk)})

    assert antwort.status_code == 302
    assert antwort["Location"].endswith(f"/oparlsource/{quelle.pk}/change/")
    assert redis.nachrichten[0][1]["source_id"] == str(quelle.pk)


def test_jetzt_synchronisieren_unbekannte_quelle(redis: _Redis, db: Any, admin_client: Client) -> None:
    for kennung in ("00000000-0000-0000-0000-000000000000", "keine-uuid"):
        antwort = admin_client.post("/admin/insight_sync/trigger-sync/", {"source_id": kennung})
        assert antwort.status_code == 302
    assert redis.nachrichten == []


def _protokoll(quelle: OParlSource | None, status: str, vor_minuten: int) -> SyncLog:
    eintrag = SyncLog.objects.create(source=quelle, sync_type="incremental", triggered_by="daemon", status=status)
    # started_at setzt Django beim Anlegen selbst
    SyncLog.objects.filter(pk=eintrag.pk).update(started_at=timezone.now() - timedelta(minutes=vor_minuten))
    return eintrag


def _pruefung(name: str) -> dict[str, Any]:
    from insight_core.services.source_health import _ingestor_checks

    return next(c for c in _ingestor_checks() if c["name"] == name)


def test_monitor_daemon_aktiv_mit_lebenszeichen_und_letztem_lauf(redis: _Redis, quelle: OParlSource) -> None:
    redis.lebenszeichen = True
    _protokoll(quelle, "success", 90)

    pruefung = _pruefung("Ingestor-Daemon")
    assert pruefung["status"] == "ok"
    assert pruefung["detail"] == "Daemon aktiv (Lebenszeichen), letzter Lauf vor 90 Minuten"

    _protokoll(quelle, "success", 5 * 60)
    SyncLog.objects.filter(started_at__gte=timezone.now() - timedelta(hours=2)).delete()
    assert _pruefung("Ingestor-Daemon")["detail"].endswith("letzter Lauf vor 5 Stunden")


def test_monitor_daemon_ohne_lebenszeichen(redis: _Redis, quelle: OParlSource) -> None:
    _protokoll(quelle, "success", 10)
    assert _pruefung("Ingestor-Daemon")["detail"] == "Daemon aktiv (Sync-Protokoll), letzter Lauf vor 10 Minuten"

    SyncLog.objects.all().delete()
    _protokoll(quelle, "success", 7 * 60)
    pruefung = _pruefung("Ingestor-Daemon")
    assert pruefung["status"] == "critical"
    assert pruefung["detail"] == "Kein Lebenszeichen, letzter Lauf vor 7 Stunden"


def test_monitor_laeufe_je_quelle_eine_gestoerte_quelle_haelt_nicht_auf_warnung(
    redis: _Redis, quelle: OParlSource
) -> None:
    hamm = OParlSource.objects.create(name="Hamm", url="https://hamm.example.org/oparl/system")
    for minuten in (10, 70, 130):
        _protokoll(quelle, "success", minuten)
        _protokoll(hamm, "failed", minuten)

    pruefung = _pruefung("Sync-Läufe (24 h)")
    assert pruefung["status"] == "ok"
    assert pruefung["detail"] == "3 von 6 Läufen fehlgeschlagen; ohne Erfolg: Hamm (Bewertung je Quelle)"

    SyncLog.objects.filter(source=quelle).update(status="failed")
    pruefung = _pruefung("Sync-Läufe (24 h)")
    assert pruefung["status"] == "critical"
    assert pruefung["detail"] == "Alle 6 Läufe fehlgeschlagen"

    SyncLog.objects.all().delete()
    assert _pruefung("Sync-Läufe (24 h)")["status"] == "warning"
