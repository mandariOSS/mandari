# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Statusprüfungen des Workers für die Statusseite (Issue #574).

- ``/health/worker/``: Lebenszeichen, Rückstau, Fehlerquote, gescheiterte Arbeit; 503, sobald eine
  Prüfung scheitert (daraus alarmiert die Statusseite per Mail); nur Zahlen, keine Namen.
- „Worker lebt“: Der Worker mit dem Scheduler meldet sich selbst an die Statusseite; bleibt die
  Meldung aus, alarmiert sie.
- Deploy-Prüfung: Nach dem Umschalten muss ein Worker alle nötigen Rollen bedienen.
"""

from __future__ import annotations

import threading
import time
import urllib.request
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.events import presence, status
from apps.events.models import ParkedEvent, ParkedState, Task, TaskStatus
from apps.events.push import Push, push_from_settings
from apps.events.tests.test_worker import Schleife, _stoppen_wenn, _worker

VERIFY = Path(__file__).resolve().parents[4] / "deploy" / "scripts" / "verify_deploy.py"


@pytest.fixture(autouse=True)
def _ohne_cache() -> None:
    cache.clear()


@pytest.fixture
def mit_bedarf(settings: Any) -> Any:
    settings.EVENTS_WORKER_REQUIRED = "true"
    return settings


def _auftrag(status_: str, *, vor: timedelta = timedelta(minutes=5), queue: str = "default") -> Task:
    beendet = None if status_ in (TaskStatus.WARTEND, TaskStatus.LAEUFT) else timezone.now() - vor
    return Task.objects.create(queue=queue, task_path="x.y", status=status_, finished_at=beendet)


def _alle_worker() -> None:
    presence.announce("w1", sorted(presence.ALL_ROLES), [])


# --- Prüfungen ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_alles_in_ordnung(client: Client, mit_bedarf: Any) -> None:
    _alle_worker()
    for _ in range(3):
        _auftrag(TaskStatus.ERLEDIGT)

    antwort = client.get("/health/worker/")

    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["status"] == "ok"
    assert set(daten["checks"]) == {"lebenszeichen", "rueckstau", "fehlerquote", "gescheitert", "texterkennung"}
    assert all(c["ok"] for c in daten["checks"].values()), daten
    assert daten["checks"]["lebenszeichen"]["detail"] == "1 Worker (dispatch, scheduler, sequencer, tasks)"
    assert daten["checks"]["fehlerquote"]["detail"] == "0 von 3 Aufträgen der letzten Stunde gescheitert (0%)"
    assert antwort["Cache-Control"].startswith("max-age=0")


@pytest.mark.django_db
def test_ohne_worker_503(client: Client, mit_bedarf: Any) -> None:
    antwort = client.get("/health/worker/")

    assert antwort.status_code == 503
    daten = antwort.json()
    assert daten["status"] == "error"
    assert daten["checks"]["lebenszeichen"] == {
        "ok": False,
        "detail": "kein Worker für dispatch, scheduler, sequencer, tasks",
    }


@pytest.mark.django_db
def test_ohne_bedarf_ist_das_lebenszeichen_in_ordnung(settings: Any) -> None:
    settings.EVENTS_WORKER_REQUIRED = "false"
    assert status.check_heartbeat() == status.Check(True, "nicht erforderlich")


@pytest.mark.django_db
def test_rueckstau_der_auftraege(mit_bedarf: Any) -> None:
    assert status.check_backlog().ok
    alt = _auftrag(TaskStatus.WARTEND)
    Task.objects.filter(pk=alt.pk).update(run_after=timezone.now() - timedelta(seconds=status.MAX_TASK_WAIT + 60))

    ergebnis = status.check_backlog()

    assert not ergebnis.ok
    assert "Aufträge 960 s" in ergebnis.detail


@pytest.mark.django_db
def test_rueckstau_der_zustellung_ausser_pausiert(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.events import metrics

    lags = [metrics.SubscriptionLag("a", "aktiv", 1, status.MAX_EVENT_LAG + 1)]
    monkeypatch.setattr(metrics, "subscription_lags", lambda: lags)
    assert not status.check_backlog().ok
    lags[0] = metrics.SubscriptionLag("a", "pausiert", 1, 9999.0)
    assert status.check_backlog().ok, "ein pausiertes Abonnement wächst bewusst"


@pytest.mark.django_db
def test_rueckstau_des_sequenzierers(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.events import metrics

    monkeypatch.setattr(metrics, "sequencer_backlog", lambda: metrics.SequencerBacklog(400.0, None, 0.0))
    ergebnis = status.check_backlog()
    assert not ergebnis.ok
    assert ergebnis.detail.startswith("Sequenzierer 400 s")


@pytest.mark.django_db
def test_fehlerquote_erst_ab_mindestzahl() -> None:
    for _ in range(3):
        _auftrag(TaskStatus.FEHLGESCHLAGEN)
    assert status.check_error_rate().ok, "unter MIN_FINISHED zählt keine Quote"

    for _ in range(2):
        _auftrag(TaskStatus.ERLEDIGT)
    _auftrag(TaskStatus.ERLEDIGT, vor=timedelta(hours=2))  # außerhalb des Fensters
    ergebnis = status.check_error_rate()
    assert not ergebnis.ok
    assert ergebnis.detail == "3 von 5 Aufträgen der letzten Stunde gescheitert (60%)"

    for _ in range(10):
        _auftrag(TaskStatus.ERLEDIGT)
    assert status.check_error_rate().ok, "3 von 15 = 20 %"


@pytest.mark.django_db
def test_gescheiterte_auftraege_und_tote_ereignisse() -> None:
    assert status.check_failed().ok
    _auftrag(TaskStatus.TOT, vor=timedelta(hours=25))
    assert status.check_failed().ok, "älter als 24 Stunden"

    _auftrag(TaskStatus.FEHLGESCHLAGEN)
    ParkedEvent.objects.create(subscription="s", event_seq=1, aggregate_id=uuid.uuid4(), state=ParkedState.TOT)
    ergebnis = status.check_failed()
    assert not ergebnis.ok
    assert ergebnis.detail == "1 gescheiterte Aufträge (24 h), 1 tote Ereignisse"


def test_eine_kaputte_pruefung_scheitert_ohne_absturz(monkeypatch: pytest.MonkeyPatch) -> None:
    def kaputt() -> status.Check:
        raise RuntimeError("geheime Adresse 10.0.0.1")

    monkeypatch.setattr(status, "CHECKS", {"x": kaputt})
    assert status.run_checks() == {"x": status.Check(False, "nicht prüfbar (RuntimeError)")}


@pytest.mark.django_db
def test_ergebnis_gilt_kurz_aus_dem_cache(client: Client, mit_bedarf: Any) -> None:
    assert client.get("/health/worker/").status_code == 503
    _alle_worker()
    assert client.get("/health/worker/").status_code == 503, "Cache"
    cache.clear()
    assert client.get("/health/worker/").status_code == 200


@pytest.mark.django_db
def test_je_pruefung_ein_status_damit_kein_befund_einen_anderen_verdeckt(client: Client, mit_bedarf: Any) -> None:
    """Ein gescheiterter Auftrag bleibt 24 h rot; ein späterer Rückstau muss trotzdem alarmieren."""
    _alle_worker()
    _auftrag(TaskStatus.FEHLGESCHLAGEN)

    def status_von(auswahl: str) -> int:
        cache.clear()
        return client.get(f"/health/worker/?pruefung={auswahl}").status_code

    assert client.get("/health/worker/").status_code == 503, "ohne Auswahl entscheiden alle"
    assert status_von("gescheitert") == 503
    assert status_von("rueckstau") == 200
    assert status_von("lebenszeichen,rueckstau") == 200
    assert status_von("fehlerquote") == 200, "1 von 1 zählt erst ab MIN_FINISHED"

    alt = _auftrag(TaskStatus.WARTEND)
    Task.objects.filter(pk=alt.pk).update(run_after=timezone.now() - timedelta(seconds=status.MAX_TASK_WAIT + 60))
    assert status_von("rueckstau") == 503, "der neue Befund schlägt an, obwohl gescheitert schon rot ist"

    cache.clear()
    daten = client.get("/health/worker/?pruefung=rueckstau").json()
    assert set(daten["checks"]) == {"rueckstau"}
    assert daten["status"] == "error"


@pytest.mark.django_db
def test_unbekannte_pruefung_400_ohne_echo(client: Client) -> None:
    antwort = client.get("/health/worker/?pruefung=rueckstau,<b>gibt-es-nicht</b>")

    assert antwort.status_code == 400
    assert b"gibt-es-nicht" not in antwort.content
    assert antwort.json()["pruefungen"] == ["lebenszeichen", "rueckstau", "fehlerquote", "gescheitert", "texterkennung"]


@pytest.mark.django_db
def test_texte_der_pruefungen_nur_fuer_die_eigene_ueberwachung(client: Client, mit_bedarf: Any) -> None:
    mit_bedarf.METRICS_TOKEN = "t" * 40
    _alle_worker()
    _auftrag(TaskStatus.FEHLGESCHLAGEN)

    aussen = client.get("/health/worker/", REMOTE_ADDR="203.0.113.7")
    assert aussen.status_code == 503
    assert aussen.json() == {
        "status": "error",
        "checks": {
            "lebenszeichen": {"ok": True},
            "rueckstau": {"ok": True},
            "fehlerquote": {"ok": True},
            "gescheitert": {"ok": False},
            "texterkennung": {"ok": True},
        },
    }

    mit_token = client.get("/health/worker/", REMOTE_ADDR="203.0.113.7", HTTP_AUTHORIZATION="Bearer " + "t" * 40)
    assert mit_token.json()["checks"]["gescheitert"]["detail"] == "1 gescheiterte Aufträge (24 h), 0 tote Ereignisse"
    intern = client.get("/health/worker/", REMOTE_ADDR="10.1.2.3")
    assert "detail" in intern.json()["checks"]["lebenszeichen"]


# --- „Worker lebt“ --------------------------------------------------------------------------------


class _Sender:
    def __init__(self) -> None:
        self.meldungen: list[tuple[str, str, bool, str]] = []

    def __call__(self, url: str, token: str, ok: bool, error: str) -> None:
        self.meldungen.append((url, token, ok, error))


def test_push_meldet_im_takt_und_sofort_bei_wechsel() -> None:
    uhr = [0.0]
    sender = _Sender()
    push = Push("https://status.example/api/v1/endpoints/w/external", "t", 60, sender=sender, clock=lambda: uhr[0])
    push._background = False

    assert push.report(True)
    assert not push.report(True), "im Takt"
    uhr[0] = 30.0
    assert push.report(False, "Rolle(n) tasks ohne Lebenszeichen"), "Wechsel sofort"
    uhr[0] = 100.0
    assert push.report(False, "x")
    assert [m[2] for m in sender.meldungen] == [True, False, False]
    assert sender.meldungen[1][3] == "Rolle(n) tasks ohne Lebenszeichen"


def test_push_an_gatus_mit_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.events import push as push_modul

    gesehen: dict[str, Any] = {}

    class Antwort:
        def __enter__(self) -> Antwort:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b""

    def oeffnen(anfrage: Any, timeout: float) -> Antwort:
        gesehen.update(url=anfrage.full_url, methode=anfrage.get_method(), kopf=dict(anfrage.header_items()))
        return Antwort()

    monkeypatch.setattr(urllib.request, "urlopen", oeffnen)
    push_modul.send("https://status.example/api/v1/endpoints/betrieb_worker/external", "geheim", False, "Rolle x")

    assert gesehen["methode"] == "POST"
    assert gesehen["url"].endswith("/external?success=false&error=Rolle+x")
    assert gesehen["kopf"]["Authorization"] == "Bearer geheim"


def test_fehler_beim_melden_stoert_den_worker_nicht(caplog: pytest.LogCaptureFixture) -> None:
    def kaputt(*args: object) -> None:
        raise OSError("Statusseite weg")

    push = Push("https://status.example/x", sender=kaputt, background=False)
    assert push.report(True)
    assert "gescheitert (OSError)" in caplog.text


def test_push_nur_mit_adresse(settings: Any) -> None:
    settings.WORKER_PUSH_URL = ""
    assert push_from_settings() is None
    settings.WORKER_PUSH_URL = "https://status.example/x"
    settings.WORKER_PUSH_TOKEN = "t"
    settings.WORKER_PUSH_INTERVAL = 30
    eingestellt = push_from_settings()
    assert eingestellt is not None
    assert (eingestellt.url, eingestellt.token, eingestellt.interval) == ("https://status.example/x", "t", 30.0)


def test_nur_der_worker_mit_scheduler_meldet(settings: Any) -> None:
    settings.WORKER_PUSH_URL = "https://status.example/x"
    assert _worker(scheduler=Schleife()).push is not None
    assert _worker(sequencer=Schleife(), roles=["tasks"]).push is None, "worker-heavy verdeckt keinen Ausfall"


@pytest.mark.django_db
def test_worker_meldet_lebt_und_haengende_rolle(settings: Any) -> None:
    settings.WORKER_PUSH_URL = "https://status.example/x"
    haengt = Schleife(haengt=True)
    worker = _worker(sequencer=haengt, scheduler=Schleife(), stale_after=0.2)
    sender = _Sender()
    assert worker.push is not None
    worker.push = Push("https://status.example/x", interval=0.0, sender=sender, background=False)
    stop = threading.Event()

    _stoppen_wenn(stop, lambda: any(not m[2] for m in sender.meldungen))
    with mock.patch.object(presence, "announce"):
        worker.run(stop)

    assert sender.meldungen[0][2] is True, "solange alle Rollen arbeiten"
    fehler = next(m for m in sender.meldungen if not m[2])
    assert fehler[3] == "Rolle(n) sequencer ohne Lebenszeichen"


# --- Deploy-Prüfung (deploy/scripts/verify_deploy.py) ---------------------------------------------


def _verify() -> dict[str, Any]:
    quelle = VERIFY.read_text(encoding="utf-8").split('if __name__ == "__main__"')[0]
    namensraum: dict[str, Any] = {"__name__": "verify_deploy"}
    exec(compile(quelle, str(VERIFY), "exec"), namensraum)  # noqa: S102 – eigenes Skript aus dem Repo
    return namensraum


@pytest.mark.django_db
def test_deploy_pruefung_verlangt_den_worker(mit_bedarf: Any) -> None:
    pruefe_worker = cast(Any, _verify()["pruefe_worker"])

    beginn = time.monotonic()
    ok, detail = pruefe_worker(warten=0.05, takt=0.01)
    assert not ok
    assert detail.startswith("kein Worker für dispatch, scheduler, sequencer, tasks")
    assert time.monotonic() - beginn < 5

    _alle_worker()
    assert pruefe_worker(warten=1, takt=0.01) == (True, "1 Worker (dispatch, scheduler, sequencer, tasks)")


def test_deploy_pruefung_ohne_bedarf_oder_abgeschaltet(settings: Any) -> None:
    pruefe_worker = cast(Any, _verify()["pruefe_worker"])
    settings.EVENTS_WORKER_REQUIRED = "false"
    assert pruefe_worker(warten=1) == (True, "nicht erforderlich")
    assert pruefe_worker(warten=0) == (True, "nicht geprüft (VERIFY_WORKER_SECONDS=0)")


def test_gatus_beispiel_alarmiert_je_pruefung_getrennt() -> None:
    """docs/MONITORING.md: je Prüfung ein Endpunkt, keiner für alle zusammen."""
    text = (VERIFY.parents[2] / "docs" / "MONITORING.md").read_text(encoding="utf-8")
    for name in status.CHECKS:
        assert f"/health/worker/?pruefung={name}\n" in text, name
    assert "url: https://mandari.example.org/health/worker/\n" not in text
