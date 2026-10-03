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
    """Dienst worker (Issue #509): gleiches Image und gleiche Umgebung, 1 GB, Heartbeat als Healthcheck."""
    modul = _lade_skript()
    basis = modul._lade(modul.BASIS)["services"]
    worker, anwendung = basis["worker"], basis["mandari"]

    assert worker["image"] == anwendung["image"]
    assert worker["environment"] == anwendung["environment"], "Aufträge sehen dieselbe Konfiguration"
    assert worker["volumes"] == anwendung["volumes"]
    assert worker["mem_limit"] == "1g", "Verwaltungsbefehle der Zeitpläne laufen als eigene Prozesse (#516)"
    befehl = worker["command"]
    assert befehl[:3] == ["python", "manage.py", "events_worker"]
    datei = befehl[befehl.index("--heartbeat-file") + 1]
    assert datei in " ".join(worker["healthcheck"]["test"]), "Healthcheck prüft die Datei, die der Worker erneuert"
    assert "mandari" not in worker["depends_on"], "Worker startet vor der Anwendung (Migration → Worker → Web)"
    assert worker["labels"]["mandari.autoheal"] == "true"
    for name in ("TASKS_BACKEND", "EVENTS_WORKER_REQUIRED", "INGESTOR_EVENTS_ENABLED", "EVENTS_DB_DIRECT_URL"):
        assert name in anwendung["environment"]


def test_schalter_des_aenderungsfeeds_erreichen_die_anwendung() -> None:
    """Issue #707: Ohne Durchreichung bliebe ``OPARL_CHANGES_ENABLED`` in der ``.env`` wirkungslos."""
    modul = _lade_skript()
    umgebung = modul._lade(modul.BASIS)["services"]["mandari"]["environment"]
    for name in ("OPARL_CHANGES_ENABLED", "OPARL_CHANGES_RETENTION_DAYS", "OPARL_SNAPSHOT_PARALLEL"):
        assert name in umgebung, name
    assert umgebung["OPARL_CHANGES_ENABLED"] == "${OPARL_CHANGES_ENABLED:-false}", "Standard bleibt aus"


def test_zeitplaene_im_worker_bekommen_ihre_einstellungen() -> None:
    """Issue #516: Die Betriebsprüfungen laufen im Worker und brauchen Empfänger, Statusseite und Metriken."""
    modul = _lade_skript()
    umgebung = modul._lade(modul.BASIS)["services"]["worker"]["environment"]
    for name in ("EVENTS_SCHEDULES_DISABLED", "INSIGHT_ALERT_EMAILS", "GATUS_URL", "METRICS_URL"):
        assert name in umgebung, name
    assert umgebung["METRICS_URL"] == "${METRICS_URL:-http://mandari:8000/metrics/}", "nicht 127.0.0.1 im Worker"


def _option(befehl: list[str], name: str) -> str | None:
    return befehl[befehl.index(name) + 1] if name in befehl else None


def test_texterkennung_und_ki_in_eigenem_worker() -> None:
    """Jeder Neustart eines Runners wartet auf seinen längsten Auftrag (ocr bis 30 min): ocr und ai getrennt."""
    from django.conf import settings

    modul = _lade_skript()
    basis = modul._lade(modul.BASIS)["services"]
    haupt, ocr = basis["worker"], basis["worker-heavy"]

    assert _option(ocr["command"], "--roles") == "tasks"
    assert set(str(_option(ocr["command"], "--queues")).split(",")) == {"ocr", "ai"}
    haupt_queues = set(str(_option(haupt["command"], "--queues")).split(","))
    assert _option(haupt["command"], "--roles") is None, "der Hauptworker hat alle Rollen"
    assert not haupt_queues & {"ocr", "ai"}, "der Hauptworker wartet nie auf die Texterkennung"
    assert haupt_queues | {"ocr", "ai"} == set(settings.TASK_QUEUES), "zusammen jede Warteschlange"

    # sonst wie der Hauptworker: Image, Umgebung, Volumes, Lebenszeichen, Neustart bei Hängern
    for schluessel in ("image", "environment", "volumes", "healthcheck", "labels", "depends_on", "stop_grace_period"):
        assert ocr[schluessel] == haupt[schluessel], schluessel
    assert ocr["container_name"] != haupt["container_name"]
    datei = _option(ocr["command"], "--heartbeat-file")
    assert datei and datei in " ".join(ocr["healthcheck"]["test"])
    grenze = int(str(_option(ocr["command"], "--max-memory-mb")))
    assert ocr["mem_limit"] == "1g" and grenze < 1024, "eigenes Limit, Runner startet vorher neu"


def test_rollen_worker_mit_direktverbindung_fuer_den_weckruf() -> None:
    modul = _lade_skript()
    worker = modul._lade(modul.ROLLEN["worker"])["services"]["worker"]
    assert ":5432/" in worker["environment"]["EVENTS_DB_DIRECT_URL"], "am Pooler vorbei"
    ocr = modul._lade(modul.ROLLEN["worker"])["services"]["worker-heavy"]
    assert "DATA_HOST" in ocr["environment"]["DATABASE_URL"]
    for rolle in ("web", "data"):
        dienste = modul._lade(modul.ROLLEN[rolle])["services"]
        assert dienste["worker"]["profiles"] == ["aus"]
        assert dienste["worker-heavy"]["profiles"] == ["aus"]
