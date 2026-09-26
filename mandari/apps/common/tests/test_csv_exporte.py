# SPDX-License-Identifier: AGPL-3.0-or-later
"""
CSV-Exporte schreiben jede Zelle über den gemeinsamen Schutz gegen Formel-Injektion
(``apps.common.csv_safety``): Zellen, die mit ``=``, ``+``, ``-``, ``@``, Tabulator oder
Wagenrücklauf beginnen, werden als Text ausgegeben. Reine Zahlen bleiben Zahlen.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apps.common import csv_safety

APP_ROOT = Path(__file__).resolve().parents[3]
BOESE = '=HYPERLINK("https://example.invalid";"x")'


def _zellen(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))


# ---------------------------------------------------------------- gemeinsamer Helfer


@pytest.mark.parametrize("wert", ["=1+1", "+1+1", "-1+1", "@SUMME(A1)", "\t=1", "\r=1", BOESE])
def test_formelanfaenge_werden_text(wert: str) -> None:
    assert csv_safety.csv_safe_cell(wert) == "'" + wert


@pytest.mark.parametrize("wert", ["-12,50", "+3", "-7", "-0.5", "1.234,56"])
def test_reine_zahlen_bleiben_zahlen(wert: str) -> None:
    assert csv_safety.csv_safe_cell(wert) == wert


@pytest.mark.parametrize("wert", [-5, Decimal("-12.50"), -0.25, 0, True, None])
def test_zahlentypen_unveraendert(wert: Any) -> None:
    assert csv_safety.csv_safe_cell(wert) == ("" if wert is None else str(wert))


def test_writer_schuetzt_jede_zelle() -> None:
    puffer = io.StringIO()
    schreiber = csv_safety.writer(puffer, delimiter=";", lineterminator="\r\n")
    schreiber.writerow(["Name", BOESE, -3, "-3,00"])
    schreiber.writerows([["@x", "ok"]])
    assert _zellen(puffer.getvalue()) == [["Name", "'" + BOESE, "-3", "-3,00"], ["'@x", "ok"]]


def test_doppelte_anwendung_aendert_nichts() -> None:
    einmal = csv_safety.csv_safe_cell(BOESE)
    assert csv_safety.csv_safe_cell(einmal) == einmal


# ---------------------------------------------------------------- alle Exporte nutzen den Helfer


def test_kein_export_schreibt_csv_am_schutz_vorbei() -> None:
    """Jede CSV-Ausgabe der Anwendung läuft über ``csv_safety.writer``."""
    muster = re.compile(r"\bcsv\.(writer|DictWriter)\(")
    treffer = []
    for pfad in APP_ROOT.rglob("*.py"):
        teile = set(pfad.relative_to(APP_ROOT).parts)
        if teile & {"tests", "migrations", "node_modules", "static", "tests_e2e"} or pfad.name == "csv_safety.py":
            continue
        text = pfad.read_text(encoding="utf-8", errors="replace")
        treffer += [f"{pfad.relative_to(APP_ROOT)}" for _ in muster.finditer(text)]
    assert treffer == []


# ---------------------------------------------------------------- einzelne Exporte


def _person(name: str) -> Any:
    return SimpleNamespace(
        display_name=name,
        get_bank_account_holder_decrypted=lambda: "=Inhaber",
        get_bank_iban_decrypted=lambda: "DE00",
        get_bank_bic_decrypted=lambda: "",
    )


def test_sitzungsgeld_jahresuebersicht() -> None:
    from apps.session.services.allowance_service import year_summary_csv

    zeilen = [
        {
            "person": _person(BOESE),
            "count": 1,
            "total": Decimal("10"),
            "paid": Decimal("0"),
            "approved": Decimal("0"),
            "pending": Decimal("10"),
        }
    ]
    zellen = _zellen(year_summary_csv(zeilen, 2026))
    assert zellen[1][1] == "'" + BOESE
    assert zellen[1][3] == "10,00"


@pytest.mark.django_db
def test_aufgaben_export_und_reimport(org: Any, make_member: Any) -> None:
    from apps.work.tasks.export_service import render_csv
    from apps.work.tasks.import_service import rows_from_csv
    from apps.work.tasks.models import Task

    mitglied = make_member(org, ["tasks.view"], email="aufgaben@example.org")
    aufgabe = Task.objects.create(organization=org, title=BOESE, description="-Notiz", created_by=mitglied)

    text, _typ = render_csv([aufgabe])
    zeile = _zellen(text)[1]
    assert zeile[1] == "'" + BOESE
    assert zeile[2] == "'-Notiz"
    # Eigener Export lässt sich unverändert wieder einlesen
    zeilen, fehler = rows_from_csv(text)
    assert fehler == []
    assert zeilen[0]["title"] == BOESE


def test_monatspauschalen_export() -> None:
    from datetime import date

    from apps.session.services.allowance_service import build_monthly_export_csv

    pauschale = SimpleNamespace(
        person=_person(BOESE),
        rate=SimpleNamespace(name="+Pauschale", legal_basis="@Satzung"),
        period=date(2026, 9, 1),
        amount=Decimal("25"),
        get_status_display=lambda: "Genehmigt",
        export_reference="-REF",
    )
    zellen = _zellen(build_monthly_export_csv([pauschale]))
    assert zellen[1][:4] == ["'" + BOESE, "'+Pauschale", "'@Satzung", "09/2026"]
    assert zellen[1][6] == "'=Inhaber"
    assert zellen[1][-1] == "'-REF"
