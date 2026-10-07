# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compose-Rollenprofile (#55): das Prüfskript läuft im Testlauf mit, damit die CI es ohne Docker abdeckt."""

from __future__ import annotations

import importlib.util
import re
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
    for name in (
        "TASKS_BACKEND",
        "EVENTS_WORKER_REQUIRED",
        "INGESTOR_EVENTS_ENABLED",
        "SESSION_EVENTS",
        "EVENTS_DB_DIRECT_URL",
    ):
        assert name in anwendung["environment"]
    assert anwendung["environment"]["SESSION_EVENTS"] == "${SESSION_EVENTS:-aus}", "Standard bleibt aus"


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
    live = set(str(_option(basis["worker-live"]["command"], "--queues")).split(","))
    assert haupt_queues | {"ocr", "ai"} | live == set(settings.TASK_QUEUES), "zusammen jede Warteschlange"

    # sonst wie der Hauptworker: Image, Volumes, Lebenszeichen, Neustart bei Hängern
    for schluessel in ("image", "volumes", "healthcheck", "labels", "depends_on", "stop_grace_period"):
        assert ocr[schluessel] == haupt[schluessel], schluessel
    assert ocr["container_name"] != haupt["container_name"]
    datei = _option(ocr["command"], "--heartbeat-file")
    assert datei and datei in " ".join(ocr["healthcheck"]["test"])


def _vorgabe(wert: str) -> str:
    """Vorgabe aus ``${NAME:-vorgabe}``."""
    treffer = re.fullmatch(r"\$\{(\w+):-([^}]*)\}", wert)
    assert treffer, wert
    return treffer.group(2)


def _megabyte(wert: str) -> int:
    zahl, einheit = re.fullmatch(r"(\d+)([gm])", wert.lower()).groups()  # type: ignore[union-attr]
    return int(zahl) * (1024 if einheit == "g" else 1)


def test_dokumentkette_texterkennung_im_worker_heavy() -> None:
    """
    ADR Dokumentkette (#919), Abschnitt 8: Die Erkennung liest nur aus Ablage und Objektspeicher; worker-heavy
    braucht deshalb dieselbe Ablage und dieselben OBJ_*-Variablen wie die Anwendung, Parallelität 1 in ocr und
    eine eigene Speichergrenze, unter der Runner und ein Tesseract-Unterprozess zugleich Platz haben.
    """
    modul = _lade_skript()
    basis = modul._lade(modul.BASIS)["services"]
    haupt, ocr, anwendung = basis["worker"], basis["worker-heavy"], basis["mandari"]
    umgebung = ocr["environment"]

    assert "mandari_files:/app/files" in ocr["volumes"], "Dokumentablage eingehängt"
    assert umgebung["OPARL_FILES_ROOT"] == "/app/files"
    for name in (
        "OBJ_ENABLED",
        "OBJ_ENDPOINT",
        "OBJ_BUCKET",
        "OBJ_KEY",
        "OBJ_SECRET",
        "OBJ_REGION",
        "OBJ_CACHE_MAX_GB",
    ):
        assert umgebung[name] == anwendung["environment"][name], name
    # Umgebung wie die Anwendung; eigen ist nur der Adressraum je Tesseract-Unterprozess
    assert set(umgebung) == set(haupt["environment"])
    assert {k for k in umgebung if umgebung[k] != haupt["environment"][k]} == {"OCR_MEMORY_LIMIT_MB"}
    # Eigene Variable: OCR_MEMORY_LIMIT_MB in der .env träfe auch Anwendung (1 GB) und Ingestor (512 MB)
    assert umgebung["OCR_MEMORY_LIMIT_MB"].startswith("${WORKER_HEAVY_OCR_MEMORY_LIMIT_MB:-")
    assert ocr["mem_limit"].startswith("${WORKER_HEAVY_MEM_LIMIT:-")

    assert _option(ocr["command"], "--concurrency") == "ocr=1", "eine Erkennung zugleich"
    limit = _megabyte(_vorgabe(ocr["mem_limit"]))
    runner = int(str(_option(ocr["command"], "--max-memory-mb")))
    tesseract = int(_vorgabe(umgebung["OCR_MEMORY_LIMIT_MB"]))
    assert limit == 3 * 1024, "Richtwert des ADR"
    assert tesseract >= 2048, "Seitengrenze wie in Produktion (#817)"
    assert runner + tesseract < limit, "Runner und Tesseract passen unter das Limit, der Runner startet vorher neu"


def test_schalter_der_dokumentkette_bleiben_aus_und_gleich() -> None:
    """Standard bleibt der Ingestor; Anwendung, Worker und Ingestor lesen denselben Schalter (sonst arbeiten zwei)."""
    modul = _lade_skript()
    basis = modul._lade(modul.BASIS)["services"]
    anwendung, ingestor = basis["mandari"]["environment"], basis["ingestor"]["environment"]

    assert anwendung["TEXT_EXTRACTION_RUNNER"] == ingestor["TEXT_EXTRACTION_RUNNER"]
    assert anwendung["TEXT_EXTRACTION_RUNNER"] == "${TEXT_EXTRACTION_RUNNER:-ingestor}"
    assert anwendung["TASKS_BACKEND"] == "${TASKS_BACKEND:-immediate}"
    # Abruf im Worker schreibt in dieselbe Ablage wie der Ingestor: gleiche Schutzgrenze für freien Platz
    assert anwendung["FILE_CACHE_MIN_FREE_GB"] == ingestor["FILE_CACHE_MIN_FREE_GB"]
    assert anwendung["TEXT_EXTRACTION_QUEUE_DEPTH"] == "${TEXT_EXTRACTION_QUEUE_DEPTH:-20}"


def test_env_beispiel_nennt_die_grenzen_des_worker_heavy() -> None:
    text = (SKRIPT.parents[1] / ".env.example").read_text(encoding="utf-8")
    for name in ("WORKER_HEAVY_MEM_LIMIT", "WORKER_HEAVY_OCR_MEMORY_LIMIT_MB", "TEXT_EXTRACTION_QUEUE_DEPTH"):
        assert f"# {name}=" in text, name


def test_helm_worker_heavy_mit_eigener_grenze() -> None:
    """Helm wie Compose: 3Gi, eigene Seitengrenze nur im worker-heavy (worker.heavy.extraEnv)."""
    import yaml

    chart = SKRIPT.parents[1] / "deploy" / "kubernetes" / "helm" / "mandari"
    heavy = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))["worker"]["heavy"]
    assert heavy["resources"]["limits"]["memory"] == "3Gi"
    assert heavy["maxMemoryMb"] + int(heavy["extraEnv"]["OCR_MEMORY_LIMIT_MB"]) < 3 * 1024
    produktion = yaml.safe_load((chart / "values-production.yaml").read_text(encoding="utf-8"))["worker"]["heavy"]
    assert produktion["resources"]["limits"]["memory"] == "3Gi"
    vorlage = (chart / "templates" / "worker.yaml").read_text(encoding="utf-8")
    assert '"extraEnv" $heavy.extraEnv' in vorlage, "worker.heavy.extraEnv erreicht nur worker-heavy"


def test_helm_schalter_der_texterkennung_erreicht_auch_den_ingestor() -> None:
    """TEXT_EXTRACTION_RUNNER in der gemeinsamen Umgebung: der Ingestor kennt kein app.extraEnv."""
    import yaml

    chart = SKRIPT.parents[1] / "deploy" / "kubernetes" / "helm" / "mandari"
    werte = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))
    assert werte["textExtraction"]["runner"] == "ingestor", "Standard bleibt der Ingestor"
    hilfen = (chart / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    gemeinsam = hilfen[hilfen.index('define "mandari.commonEnv"') :]
    assert "- name: TEXT_EXTRACTION_RUNNER" in gemeinsam[: gemeinsam.index("{{- end -}}")]
    assert "mandari.commonEnv" in (chart / "templates" / "ingestor.yaml").read_text(encoding="utf-8")


def test_live_uebertragungen_in_eigenem_worker() -> None:
    """Issue #915: Leseaufträge (~50 s) warten nie hinter Texterkennung oder Mail; Schalter erreicht den Worker."""
    modul = _lade_skript()
    basis = modul._lade(modul.BASIS)["services"]
    haupt, live = basis["worker"], basis["worker-live"]

    assert _option(live["command"], "--roles") == "tasks"
    assert _option(live["command"], "--queues") == "live"
    assert "live" not in str(_option(haupt["command"], "--queues")).split(",")
    # Feste Parallelität: Der Worker startet auch bei ausgeschaltetem Schalter (Parallelität laut Einstellung 0)
    assert _option(live["command"], "--concurrency") == "live=2"
    for schluessel in ("image", "environment", "volumes", "healthcheck", "labels", "depends_on", "stop_grace_period"):
        assert live[schluessel] == haupt[schluessel], schluessel
    datei = _option(live["command"], "--heartbeat-file")
    assert datei and datei in " ".join(live["healthcheck"]["test"])
    grenze = int(str(_option(live["command"], "--max-memory-mb")))
    assert live["mem_limit"] == "512m" and grenze < 512, "eigenes Limit, Runner startet vorher neu"
    assert live["environment"]["LIVE_UEBERTRAGUNG_AKTIV"] == "${LIVE_UEBERTRAGUNG_AKTIV:-false}", "Standard aus"
    for rolle in ("web", "data"):
        assert modul._lade(modul.ROLLEN[rolle])["services"]["worker-live"]["profiles"] == ["aus"]
    assert "DATA_HOST" in modul._lade(modul.ROLLEN["worker"])["services"]["worker-live"]["environment"]["DATABASE_URL"]


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


def test_worker_lebt_meldung_erreicht_den_worker() -> None:
    """Issue #574: Der Worker meldet sich selbst an die Statusseite, wenn eine Adresse gesetzt ist."""
    modul = _lade_skript()
    umgebung = modul._lade(modul.BASIS)["services"]["worker"]["environment"]
    assert umgebung["WORKER_PUSH_URL"] == "${WORKER_PUSH_URL:-}", "Standard: keine Meldung"
    assert "WORKER_PUSH_TOKEN" in umgebung


def test_doku_der_dokumentkette_nennt_befehle_kennzahlen_und_alarme() -> None:
    """Befehls- und Metriknamen wie im ADR Dokumentkette (#919); jeder Beispielalarm ist in MONITORING.md erklärt."""
    import yaml

    wurzel = SKRIPT.parents[1]
    deployment = (wurzel / "DEPLOYMENT.md").read_text(encoding="utf-8")
    for befehl in ("umschalten", "nacharbeiten", "zuruecksetzen"):
        assert f"manage.py dokumentkette {befehl}" in deployment, befehl
    # Stichtag vor dem Umschalten der Erkennung, sonst gälte für bisher nicht abgelegte Quellen kein Altbestand
    assert deployment.index("manage.py dokumentkette umschalten") < deployment.index("**Schritt 3: Erkennung im Worker")

    monitoring = (wurzel / "docs" / "MONITORING.md").read_text(encoding="utf-8")
    for metrik in (
        "mandari_files_fetch_queued",
        "mandari_files_fetch_retry_due",
        "mandari_files_fetch_errors_total",
        "mandari_files_stored_without_text",
        "mandari_files_text_outdated",
    ):
        assert f"| `{metrik}` |" in monitoring, metrik
    regeln = yaml.safe_load(
        (wurzel / "deploy" / "monitoring" / "prometheus-alerts.example.yml").read_text(encoding="utf-8")
    )
    gruppe = next(g for g in regeln["groups"] if g["name"] == "mandari-dokumentkette")
    for regel in gruppe["rules"]:
        assert f"`{regel['alert']}`" in monitoring, regel["alert"]


def _abschnitt(text: str, ueberschrift: str) -> str:
    """Text eines Markdown-Abschnitts bis zur nächsten Überschrift derselben oder höheren Ebene."""
    ebene = ueberschrift.split(" ", 1)[0]
    beginn = text.index(ueberschrift + "\n")
    rest = text[beginn + len(ueberschrift) :]
    ende = re.search(rf"^#{{1,{len(ebene)}}} ", rest, re.M)
    return rest[: ende.start()] if ende else rest


def test_doku_der_dokumentkette_passt_zu_befehl_pruefungen_und_warteschlange_ocr() -> None:
    """
    Abgleich mit der Umsetzung (#936, #942): Schritte und Optionen von ``dokumentkette`` gibt es im Befehl, die
    genannten Prüfungen in ``/health/worker/``, die Warnschwellen nennen ``dokumentabruf`` und ``dokumenttext``,
    und ``ocr`` (Parallelität 1, Tiefe ``TEXT_EXTRACTION_QUEUE_DEPTH``) hat einen eigenen Alarm statt
    „Auftragsrückstand“.
    """
    import yaml

    from apps.events.status import CHECKS
    from hub.ris.management.commands.dokumentkette import Command

    wurzel = SKRIPT.parents[1]
    deployment = (wurzel / "DEPLOYMENT.md").read_text(encoding="utf-8")
    einschalten = _abschnitt(deployment, "### Dokumentkette einschalten (Issue #919)")
    monitoring = (wurzel / "docs" / "MONITORING.md").read_text(encoding="utf-8")
    kette = _abschnitt(monitoring, "## Dokumentkette")

    parser = Command().create_parser("manage.py", "dokumentkette")
    aktion = next(a for a in parser._actions if a.dest == "schritt")
    assert isinstance(aktion.choices, dict)
    schritte: dict[str, Any] = aktion.choices
    aufrufe = re.findall(r"manage\.py dokumentkette (\w+)((?:\s+--[\w-]+)*)", einschalten)
    assert {schritt for schritt, _ in aufrufe} >= {"umschalten", "nacharbeiten", "zuruecksetzen"}
    for schritt, optionen in aufrufe:
        assert schritt in schritte, schritt
        for option in optionen.split():
            assert option in schritte[schritt]._option_string_actions, (schritt, option)
    assert "nacharbeiten --help" not in einschalten, "Die Option zum Ausführen heißt --ausfuehren"
    assert "`--ausfuehren`" in einschalten
    assert "--ausfuehren" in schritte["nacharbeiten"]._option_string_actions

    for name in re.findall(r"pruefung=(\w+)", einschalten + kette):
        assert name in CHECKS, name
    assert "?pruefung=dokumentabruf" in kette and "?pruefung=dokumenttext" in kette

    regeln = yaml.safe_load(
        (wurzel / "deploy" / "monitoring" / "prometheus-alerts.example.yml").read_text(encoding="utf-8")
    )
    alle = {r["alert"]: r for g in regeln["groups"] for r in g["rules"]}
    assert 'queue!="ocr"' in alle["MandariAuftragsrueckstand"]["expr"]
    steht = alle["MandariTexterkennungSteht"]["expr"]
    assert 'mandari_tasks_oldest_queued_seconds{queue="ocr"}' in steht
    assert 'mandari_tasks_running{queue="ocr"}' in steht
