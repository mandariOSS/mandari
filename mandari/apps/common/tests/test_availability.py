# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verfügbarkeitsbericht (Issue #231): Gatus-API gemockt; Störungen aus Ereignissen, Zuschnitt auf
den Monat, Bericht mit Zahlen; ohne erreichbare Statusseite Exit 1 (CommandError).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command

from apps.common import availability as av
from apps.common.management.commands.availability_report import vormonat

BASIS = "https://status.example"
JETZT = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)

STATUSES = [
    {
        "name": "Bürgerportal",
        "group": "mandari",
        "key": "mandari_buergerportal",
        "results": [],
        "events": [
            {"type": "START", "timestamp": "2026-07-01T00:00:00Z"},
            {"type": "HEALTHY", "timestamp": "2026-07-01T00:00:30Z"},
            # Störung über den Monatswechsel hinweg: nur der August-Anteil zählt (30 min)
            {"type": "UNHEALTHY", "timestamp": "2026-07-31T23:30:00Z"},
            {"type": "HEALTHY", "timestamp": "2026-08-01T00:30:00Z"},
            {"type": "UNHEALTHY", "timestamp": "2026-08-15T10:00:00Z"},
            {"type": "HEALTHY", "timestamp": "2026-08-15T11:30:00Z"},
        ],
    },
    {
        "name": "OParl-API",
        "group": "mandari",
        "key": "mandari_oparl-api",
        # keine Ereignisse: Ersatz aus den Einzelergebnissen
        "results": [
            {"success": True, "timestamp": "2026-08-20T10:00:00Z"},
            {"success": False, "timestamp": "2026-08-20T10:01:00Z"},
            {"success": False, "timestamp": "2026-08-20T10:02:00Z"},
            {"success": True, "timestamp": "2026-08-20T10:05:00Z"},
        ],
    },
]
UPTIMES = {"mandari_buergerportal": b"0.998750", "mandari_oparl-api": b"1.000000"}


@pytest.fixture
def gatus(monkeypatch: pytest.MonkeyPatch) -> None:
    def abrufen(url: str, timeout: float) -> bytes:
        assert url.startswith(BASIS)
        if "/statuses" in url:
            return json.dumps(STATUSES).encode()
        key = url.split("/endpoints/")[1].split("/")[0]
        return UPTIMES[key]

    monkeypatch.setattr(av, "_abrufen", abrufen)


def test_zeitstempel_mit_neun_nachkommastellen() -> None:
    assert av.zeitstempel("2026-08-01T00:30:00.123456789Z") == datetime(2026, 8, 1, 0, 30, 0, 123456, tzinfo=UTC)


def test_monatsgrenzen() -> None:
    assert av.monatsgrenzen("2026-02") == (datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC))


def test_vormonat() -> None:
    assert vormonat(datetime(2026, 1, 15, tzinfo=UTC)) == "2025-12"
    assert vormonat(datetime(2026, 9, 1, tzinfo=UTC)) == "2026-08"


def test_stoerungen_aus_ereignissen_auf_monat_zugeschnitten(gatus: None) -> None:
    dienste = av.sammle_dienste(BASIS, "2026-08", jetzt=JETZT)

    portal, oparl = dienste
    assert portal.uptime_30d == pytest.approx(0.99875)
    assert [s.dauer for s in portal.stoerungen] == [timedelta(minutes=30), timedelta(minutes=90)]
    assert portal.ausfallzeit == timedelta(hours=2)
    # Ersatz aus Einzelergebnissen: 10:01 bis 10:05
    assert [s.dauer for s in oparl.stoerungen] == [timedelta(minutes=4)]


def test_offene_stoerung_endet_bei_jetzt_oder_monatsende() -> None:
    status = {"events": [{"type": "UNHEALTHY", "timestamp": "2026-08-31T22:00:00Z"}]}
    beginn, ende = av.monatsgrenzen("2026-08")

    [stoerung] = av.stoerungen_im_monat(status, beginn, ende, JETZT)

    assert stoerung.ende == ende
    assert stoerung.dauer == timedelta(hours=2)


def test_bericht_enthaelt_zahlen(gatus: None) -> None:
    dienste = av.sammle_dienste(BASIS, "2026-08", jetzt=JETZT)

    bericht = av.bericht_markdown("2026-08", dienste, ziel=99.5, quelle=BASIS)

    assert "# Verfügbarkeitsbericht 2026-08" in bericht
    assert "Zielwert: 99.5 % je Dienst" in bericht
    assert "| Bürgerportal | mandari | 99.875 % | ja | 2 | 2 h 00 min | 99.731 % |" in bericht
    assert "| OParl-API | mandari | 100.000 % | ja | 1 | 4 min |" in bericht
    assert "Gesamtverfügbarkeit (Mittel über alle Dienste, letzte 30 Tage): **99.938 %**" in bericht
    assert "Störungen im Monat: 3, Ausfallzeit zusammen 2 h 04 min" in bericht
    assert "| Bürgerportal | 15.08.2026 10:00 | 15.08.2026 11:30 | 1 h 30 min |" in bericht
    assert "je Mandant" in bericht


def test_ziel_verfehlt_wird_markiert() -> None:
    dienst = av.Dienst("Work", "", "work", uptime_30d=0.99)

    assert "| **nein** |" in av.bericht_markdown("2026-08", [dienst])


def test_command_schreibt_datei(gatus: None, tmp_path: Path) -> None:
    ziel = tmp_path / "bericht.md"
    out = StringIO()

    call_command("availability_report", "--month", "2026-08", "--gatus-url", BASIS, "--out", str(ziel), stdout=out)

    assert "2 Dienste" in out.getvalue()
    assert "99.875 %" in ziel.read_text(encoding="utf-8")


def test_command_ohne_statusseite_bricht_ab(monkeypatch: pytest.MonkeyPatch) -> None:
    def weg(url: str, timeout: float) -> bytes:
        raise av.GatusError(f"Statusseite nicht erreichbar ({url})")

    monkeypatch.setattr(av, "_abrufen", weg)

    with pytest.raises(CommandError, match="nicht erreichbar"):
        call_command("availability_report", "--month", "2026-08", "--gatus-url", BASIS)


def test_command_ohne_url_bricht_ab(settings: Any) -> None:
    settings.GATUS_URL = ""

    with pytest.raises(CommandError, match="GATUS_URL"):
        call_command("availability_report", "--month", "2026-08")


def test_command_prueft_monatsformat() -> None:
    with pytest.raises(CommandError, match="YYYY-MM"):
        call_command("availability_report", "--month", "August", "--gatus-url", BASIS)
