# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verwaltungsbefehle als Zeitpläne statt Host-Cronjobs (Issue #516, ``apps.events.verwaltungsbefehle``).

- Die frühere Beispiel-Crontab steht vollständig als Zeitpläne im Code (gleiche Zeiten, gleiche
  Argumente), auf dem Host bleiben nur Aufgaben des Betriebssystems.
- Jeder Lauf startet den Befehl als eigenen Prozess mit Zeitgrenze; ein Fehlschlag wird nicht
  wiederholt.
- Übergabe ohne Doppelläufe: Bedient ein Worker den Zeitplan, überspringt ein Aufruf von außen (alter
  Cron-Eintrag) mit Hinweis; ohne Worker läuft er wie bisher.
- Einzelne Zeitpläne lassen sich abschalten; ihre Termine verstreichen, ohne später nachgeholt zu
  werden.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from django.conf import settings
from django.core.management import call_command

from apps.events import presence, verwaltungsbefehle
from apps.events.models import ScheduleState
from apps.events.models import Task as TaskRow
from apps.events.schedule import Catchup, Cron, Every, ScheduleRegistry, autodiscover, every, registry
from apps.events.scheduler import Scheduler
from apps.events.tasks_backend import PermanentTaskError, journal_options
from apps.events.tests import auftraege

#: Die bisherige Beispiel-Crontab aus DEPLOYMENT.md (Befehl → Zeit, Argumente)
FRUEHERE_CRONTAB: dict[str, tuple[str, list[str]]] = {
    "send_session_reminders": ("0 7 * * *", []),
    "send_question_reminders": ("30 7 * * *", []),
    "send_task_due_reminders": ("15 7 * * *", []),
    "fetch_person_photos": ("0 3 * * 1", []),
    "sync_plan_boundaries": ("50 4 * * *", []),
    "cleanup_orphaned_accounts": ("45 3 * * *", []),
    # docs/FILE_CACHE.md
    "cache_files": ("40 * * * *", ["--limit", "400"]),
    "loeschabgleich": ("15 * * * *", []),
    "dokumentablage": ("50 * * * *", ["--aufraeumen"]),
    "check_source_health": ("15 * * * *", []),
    "check_service_levels": ("30 6 * * *", []),
    "verify_audit_chain": ("20 4 * * *", []),
    "purge_security_audit_log": ("40 4 * * *", []),
    "session_privacy_purge": ("0 5 1 * *", []),
}
BEGINN = datetime(2026, 10, 3, 6, 59, tzinfo=UTC)


def test_die_fruehere_crontab_steht_vollstaendig_als_zeitplaene_im_code() -> None:
    autodiscover()
    for befehl, (ausdruck, argumente) in FRUEHERE_CRONTAB.items():
        eintrag = registry.get(f"befehl:{befehl}")
        assert eintrag is not None, befehl
        assert eintrag.task is verwaltungsbefehle.befehl_ausfuehren
        assert eintrag.trigger == Cron(ausdruck), befehl
        assert eintrag.args[:2] == (befehl, argumente), befehl
        assert eintrag.catchup == Catchup.NACHHOLEN

    pakete = registry.get("befehl:build_meeting_packages")
    assert pakete is not None
    assert pakete.trigger == Every(timedelta(minutes=1))
    assert pakete.args == ("build_meeting_packages", ["--limit", "5", "--max-seconds", "240"], 300)


def test_jeder_befehl_der_zeitplaene_existiert() -> None:
    from django.core.management import get_commands

    autodiscover()
    befehle = {e.args[0] for e in registry if e.name.startswith(verwaltungsbefehle.PRAEFIX)}
    assert befehle - set(get_commands()) == set()
    assert befehle >= set(FRUEHERE_CRONTAB)


def test_verfuegbarkeitsbericht_nur_mit_statusseite() -> None:
    """Ohne GATUS_URL endete jeder Lauf mit Fehler; in den Tests ist keine gesetzt."""
    assert not settings.GATUS_URL
    autodiscover()
    assert registry.get("befehl:availability_report") is None

    register = ScheduleRegistry()
    verwaltungsbefehle.befehl_als_zeitplan(
        "availability_report", crontab="15 0 1 * *", argumente=["--out", "/berichte/"], zeitgrenze=600, ziel=register
    )
    eintrag = register.get("befehl:availability_report")
    assert eintrag is not None
    assert eintrag.args == ("availability_report", ["--out", "/berichte/"], 600)


def test_runner_zeitgrenze_liegt_ueber_der_des_prozesses() -> None:
    optionen, _ = journal_options()
    pfad = verwaltungsbefehle.befehl_ausfuehren.module_path
    assert optionen.timeout_for(pfad, "default") > verwaltungsbefehle.MAX_ZEITGRENZE
    assert optionen.max_attempts_for(pfad) == 1, "der nächste Termin ist die Wiederholung"
    autodiscover()
    for eintrag in registry:
        if eintrag.name.startswith(verwaltungsbefehle.PRAEFIX):
            assert 0 < eintrag.args[2] <= verwaltungsbefehle.MAX_ZEITGRENZE, eintrag.name


@pytest.mark.parametrize(
    "falsch",
    [
        {"crontab": "0 7 * * *", "minuten": 5},
        {},
        {"crontab": "0 7 * * *", "zeitgrenze": verwaltungsbefehle.MAX_ZEITGRENZE + 1},
    ],
)
def test_registrierung_prueft_ihre_angaben(falsch: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        verwaltungsbefehle.befehl_als_zeitplan("check", ziel=ScheduleRegistry(), **falsch)


# --- Ausführung als eigener Prozess --------------------------------------------------------------


class _Lauf:
    def __init__(self, returncode: int = 0, ausnahme: Exception | None = None) -> None:
        self.returncode = returncode
        self.ausnahme = ausnahme
        self.aufrufe: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.aufrufe.append((argv, kwargs))
        if self.ausnahme is not None:
            raise self.ausnahme
        return subprocess.CompletedProcess(argv, self.returncode)


def test_befehl_laeuft_als_eigener_prozess_mit_kennung_und_zeitgrenze(monkeypatch: pytest.MonkeyPatch) -> None:
    lauf = _Lauf()
    monkeypatch.setattr(subprocess, "run", lauf)

    assert verwaltungsbefehle.befehl_ausfuehren.call("build_meeting_packages", ["--limit", "5"], 300) == 0

    (argv, kwargs), *_ = lauf.aufrufe
    assert argv == [
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        "build_meeting_packages",
        "--limit",
        "5",
    ]
    assert kwargs["env"][verwaltungsbefehle.AUS_ZEITPLAN_ENV] == "1"
    assert kwargs["timeout"] == 300
    assert kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize(
    "lauf",
    [_Lauf(returncode=1), _Lauf(ausnahme=subprocess.TimeoutExpired(["x"], 5))],
    ids=["exit-code", "zeitgrenze"],
)
def test_fehlschlag_wird_nicht_wiederholt(monkeypatch: pytest.MonkeyPatch, lauf: _Lauf) -> None:
    monkeypatch.setattr(subprocess, "run", lauf)
    with pytest.raises(PermanentTaskError):
        verwaltungsbefehle.befehl_ausfuehren.call("verify_audit_chain", [], 5)


def test_befehl_laeuft_wirklich_als_prozess() -> None:
    """Ohne Attrappe: ``manage.py check`` startet mit derselben Umgebung und endet mit 0."""
    assert verwaltungsbefehle.befehl_ausfuehren.call("check", [], 300) == 0
    with pytest.raises(PermanentTaskError):
        verwaltungsbefehle.befehl_ausfuehren.call("gibt_es_nicht", [], 300)


# --- Übergabe vom Cron-Eintrag an den Zeitplan ----------------------------------------------------


@pytest.mark.django_db
def test_zeitplan_uebernimmt_nur_mit_scheduler_und_runner(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    monkeypatch.delenv(verwaltungsbefehle.AUS_ZEITPLAN_ENV, raising=False)
    befehl = "send_question_reminders"
    assert not verwaltungsbefehle.zeitplan_uebernimmt(befehl), "ohne Worker läuft der Cron-Eintrag weiter"

    presence.announce("planer", ["scheduler", "sequencer"], [])
    assert not verwaltungsbefehle.zeitplan_uebernimmt(befehl), "niemand führt den Auftrag aus"
    presence.announce("nur-ocr", ["tasks"], ["ocr", "ai"])
    assert not verwaltungsbefehle.zeitplan_uebernimmt(befehl), "kein Runner für default"
    presence.announce("runner", ["tasks"], ["default", "mail"])
    assert verwaltungsbefehle.zeitplan_uebernimmt(befehl)

    assert not verwaltungsbefehle.zeitplan_uebernimmt("audit_chain_backfill"), "ohne Zeitplan"
    settings.EVENTS_SCHEDULES_DISABLED = [f"befehl:{befehl}"]
    assert not verwaltungsbefehle.zeitplan_uebernimmt(befehl), "abgeschaltet: Cron-Eintrag läuft wieder"
    settings.EVENTS_SCHEDULES_DISABLED = []
    monkeypatch.setenv(verwaltungsbefehle.AUS_ZEITPLAN_ENV, "1")
    assert not verwaltungsbefehle.zeitplan_uebernimmt(befehl), "der Lauf aus dem Zeitplan selbst"


@pytest.mark.django_db
def test_alter_cron_eintrag_ueberspringt_wenn_der_worker_uebernimmt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(verwaltungsbefehle.AUS_ZEITPLAN_ENV, raising=False)
    presence.announce("worker", ["scheduler", "tasks"], [])

    def lauf(*argumente: str) -> tuple[str, str]:
        aus, fehler = StringIO(), StringIO()
        call_command("send_question_reminders", *argumente, stdout=aus, stderr=fehler)
        return aus.getvalue(), fehler.getvalue()

    aus, fehler = lauf()
    assert aus == ""
    assert "läuft als Zeitplan im Worker – Aufruf übersprungen" in fehler

    aus, fehler = lauf("--trotz-zeitplan")
    assert "Aufruf übersprungen" not in fehler
    assert aus, "von Hand erzwungen läuft der Befehl"
    aus, fehler = lauf("--dry-run")
    assert "Aufruf übersprungen" not in fehler, "Probelauf immer"

    presence.withdraw("worker")
    aus, fehler = lauf()
    assert "Aufruf übersprungen" not in fehler, "ohne Worker wie bisher"


def test_jeder_befehl_der_zeitplaene_hat_die_sperre_und_die_uebergabe(settings: Any) -> None:
    """Ohne EinmaligMixin liefe ein alter Cron-Eintrag neben dem Zeitplan (Doppellauf)."""
    from django.core.management import get_commands, load_command_class

    from apps.common.einmalig import EinmaligMixin

    befehle = _alle_befehle(settings)
    assert {"session_privacy_purge", "cache_files", "generate_alerts", "send_digest"} <= befehle
    ohne = sorted(b for b in befehle if not isinstance(load_command_class(get_commands()[b], b), EinmaligMixin))
    assert ohne == []


# --- Abschalten einzelner Zeitpläne ---------------------------------------------------------------


@pytest.mark.django_db
def test_abgeschalteter_zeitplan_laesst_termine_verstreichen(settings: Any) -> None:
    auftraege.zuruecksetzen()
    register = ScheduleRegistry()
    every(minutes=15, name="aus", args=("a",), registry=register)(cast(Any, auftraege).merken)
    planer = Scheduler(registry=register, holder="a")
    planer.tick(BEGINN)
    settings.EVENTS_SCHEDULES_DISABLED = ["aus"]

    assert planer.tick(BEGINN + timedelta(minutes=2)) == []
    assert not TaskRow.objects.exists()
    assert ScheduleState.objects.get(name="aus").last_slot == datetime(2026, 10, 3, 7, 0, tzinfo=UTC)

    settings.EVENTS_SCHEDULES_DISABLED = []
    assert planer.tick(BEGINN + timedelta(minutes=5)) == [], "verstrichene Termine nicht nachholen"
    assert planer.tick(BEGINN + timedelta(minutes=17)) == ["aus"]
    assert TaskRow.objects.count() == 1


def test_liste_der_abgeschalteten_aus_den_einstellungen(settings: Any) -> None:
    from apps.events.schedule import disabled_schedules

    settings.EVENTS_SCHEDULES_DISABLED = " befehl:a , ,befehl:b"
    assert disabled_schedules() == {"befehl:a", "befehl:b"}
    settings.EVENTS_SCHEDULES_DISABLED = ["befehl:c"]
    assert disabled_schedules() == {"befehl:c"}


# --- Bedingte Zeitpläne und Betriebsdoku ----------------------------------------------------------

REPO = Path(settings.BASE_DIR).parent


def _alle_befehle(settings: Any) -> set[str]:
    """Befehle aller Zeitpläne, auch der nur bei bestimmten Einstellungen registrierten."""
    from apps.common import schedules

    settings.INSIGHT_SUBSCRIPTIONS_ENABLED = True
    settings.GATUS_URL = "https://status.example.org"
    register = ScheduleRegistry()
    schedules.registrieren(ziel=register)
    return {e.args[0] for e in register if e.name.startswith(verwaltungsbefehle.PRAEFIX)}


def test_abos_laufen_als_zeitplan_nur_wenn_sie_eingeschaltet_sind(settings: Any) -> None:
    from apps.common import schedules

    settings.INSIGHT_SUBSCRIPTIONS_ENABLED = False
    aus = ScheduleRegistry()
    schedules.registrieren(ziel=aus)
    assert aus.get("befehl:generate_alerts") is None
    assert aus.get("befehl:send_digest") is None

    settings.INSIGHT_SUBSCRIPTIONS_ENABLED = True
    settings.GATUS_URL = "https://status.example.org"
    an = ScheduleRegistry()
    schedules.registrieren(ziel=an)
    alarme, wochenmail, bericht = (
        an.get("befehl:generate_alerts"),
        an.get("befehl:send_digest"),
        an.get("befehl:availability_report"),
    )
    assert alarme is not None and alarme.trigger == Cron("45 7 * * *")
    assert wochenmail is not None and wochenmail.trigger == Cron("0 8 * * 1"), "montags nach den Benachrichtigungen"
    assert bericht is not None and bericht.args[1] == ["--out", f"{settings.REPORTS_ROOT}/"]
    assert len(an) == len(aus) + 3


@pytest.mark.parametrize(
    ("objektspeicher", "layout", "argumente"),
    [
        (False, "sha256", ["--aufraeumen"]),
        (True, "sha256", ["--hochladen", "--aufraeumen"]),
        (True, "kommune", ["--aufraeumen"]),
    ],
)
def test_dokumentablage_laedt_mit_objektspeicher_vor_dem_aufraeumen_hoch(
    settings: Any, objektspeicher: bool, layout: str, argumente: list[str]
) -> None:
    """Den Zwischenspeicher verlassen nur hochgeladene Inhalte; ohne Ablage nach SHA-256 gibt es nichts hochzuladen."""
    from apps.common import schedules

    settings.OBJ_ENABLED = objektspeicher
    settings.FILE_STORE_LAYOUT = layout
    register = ScheduleRegistry()
    schedules.registrieren(ziel=register)
    eintrag = register.get("befehl:dokumentablage")
    assert eintrag is not None and eintrag.trigger == Cron("50 * * * *")
    assert eintrag.args[1] == argumente


def test_upgrade_hinweis_entfernt_alle_befehle_der_zeitplaene_aus_der_crontab(settings: Any) -> None:
    """Das grep-Muster zum Bereinigen der Crontab (DEPLOYMENT.md) nennt jeden Befehl eines Zeitplans."""
    text = (REPO / "DEPLOYMENT.md").read_text(encoding="utf-8")
    treffer = re.search(r"grep -v -E 'manage\\\.py \(([^)]*)\)'", text)
    assert treffer, "Befehl zum Bereinigen der Crontab fehlt in DEPLOYMENT.md"
    assert set(treffer.group(1).split("|")) == _alle_befehle(settings)


#: Crontab-Zeile mit Django-Befehl: fünf Zeitfelder (oder @hourly …), danach manage.py
CRON_MIT_DJANGO = re.compile(r"^\s*(?:(?:[\d*/,\-]+\s+){5}|@\w+\s+).*manage\.py\s")


def test_beispiel_crontabs_enthalten_nur_aufgaben_des_betriebssystems() -> None:
    """ADR „Aufträge und Zeitpläne“: Django-Befehle laufen als Zeitpläne, nicht per Host-Cron."""
    dateien = [REPO / ".env.example", *REPO.glob("*.md"), *(REPO / "docs").rglob("*.md"), *(REPO / "deploy").rglob("*")]
    funde = [
        f"{datei.relative_to(REPO)}: {zeile.strip()}"
        for datei in dateien
        if datei.is_file() and datei.suffix in {".md", ".yml", ".yaml", ".sh", ".example", ".txt", ""}
        for zeile in datei.read_text(encoding="utf-8", errors="replace").splitlines()
        if CRON_MIT_DJANGO.match(zeile)
    ]
    assert funde == []


def test_crontab_muster_erkennt_django_befehle() -> None:
    assert CRON_MIT_DJANGO.match("40 * * * * docker exec mandari python manage.py cache_files --limit 400")
    assert CRON_MIT_DJANGO.match("0 7 * * * cd /opt/mandari && docker compose exec -T mandari python manage.py x >> l")
    assert CRON_MIT_DJANGO.match("@hourly docker exec mandari python manage.py check_source_health")
    assert not CRON_MIT_DJANGO.match("0 2 * * * cd /opt/mandari && ./backup.sh --quiet >> backup.log 2>&1")
    assert not CRON_MIT_DJANGO.match("docker compose exec mandari python manage.py audit_chain_backfill")


@pytest.mark.django_db
def test_handaufrufe_zur_kontrolle_laufen_neben_dem_zeitplan(monkeypatch: pytest.MonkeyPatch) -> None:
    """cache_files: der alte Cron-Eintrag überspringt, die Statistik läuft (Issue #516)."""
    monkeypatch.delenv(verwaltungsbefehle.AUS_ZEITPLAN_ENV, raising=False)
    presence.announce("worker", ["scheduler", "tasks"], [])

    aus, fehler = StringIO(), StringIO()
    call_command("cache_files", "--limit", "400", stdout=aus, stderr=fehler)
    assert "läuft als Zeitplan im Worker – Aufruf übersprungen" in fehler.getvalue()
    assert aus.getvalue() == ""

    aus, fehler = StringIO(), StringIO()
    call_command("cache_files", "--stats", stdout=aus, stderr=fehler)
    assert "Aufruf übersprungen" not in fehler.getvalue()
    assert "Cache " in aus.getvalue()


def test_berichtsoptionen_der_betriebspruefungen_aendern_nichts() -> None:
    from django.core.management import get_commands, load_command_class

    def befehl(name: str) -> Any:
        return load_command_class(get_commands()[name], name)

    assert befehl("check_source_health").liest_nur({"report": True})
    assert befehl("check_service_levels").liest_nur({"report": True})
    assert not befehl("check_service_levels").liest_nur({"report": False})
    assert befehl("availability_report").liest_nur({"out": None}), "nur Ausgabe auf stdout"
    assert not befehl("availability_report").liest_nur({"out": "/berichte/"})
    assert befehl("cache_files").liest_nur({"stats": True})
    assert befehl("loeschabgleich").liest_nur({"robots": True})
    assert not befehl("loeschabgleich").liest_nur({"robots": False, "nur_loeschen": True})
    assert befehl("dokumentablage").liest_nur({"aufraeumen": False}), "nur Kennzahlen"
    assert not befehl("dokumentablage").liest_nur({"aufraeumen": True})
    assert not befehl("send_session_reminders").liest_nur({"tenant": "musterstadt"})
