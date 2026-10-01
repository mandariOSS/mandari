# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SQLAlchemy-Spans je Anweisung über den Startweg der CLI (Issue #699).

Die CLI lädt ``src.main`` – und damit Orchestrator und Speicher – bevor der Typer-Callback
``setup_opentelemetry()`` aufruft. Die Instrumentierung ersetzt ``create_async_engine`` im Modul
``sqlalchemy.ext.asyncio``; früh gebundene Namen zeigten weiter auf das Original. Die Engines des Ingestors
lieferten deshalb nur ``connect``-Spans, obwohl die Startmeldung ``sqlalchemy`` nannte.

Jeder Lauf in einem frischen Interpreter: Die Instrumentierung verändert die Bibliotheken prozessweit.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

INGESTOR = Path(__file__).resolve().parents[1]
DATABASE_URL = os.environ.get("INGESTOR_TEST_DATABASE_URL", "")

SQLALCHEMY_SPANS = "opentelemetry.instrumentation.sqlalchemy"
ASYNCPG_SPANS = "opentelemetry.instrumentation.asyncpg"

STARTWEG = """
import asyncio
import os

# 1. Wie die CLI: erst src.main laden; dabei binden Orchestrator und Speicher ihre Importe. Die Daemon-Führung
#    lädt der Befehl "daemon" heute erst später – hier schon vorher, damit eine neue Importreihenfolge nichts ändert.
import src.main as cli
import src.scheduler.singleton

# 2. Dann das Tracing einrichten wie der CLI-Callback. Nur der OTLP-Exporter weicht einem In-Memory-Exporter.
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http import trace_exporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
trace_exporter.OTLPSpanExporter = lambda: exporter
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://otel-collector.invalid:4318"
cli.main()

# 3. Die Engines so erzeugen, wie der Ingestor es tut
from sqlalchemy import text

from src.scheduler.singleton import Fuehrung
from src.storage.database import DatabaseStorage

datenbank = os.environ.get("STARTWEG_DATENBANK", "")
# Ohne Datenbank: ein Port, an dem niemand lauscht – die Führung erzeugt ihre Engine erst beim Versuch
url = datenbank or "postgresql+asyncpg://nutzer:kennwort@localhost:1/mandari"


def lauscher(engine):
    # Die Instrumentierung hängt sich mit before_cursor_execute an die Engine; der Ingestor selbst tut das nicht
    return bool(engine.sync_engine.dispatch.before_cursor_execute)


async def ablauf():
    storage = DatabaseStorage(url)
    print("LAUSCHER speicher", lauscher(storage._engine))
    fuehrung = Fuehrung(url)
    try:
        await asyncio.wait_for(fuehrung.versuchen(), timeout=30)
    except Exception as fehler:
        if datenbank:
            raise
        print("OHNE DATENBANK", type(fehler).__name__)
    print("LAUSCHER fuehrung", lauscher(fuehrung._engine))
    if datenbank:
        async with storage._engine.connect() as verbindung:
            await verbindung.execute(text("SELECT 1 AS speicher_im_startweg"))
    await fuehrung.freigeben()
    await storage.close()


asyncio.run(ablauf())
trace.get_tracer_provider().force_flush()
for span in exporter.get_finished_spans():
    attribute = span.attributes or {}
    anweisung = attribute.get("db.statement") or attribute.get("db.query.text") or ""
    print("SPAN", span.instrumentation_scope.name, "|", " ".join(str(anweisung).split()))
"""


def _startweg(datenbank: str = "") -> tuple[str, str]:
    umgebung = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "STARTWEG_"))}
    umgebung.update({"LOG_FORMAT": "text", "LOG_LEVEL": "INFO", "PYTHONIOENCODING": "utf-8"})
    if datenbank:
        umgebung["STARTWEG_DATENBANK"] = datenbank
    ergebnis = subprocess.run(  # noqa: S603 – fester Aufruf im Test
        [sys.executable, "-c", STARTWEG],
        cwd=INGESTOR,
        env=umgebung,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    ausgabe = ergebnis.stdout + ergebnis.stderr
    assert ergebnis.returncode == 0, ausgabe[-3000:]
    return ergebnis.stdout, ausgabe


def _spans(stdout: str, scope: str) -> list[str]:
    praefix = f"SPAN {scope} | "
    return [zeile.removeprefix(praefix) for zeile in stdout.splitlines() if zeile.startswith(praefix)]


def test_engines_des_ingestors_sind_nach_dem_startweg_instrumentiert() -> None:
    """Ohne Datenbank: Beide Engines tragen die Listener der Instrumentierung, die Startmeldung nennt sie."""
    stdout, ausgabe = _startweg()

    assert "OpenTelemetry aktiv" in ausgabe, ausgabe[-3000:]
    assert ": httpx, asyncpg, sqlalchemy" in ausgabe, ausgabe[-3000:]
    assert "LAUSCHER speicher True" in stdout, ausgabe[-3000:]
    assert "LAUSCHER fuehrung True" in stdout, ausgabe[-3000:]


def test_sqlalchemy_liefert_spans_je_anweisung_ueber_den_startweg_der_cli() -> None:
    """Gegen PostgreSQL: je Anweisung ein SQLAlchemy-Span (und ein asyncpg-Span), für Speicher und Führung."""
    if not DATABASE_URL:
        if os.environ.get("CI"):
            pytest.fail("INGESTOR_TEST_DATABASE_URL fehlt: In der CI muss dieser Test laufen.")
        pytest.skip("braucht PostgreSQL (INGESTOR_TEST_DATABASE_URL)")

    stdout, ausgabe = _startweg(DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1))

    assert ": httpx, asyncpg, sqlalchemy" in ausgabe, ausgabe[-3000:]
    sqlalchemy_spans = _spans(stdout, SQLALCHEMY_SPANS)
    asyncpg_spans = _spans(stdout, ASYNCPG_SPANS)
    for erwartet in ("speicher_im_startweg", "pg_try_advisory_lock", "pg_advisory_unlock"):
        assert any(erwartet in anweisung for anweisung in sqlalchemy_spans), (erwartet, ausgabe[-3000:])
        assert any(erwartet in anweisung for anweisung in asyncpg_spans), (erwartet, ausgabe[-3000:])


def test_kein_frueh_gebundener_engine_import_im_quellcode() -> None:
    """Neue Engines laufen über ``src.storage.engine.engine_erzeugen`` – sonst fehlen ihre Spans wieder."""
    verboten = {"create_async_engine", "create_engine"}
    funde: list[str] = []
    for datei in sorted((INGESTOR / "src").rglob("*.py")):
        baum = ast.parse(datei.read_text(encoding="utf-8"), filename=str(datei))
        for knoten in ast.walk(baum):
            if (
                isinstance(knoten, ast.ImportFrom)
                and (knoten.module or "").startswith("sqlalchemy")
                and any(alias.name in verboten for alias in knoten.names)
            ):
                funde.append(f"{datei.relative_to(INGESTOR).as_posix()}:{knoten.lineno}")
    assert funde == [], f"Engine über src.storage.engine.engine_erzeugen() erzeugen: {funde}"
