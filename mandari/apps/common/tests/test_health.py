# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Liveness und Readiness (Issue #231, Teil 1): Readiness meldet den Ausfall von Redis
oder Elasticsearch mit 503, Liveness bleibt davon unberührt; Zeitlimits greifen;
optionale Prüfungen werden gemeldet, ohne die Antwort rot zu machen.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from django.test import Client

from apps.common import health
from apps.events import presence

# Die Anfragen laufen durch den vollständigen Middleware-Stack; dessen Verbindungs-Aufräumen darf die DB
# berühren. Ohne Freigabe hingen die Tests von der Reihenfolge ab (unter pytest-xdist rot).
pytestmark = pytest.mark.django_db


@pytest.fixture
def alles_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "CHECKS", {name: (lambda: "ok") for name in health.CHECKS})


def _ausfall(name: str, monkeypatch: pytest.MonkeyPatch, fehler: Exception | None = None) -> None:
    def kaputt() -> str:
        raise fehler or ConnectionError("Verbindung abgelehnt")

    checks = {n: (lambda: "ok") for n in health.CHECKS}
    checks[name] = kaputt
    monkeypatch.setattr(health, "CHECKS", checks)


def test_liveness_braucht_keine_abhaengigkeiten(
    client: Client, monkeypatch: pytest.MonkeyPatch, django_assert_num_queries: Any
) -> None:
    _ausfall("database", monkeypatch)
    _ausfall("cache", monkeypatch)

    with django_assert_num_queries(0):
        response = client.get("/health/live/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_readiness_ist_gruen_wenn_alles_geht(client: Client, alles_ok: None) -> None:
    response = client.get("/health/ready/")

    daten = response.json()
    assert response.status_code == 200
    assert daten["status"] == "ok"
    assert set(daten["checks"]) == {"database", "cache", "elasticsearch", "storage", "worker"}
    assert all(c["ok"] for c in daten["checks"].values())


@pytest.mark.parametrize("dienst", ["cache", "elasticsearch"])
def test_readiness_meldet_ausfall_von_redis_oder_elasticsearch(
    client: Client, monkeypatch: pytest.MonkeyPatch, dienst: str
) -> None:
    _ausfall(dienst, monkeypatch)

    response = client.get("/health/ready/")

    daten = response.json()
    assert response.status_code == 503
    assert daten["status"] == "error"
    assert daten["checks"][dienst]["ok"] is False
    assert "ConnectionError" in daten["checks"][dienst]["detail"]
    assert client.get("/health/live/").status_code == 200, "Liveness bleibt unberührt"


def test_optionale_pruefung_macht_nicht_rot(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    _ausfall("elasticsearch", monkeypatch)
    monkeypatch.setenv("HEALTH_READY_OPTIONAL", "elasticsearch")

    response = client.get("/health/ready/")

    daten = response.json()
    assert response.status_code == 200
    assert daten["status"] == "degraded"
    assert daten["checks"]["elasticsearch"] == {
        **daten["checks"]["elasticsearch"],
        "ok": False,
        "optional": True,
    }


def test_haengende_pruefung_laeuft_ins_zeitlimit(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    def haengt() -> str:
        time.sleep(health.CHECK_TIMEOUT + 1)
        return "zu spät"

    checks: dict[str, Any] = {n: (lambda: "ok") for n in health.CHECKS}
    checks["database"] = haengt
    monkeypatch.setattr(health, "CHECKS", checks)

    start = time.monotonic()
    response = client.get("/health/ready/")

    assert response.status_code == 503
    assert "Zeitlimit" in response.json()["checks"]["database"]["detail"]
    assert time.monotonic() - start < health.CHECK_TIMEOUT + 1, "Antwort wartet nicht auf die hängende Prüfung"


@pytest.mark.django_db
def test_echte_pruefungen_datenbank_cache_speicher() -> None:
    """Die echten Prüfungen laufen gegen die Testumgebung (SQLite, LocMem, Dateisystem)."""
    assert health.check_database()
    assert health.check_cache()
    assert health.check_storage() == "beschreibbar"


def test_alter_health_endpunkt_bleibt(client: Client) -> None:
    assert client.get("/health/").status_code == 200


# -- Worker (Issue #509): nur gemeldet, wenn die Installation ihn braucht ---------------------------


@pytest.fixture
def nur_worker_echt(monkeypatch: pytest.MonkeyPatch, settings: Any) -> Any:
    """Alle Prüfungen bis auf den Worker in Ordnung; ohne Bedarf an einem Worker."""
    checks: dict[str, Any] = {n: (lambda: "ok") for n in health.CHECKS}
    checks["worker"] = health.check_worker
    monkeypatch.setattr(health, "CHECKS", checks)
    settings.EVENTS_WORKER_REQUIRED = ""
    settings.INGESTOR_EVENTS_ENABLED = False
    return settings


def test_ohne_bedarf_meldet_der_fehlende_worker_nichts(client: Client, nur_worker_echt: Any) -> None:
    daten = client.get("/health/ready/").json()

    assert daten["status"] == "ok"
    assert daten["checks"]["worker"]["ok"] is True
    assert daten["checks"]["worker"]["detail"] == "nicht erforderlich (0 Worker)"
    assert client.get("/health/").json() == {"status": "ok", "database": "ok", "worker": "nicht_erforderlich"}


def test_fehlender_worker_bei_bedarf_ist_degraded_aber_nicht_rot(client: Client, nur_worker_echt: Any) -> None:
    nur_worker_echt.INGESTOR_EVENTS_ENABLED = True  # Ereignisse brauchen den Sequenzierer

    response = client.get("/health/ready/")

    daten = response.json()
    assert response.status_code == 200, "die Anwendung bleibt in Betrieb"
    assert daten["status"] == "degraded"
    assert daten["checks"]["worker"] == {**daten["checks"]["worker"], "ok": False, "optional": True}
    assert daten["checks"]["worker"]["detail"] == "kein Worker für sequencer"
    alt = client.get("/health/")
    assert alt.status_code == 200
    assert alt.json() == {"status": "degraded", "database": "ok", "worker": "fehlt"}


def test_worker_meldung_nennt_fehlende_rollen() -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(presence, "required_roles", lambda: frozenset({"tasks", "sequencer"}))
        patch.setattr(presence, "live_workers", lambda ttl=presence.PRESENCE_TTL: [])
        assert health.worker_finding() == health.Finding(False, "kein Worker für sequencer, tasks")
        assert health.worker_state() == "fehlt"


@pytest.mark.django_db(transaction=True)
def test_laufender_worker_erfuellt_den_bedarf(client: Client, nur_worker_echt: Any) -> None:
    nur_worker_echt.EVENTS_WORKER_REQUIRED = "true"
    presence.announce("w1", sorted(presence.ALL_ROLES), [])

    daten = client.get("/health/ready/").json()

    assert daten["status"] == "ok"
    assert daten["checks"]["worker"]["detail"] == "1 Worker"
    assert client.get("/health/").json()["worker"] == "ok"


def test_worker_zustand_ohne_tabelle_ist_unbekannt(monkeypatch: pytest.MonkeyPatch) -> None:
    def kaputt(ttl: Any = None) -> Any:
        raise RuntimeError("Tabelle fehlt")

    monkeypatch.setattr(presence, "worker_status", kaputt)
    assert health.worker_state() == "unbekannt"
