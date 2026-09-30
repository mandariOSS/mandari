# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verbindungs-URLs erscheinen in CLI-Ausgaben und Logs ohne Benutzer und Passwort."""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest
from typer.testing import CliRunner

from src import main as cli
from src.observability import JsonFormatter, TextFormatter
from src.redaction import MaskingConsole, mask_credentials

GEHEIM = "S3hrGeheim"


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("redis://:S3hrGeheim@redis:6379/0", "redis://***@redis:6379/0"),
        ("postgresql+asyncpg://mandari:S3hrGeheim@db:5432/mandari", "postgresql+asyncpg://***@db:5432/mandari"),
        ("http://elastic:S3hr@Geheim@es:9200", "http://***@es:9200"),
        (
            "rediss://redis:6380/0?password=S3hrGeheim&ssl_cert_reqs=none",
            "rediss://redis:6380/0?password=***&ssl_cert_reqs=none",
        ),
        ("host=db user=mandari password=S3hrGeheim", "host=db user=mandari password=***"),
    ],
)
def test_mask_credentials_entfernt_zugangsdaten(eingabe: str, erwartet: str) -> None:
    assert mask_credentials(eingabe) == erwartet


@pytest.mark.parametrize(
    "text",
    [
        "redis://localhost:6379",
        "https://oparl.stadt-muenster.de/system?page=2",
        "mandari-ingestor/0.1.0 (+https://mandari.de; support@mandari.de)",
        "",
    ],
)
def test_mask_credentials_laesst_texte_ohne_zugangsdaten_unveraendert(text: str) -> None:
    assert mask_credentials(text) == text


def test_mask_credentials_in_fehlermeldung_mit_mehreren_urls() -> None:
    meldung = (
        "Client error '401 Unauthorized' for url 'http://elastic:S3hrGeheim@es:9200/papers/_bulk', "
        "Fallback redis://:S3hrGeheim@redis:6379"
    )
    maskiert = mask_credentials(meldung)
    assert GEHEIM not in maskiert
    assert "'http://***@es:9200/papers/_bulk'" in maskiert
    assert "redis://***@redis:6379" in maskiert


def test_status_zeigt_keine_zugangsdaten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.settings, "database_url", f"postgresql+asyncpg://mandari:{GEHEIM}@db:5432/mandari")
    monkeypatch.setattr(cli.settings, "redis_url", f"redis://:{GEHEIM}@redis:6379/0")
    monkeypatch.setattr(cli.settings, "elasticsearch_url", f"http://elastic:{GEHEIM}@es:9200")

    class OhneDatenbank:
        """Verbindungsfehler, dessen Meldung die URL samt Passwort enthält."""

        async def __aenter__(self) -> OhneDatenbank:
            raise ConnectionError(f"cannot connect to redis://:{GEHEIM}@redis:6379/0")

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(cli, "SyncOrchestrator", OhneDatenbank)

    result = CliRunner().invoke(cli.app, ["status"])

    assert result.exit_code == 0, result.output
    assert GEHEIM not in result.output
    assert "Database: postgresql+asyncpg://***@db:5432/mandari" in result.output
    assert "Redis: redis://***@redis:6379/0" in result.output
    assert "Elasticsearch: http://***@es:9200" in result.output
    assert "Could not connect to database" in result.output


def test_masking_console_maskiert_ausgaben() -> None:
    puffer = io.StringIO()
    konsole = MaskingConsole(file=puffer, width=200)
    konsole.print(f"[red]Sync failed: redis://:{GEHEIM}@redis:6379[/red]")
    assert GEHEIM not in puffer.getvalue()
    assert "redis://***@redis:6379" in puffer.getvalue()


def _record_mit_fehler() -> logging.LogRecord:
    try:
        raise ConnectionError(f"http://elastic:{GEHEIM}@es:9200 nicht erreichbar")
    except ConnectionError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "src.test", logging.WARNING, __file__, 1, "Fehler bei %s", (f"redis://:{GEHEIM}@redis:6379",), exc_info
    )
    record.__dict__.update(request_id="-", trace_id="-", span_id="-", ziel=f"redis://:{GEHEIM}@redis:6379")
    return record


def test_json_log_ohne_zugangsdaten() -> None:
    zeile = JsonFormatter().format(_record_mit_fehler())
    assert GEHEIM not in zeile
    payload = json.loads(zeile)
    assert payload["msg"] == "Fehler bei redis://***@redis:6379"
    assert payload["ziel"] == "redis://***@redis:6379"
    assert "http://***@es:9200" in payload["exception"]


def test_text_log_ohne_zugangsdaten() -> None:
    zeile = TextFormatter().format(_record_mit_fehler())
    assert GEHEIM not in zeile
    assert "Fehler bei redis://***@redis:6379" in zeile
    assert "http://***@es:9200 nicht erreichbar" in zeile
