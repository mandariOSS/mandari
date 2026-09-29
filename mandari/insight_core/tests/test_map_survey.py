# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kartenanalyse über den Dateicache (Issue #599): nur lokale, öffentliche Dateien, kein Netz,
nichts gespeichert; Kennzahlen, Stichprobe und Auswertung der Prüfung von Hand.
"""

from __future__ import annotations

import csv
import json
import uuid
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.common.tests.pdf_samples import map_pdf, photo_pdf, text_pdf
from insight_core.models import OParlBody, OParlFile, Street
from insight_core.services.map_survey import evaluate_labels, survey_body

pytestmark = pytest.mark.django_db

STREETS = ("Roxeler Straße", "Dieckmannstraße", "Gievenbecker Reihe", "Niedenstiege")


@pytest.fixture(autouse=True)
def _kein_netz(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Netzabruf in der Kartenanalyse")

    monkeypatch.setattr(httpx.Client, "send", _blocked)


@pytest.fixture
def cached(geo_body: OParlBody, tmp_path: Path) -> Callable[..., OParlFile]:
    """cached(name, daten, **felder) → Datei der Kommune mit lokaler Kopie im Dateicache."""

    def _make(name: str, data: bytes | None, **fields: Any) -> OParlFile:
        path = tmp_path / name
        if data is not None:
            path.write_bytes(data)
        fields.setdefault("mime_type", "application/pdf")
        return OParlFile.objects.create(
            external_id=f"https://ris.beispielstadt.example/oparl/files/{uuid.uuid4()}",
            body=geo_body,
            name=name,
            file_name=name,
            local_path=str(path),
            **fields,
        )

    return _make


@pytest.fixture
def cache_with_files(cached: Callable[..., OParlFile], make_street: Callable[..., Street], geo_body: OParlBody) -> None:
    for index, name in enumerate(STREETS):
        make_street(geo_body, name, 51.96 + index * 0.001, 7.62)
    cached(
        "lageplan.pdf",
        map_pdf(text="Lageplan Maßstab 1:500 Roxeler Straße Dieckmannstraße Gievenbecker Reihe Niedenstiege"),
    )
    cached("begruendung.pdf", text_pdf())
    cached("foto.pdf", photo_pdf())
    cached("fehlt.pdf", None)  # nicht (mehr) im Cache
    cached("tabelle.docx", b"PK\x03\x04", mime_type="application/vnd.openxmlformats")
    cached("kaputt.pdf", b"%PDF-1.4 kaputt")
    cached("geloescht.pdf", map_pdf(), deleted=True)


def test_survey_counts_maps_from_local_cache_only(geo_body: OParlBody, cache_with_files: None) -> None:
    survey = survey_body(geo_body, sample_size=4)
    summary = survey.summary()

    assert summary["dateien"] == 3
    assert summary["seiten"] == 3
    assert summary["kartenseiten"] == 1
    assert summary["kartenseiten_mit_massstab"] == 1
    assert summary["kartenseiten_mit_koordinaten"] == 1
    assert summary["kartenseiten_mit_4_strassen"] == 1
    assert summary["scanseiten_ohne_text"] == 1
    assert summary["uebersprungen"] == {"nicht im Cache": 1, "kein PDF": 1, "nicht lesbar": 1}
    rows = survey.sample_rows()
    assert sorted(row["predicted"] for row in rows) == ["ja", "nein", "nein"]
    map_row = next(row for row in rows if row["predicted"] == "ja")
    assert map_row["scales"] == "1:500" and map_row["streets"] == 4
    assert {row["weight"] for row in rows} == {1.0}


def test_command_requires_dry_run(geo_body: OParlBody) -> None:
    with pytest.raises(CommandError, match="--dry-run"):
        call_command("analyze_maps", "--body", "beispielstadt", stdout=StringIO())


def test_command_prints_statistics_writes_json_and_sample(
    geo_body: OParlBody, cache_with_files: None, tmp_path: Path
) -> None:
    out = StringIO()
    json_path = tmp_path / "karten.json"
    sample_path = tmp_path / "stichprobe.csv"
    call_command(
        "analyze_maps",
        "--body",
        "beispielstadt",
        "--dry-run",
        "--json",
        str(json_path),
        "--sample-csv",
        str(sample_path),
        "--sample-size",
        "10",
        stdout=out,
    )
    text = out.getvalue()

    assert "Seiten: 3, davon Karten/Pläne: 1 (33,3 %)" in text
    assert "Kartenseiten mit Maßstab im PDF (/VP): 1 (100,0 %)" in text
    assert "Kartenseiten mit ≥ 4 Straßennamen: 1 (100,0 %)" in text
    assert json.loads(json_path.read_text(encoding="utf-8"))["kartenseiten"] == 1
    with sample_path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    assert len(rows) == 3 and all(row["label"] == "" for row in rows)
    assert any(row["reasons"].startswith("'+2") for row in rows)  # Zellen gegen Formel-Injektion geschützt
    assert not OParlFile.objects.filter(text_extraction_status="completed").exists()  # nichts gespeichert


def test_evaluate_hand_labels(tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    rows = [
        {"file_id": "a", "page": 1, "predicted": "ja", "weight": 1, "label": "ja"},
        {"file_id": "b", "page": 1, "predicted": "ja", "weight": 1, "label": "nein"},
        {"file_id": "c", "page": 1, "predicted": "nein", "weight": 10, "label": "ja"},
        {"file_id": "d", "page": 1, "predicted": "nein", "weight": 10, "label": "nein"},
        {"file_id": "e", "page": 2, "predicted": "nein", "weight": 10, "label": ""},
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)

    result = evaluate_labels(path)

    assert result.labeled == 4
    assert (result.precision, result.recall) == (0.5, 0.5)
    assert result.weighted_recall == pytest.approx(1 / 11, abs=0.001)
    assert result.false_positives == ["b S. 1"] and result.false_negatives == ["c S. 1"]

    out = StringIO()
    call_command("analyze_maps", "--evaluate", str(path), stdout=out)
    assert "Präzision: 0.5  Trefferquote: 0.5 (Stichprobe)" in out.getvalue()
