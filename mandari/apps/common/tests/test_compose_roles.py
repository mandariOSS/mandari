# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compose-Rollenprofile (#55): das Prüfskript läuft im Testlauf mit, damit die CI es ohne Docker abdeckt."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

SKRIPT = Path(__file__).resolve().parents[4] / "scripts" / "check_compose_roles.py"


def _lade_skript() -> Any:
    spec = importlib.util.spec_from_file_location("check_compose_roles", SKRIPT)
    assert spec and spec.loader
    modul = importlib.util.module_from_spec(spec)
    sys.modules["check_compose_roles"] = modul
    spec.loader.exec_module(modul)
    return modul


def test_rollen_decken_alle_dienste_genau_einmal() -> None:
    modul = _lade_skript()
    assert modul.pruefe() == []


def test_basisdatei_bleibt_profilfrei() -> None:
    modul = _lade_skript()
    basis = modul._lade(modul.BASIS)["services"]
    assert basis and all("profiles" not in d for d in basis.values())
    assert {
        "postgres",
        "redis",
        "elasticsearch",
        "mandari",
        "caddy",
        "website",
        "ingestor",
        "minutes-orchestrator",
    } <= set(basis)


def test_worker_dienst_wie_die_anwendung_mit_lebenszeichen() -> None:
    """Dienst worker (Issue #509): gleiches Image und gleiche Umgebung, 512 MB, Heartbeat als Healthcheck."""
    modul = _lade_skript()
    basis = modul._lade(modul.BASIS)["services"]
    worker, anwendung = basis["worker"], basis["mandari"]

    assert worker["image"] == anwendung["image"]
    assert worker["environment"] == anwendung["environment"], "Aufträge sehen dieselbe Konfiguration"
    assert worker["volumes"] == anwendung["volumes"]
    assert worker["mem_limit"] == "512m"
    befehl = worker["command"]
    assert befehl[:3] == ["python", "manage.py", "events_worker"]
    datei = befehl[befehl.index("--heartbeat-file") + 1]
    assert datei in " ".join(worker["healthcheck"]["test"]), "Healthcheck prüft die Datei, die der Worker erneuert"
    assert "mandari" not in worker["depends_on"], "Worker startet vor der Anwendung (Migration → Worker → Web)"
    assert worker["labels"]["mandari.autoheal"] == "true"
    for name in ("TASKS_BACKEND", "EVENTS_WORKER_REQUIRED", "INGESTOR_EVENTS_ENABLED", "EVENTS_DB_DIRECT_URL"):
        assert name in anwendung["environment"]


def test_rollen_worker_mit_direktverbindung_fuer_den_weckruf() -> None:
    modul = _lade_skript()
    worker = modul._lade(modul.ROLLEN["worker"])["services"]["worker"]
    assert ":5432/" in worker["environment"]["EVENTS_DB_DIRECT_URL"], "am Pooler vorbei"
    for rolle in ("web", "data"):
        assert modul._lade(modul.ROLLEN[rolle])["services"]["worker"]["profiles"] == ["aus"]
