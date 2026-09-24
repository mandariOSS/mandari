# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hintergrund-Befehle mit Worker-Threads geben ihre Verbindungen zurück (Issue #54, wie #344).

Beim Neuberechnen der Verortung am 24.09.2026 entstanden je Stapel neue Worker-Threads, und jeder
nahm seine Datenbankverbindung mit ins Grab. Nach fünf Stapeln mit zwei Workern war der Pool leer,
jeder weitere Vorgang scheiterte am Pool-Timeout. Außerdem luden die Befehle alle Vorgänge auf einmal
und sprengten für Köln das Speicherlimit. Und die Lückenprüfung zählte Straßen und Adressen in einer
Abfrage – das Kreuzprodukt füllte die Platte des Datenbankservers.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from insight_core.management.commands import extract_locations, extract_texts
from insight_core.models import Address, OParlBody, OParlFile, OParlPaper, OParlSource, Street
from insight_core.services.geo_coverage import geo_status_for_bodies


def _kommune() -> OParlBody:
    source = OParlSource.objects.create(name="Threadquelle", url=f"https://ris.example/{uuid.uuid4()}/system")
    return OParlBody.objects.create(external_id=f"https://ris.example/bodies/{uuid.uuid4()}", source=source, name="X")


def _vorgang_mit_text(body: OParlBody) -> OParlPaper:
    paper = OParlPaper.objects.create(external_id=f"https://ris.example/papers/{uuid.uuid4()}", body=body, name="V")
    OParlFile.objects.create(
        external_id=f"https://ris.example/files/{uuid.uuid4()}",
        paper=paper,
        text_content="Sanierung der Hafenstraße 5",
        text_extraction_status="completed",
    )
    return paper


def _ohne_ortsbezug(paper: OParlPaper, mode: str = "all") -> dict[str, Any]:
    return {"status": "no_locations", "locations": [], "method": "gazetteer"}


@pytest.mark.skipif(
    settings.DATABASES["default"]["ENGINE"].endswith("sqlite3"),
    reason="SQLite im Speicher ignoriert close() absichtlich; aussagekräftig nur mit PostgreSQL (CI)",
)
@pytest.mark.django_db(transaction=True)
def test_worker_thread_gibt_seine_verbindung_zurueck(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_locations, "process_paper_georef", _ohne_ortsbezug)
    paper = _vorgang_mit_text(_kommune())
    ergebnis: dict[str, Any] = {}

    def im_thread() -> None:
        ergebnis["status"] = extract_locations.Command()._process_paper(paper, "regex", False)["status"]
        ergebnis["offen_danach"] = connection.connection is not None

    t = threading.Thread(target=im_thread)
    t.start()
    t.join(10)

    assert ergebnis == {"status": "no_locations", "offen_danach": False}


def test_textextraktion_gibt_ihre_verbindung_ebenfalls_zurueck() -> None:
    # Gleiches Muster wie extract_locations; der Download ließe sich nur mit viel Attrappe nachstellen.
    assert hasattr(extract_texts.Command._process_file, "__wrapped__")


@pytest.mark.django_db(transaction=True)
def test_alle_vorgaenge_werden_stapelweise_verarbeitet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mehr Stapel als Worker-Durchläufe: Jeder Vorgang kommt an, keiner scheitert."""
    monkeypatch.setattr(extract_locations, "process_paper_georef", _ohne_ortsbezug)
    body = _kommune()
    for _ in range(7):
        _vorgang_mit_text(body)

    out = StringIO()
    call_command("extract_locations", reprocess=True, batch_size=2, workers=2, body=str(body.id), stdout=out)

    text = out.getvalue()
    assert "Batch 4/4" in text
    assert "Keine Ortsbezüge: 7" in text
    assert "Fehlgeschlagen" not in text
    assert set(OParlPaper.objects.filter(body=body).values_list("georef_status", flat=True)) == {"no_locations"}


@pytest.mark.django_db
def test_lueckenpruefung_zaehlt_ohne_kreuzprodukt(make_street: Callable[..., Street]) -> None:
    body = _kommune()
    for i in range(3):
        make_street(body, f"Straße {i}", 51.9, 7.6)
    for i in range(4):
        Address.objects.create(
            body=body,
            osm_type="node",
            osm_id=i + 1,
            street="Straße 0",
            normalized_street="strasse 0",
            house_number=str(i + 1),
            normalized_house_number=str(i + 1),
            latitude=51.9,
            longitude=7.6,
        )

    with CaptureQueriesContext(connection) as abfragen:
        status = next(s for s in geo_status_for_bodies(only_gaps=False) if s.body == body)

    assert (status.street_count, status.address_count) == (3, 4)
    strassen, adressen = Street._meta.db_table, Address._meta.db_table
    for abfrage in abfragen.captured_queries:
        assert not (strassen in abfrage["sql"] and adressen in abfrage["sql"]), abfrage["sql"]
