# SPDX-License-Identifier: AGPL-3.0-or-later
"""``list-sources`` listet die registrierten Quellen, ``list-bodies`` die Kommunen (Issue #615)."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from src import main as cli
from src.client.oparl_client import ERROR_KIND_UA_BLOCKED
from src.sync.orchestrator import source_status_label

JETZT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _quelle(name: str, **felder: Any) -> SimpleNamespace:
    werte: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": name,
        "url": f"https://ris.example.org/{name.lower()}/oparl/system",
        "is_active": True,
        "last_sync": None,
        "last_full_sync": None,
        "last_error_kind": None,
        "last_error_at": None,
        "consecutive_failures": 0,
    }
    werte.update(felder)
    return SimpleNamespace(**werte)


def _kommune(name: str, source_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        source_id=source_id,
        external_id=f"https://ris.example.org/bodies/{name.lower()}",
        last_sync=None,
        classification="Stadt",
    )


class _Speicher:
    def __init__(self, quellen: list[SimpleNamespace], kommunen: list[SimpleNamespace]) -> None:
        self.quellen = quellen
        self.kommunen = kommunen
        self.active_only: bool | None = None

    async def get_all_sources(self, active_only: bool = True) -> list[SimpleNamespace]:
        self.active_only = active_only
        return [q for q in self.quellen if q.is_active or not active_only]

    async def get_all_bodies(self) -> list[SimpleNamespace]:
        return self.kommunen


def _cli_mit_speicher(monkeypatch: pytest.MonkeyPatch, speicher: _Speicher) -> io.StringIO:
    class FakeOrchestrator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.storage = speicher

        async def __aenter__(self) -> FakeOrchestrator:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    ausgabe = io.StringIO()
    monkeypatch.setattr(cli, "SyncOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(cli, "console", Console(file=ausgabe, width=250))
    return ausgabe


def test_list_sources_zeigt_quellen_statt_kommunen(monkeypatch: pytest.MonkeyPatch) -> None:
    aktiv = _quelle("Musterstadt", last_sync=datetime(2026, 9, 29, 11, 50, tzinfo=UTC))
    inaktiv = _quelle("Ruhestadt", is_active=False)
    speicher = _Speicher(
        [aktiv, inaktiv],
        [_kommune("Kommune Nord", aktiv.id), _kommune("Kommune Sued", aktiv.id)],
    )
    ausgabe = _cli_mit_speicher(monkeypatch, speicher)

    result = CliRunner().invoke(cli.app, ["list-sources"])

    assert result.exit_code == 0, result.output
    text = ausgabe.getvalue()
    assert speicher.active_only is False, "auch inaktive Quellen gehören in die Übersicht"
    assert "Registrierte Quellen" in text
    zeile_aktiv = next(z for z in text.splitlines() if "Musterstadt" in z)
    assert aktiv.url in zeile_aktiv
    assert " aktiv " in zeile_aktiv
    assert "2026-09-29 11:50" in zeile_aktiv
    assert zeile_aktiv.rstrip(" │|").endswith("2")
    zeile_inaktiv = next(z for z in text.splitlines() if "Ruhestadt" in z)
    assert " inaktiv " in zeile_inaktiv
    assert " nie " in zeile_inaktiv
    # Kommunen stehen nicht in der Quellenliste
    assert "Kommune Nord" not in text


def test_list_sources_ohne_quellen(monkeypatch: pytest.MonkeyPatch) -> None:
    ausgabe = _cli_mit_speicher(monkeypatch, _Speicher([], []))

    result = CliRunner().invoke(cli.app, ["list-sources"])

    assert result.exit_code == 0, result.output
    assert "Noch keine Quelle registriert" in ausgabe.getvalue()


def test_list_bodies_zeigt_kommunen(monkeypatch: pytest.MonkeyPatch) -> None:
    quelle = _quelle("Musterstadt")
    ausgabe = _cli_mit_speicher(monkeypatch, _Speicher([quelle], [_kommune("Kommune Nord", quelle.id)]))

    result = CliRunner().invoke(cli.app, ["list-bodies"])

    assert result.exit_code == 0, result.output
    text = ausgabe.getvalue()
    assert "Kommune Nord" in text
    assert "https://ris.example.org/bodies/kommune nord" in text
    assert quelle.url not in text


@pytest.mark.parametrize(
    ("felder", "erwartet"),
    [
        ({}, "aktiv"),
        ({"is_active": False, "consecutive_failures": 5}, "inaktiv"),
        ({"consecutive_failures": 1, "last_error_at": JETZT}, "Fehler (1 Fehlversuch in Folge)"),
        (
            {"consecutive_failures": 1, "last_error_kind": ERROR_KIND_UA_BLOCKED, "last_error_at": JETZT},
            "Schonung bis 29.09. 13:00 UTC (User-Agent gesperrt)",
        ),
        (
            {"consecutive_failures": 3, "last_error_at": JETZT - timedelta(minutes=5)},
            "Schonung bis 29.09. 12:05 UTC (3 Fehlversuche in Folge)",
        ),
    ],
)
def test_source_status_label(felder: dict[str, Any], erwartet: str) -> None:
    assert source_status_label(_quelle("Stadt", **felder), JETZT) == erwartet
