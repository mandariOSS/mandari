# SPDX-License-Identifier: AGPL-3.0-or-later
"""Logging und Tracing werden vor jedem CLI-Befehl eingerichtet (Issue #426).

Früher gab es zwei ``@app.callback()``. Typer behält nur den zuletzt registrierten, sodass der Callback mit
``setup_logging()``/``setup_opentelemetry()`` nie lief: Der Ingestor schrieb keine JSON-Logs und keine Traces.
"""

from __future__ import annotations

import logging

import pytest
from typer.testing import CliRunner

from src import main as cli
from src import observability


def _befehl_ohne_seiteneffekte() -> int:
    """``probe-ris`` ohne URL bricht nach dem Callback sofort mit Exit-Code 1 ab (kein Netz, keine Datenbank)."""
    return CliRunner().invoke(cli.app, ["probe-ris"]).exit_code


def test_callback_laeuft_vor_dem_befehl(monkeypatch: pytest.MonkeyPatch) -> None:
    aufrufe: list[str] = []

    def tracing() -> bool:
        aufrufe.append("tracing")
        return False

    monkeypatch.setattr(observability, "setup_logging", lambda: aufrufe.append("logging"))
    monkeypatch.setattr(observability, "setup_opentelemetry", tracing)

    assert _befehl_ohne_seiteneffekte() == 1
    assert aufrufe == ["logging", "tracing"]


def test_cli_schreibt_json_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(observability, "_logging_ready", False)

    assert _befehl_ohne_seiteneffekte() == 1

    root = logging.getLogger()
    assert root.level == logging.INFO
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, observability.JsonFormatter)
