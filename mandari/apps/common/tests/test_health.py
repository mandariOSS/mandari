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
    assert set(daten["checks"]) == {"database", "cache", "elasticsearch", "storage"}
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
