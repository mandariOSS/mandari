# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin-Spalten maskieren Werte aus der Datenbank.

Quellenart und Gesundheitsgründe stammen aus ``sync_config`` bzw. dem Fehlerzustand einer Quelle.
Früher setzte der Admin sie per ``mark_safe(f"…")`` unmaskiert in das HTML; ein präparierter Wert
hätte Skript im Admin eines Superusers ausgeführt.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib import admin

from insight_core.admin import OParlSourceAdmin
from insight_core.models import OParlSource
from insight_core.services import source_health
from insight_sync.admin import SyncLogAdmin
from insight_sync.models import SyncLog

BOESE = '<script>alert("x")</script>'


@pytest.fixture
def source_admin() -> Any:
    return OParlSourceAdmin(OParlSource, admin.site)


def test_quellenart_normal_unveraendert(source_admin: Any) -> None:
    quelle = OParlSource(name="Test", url="https://ris.example.org/system", sync_config={})

    assert source_admin.source_type_display(quelle) == '<span style="color: #16a34a; font-weight: 600;">oparl</span>'

    quelle.sync_config = {"source_type": "scraper:sessionnet"}
    assert (
        source_admin.source_type_display(quelle)
        == '<span style="color: #9333ea; font-weight: 600;">scraper:sessionnet</span>'
    )


def test_quellenart_mit_skript_wird_maskiert(source_admin: Any) -> None:
    quelle = OParlSource(
        name="Test", url="https://ris.example.org/system", sync_config={"source_type": "bridge:" + BOESE}
    )

    html = str(source_admin.source_type_display(quelle))

    assert "<script>" not in html
    assert "bridge:&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html


def _bewertung(label: str, gruende: list[str], farbe: str = "green") -> Any:
    def evaluate_source(_source: Any, now: Any = None) -> dict[str, Any]:
        return {"color": farbe, "label": label, "reasons": gruende}

    return evaluate_source


def test_gesundheit_normal_unveraendert(source_admin: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_health, "evaluate_source", _bewertung("OK", ["Sync aktuell", "keine Fehler"]))

    html = source_admin.health_display(OParlSource(name="Test"))

    assert html == '<span title="Sync aktuell; keine Fehler" style="color: #16a34a; font-weight: 600;">OK</span>'


def test_gesundheit_mit_skript_wird_maskiert(source_admin: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_health, "evaluate_source", _bewertung(BOESE, ['" onmouseover="alert(1)', BOESE], "red"))

    html = str(source_admin.health_display(OParlSource(name="Test")))

    assert "<script>" not in html
    assert 'onmouseover="' not in html
    assert html.count("&lt;script&gt;") == 2


def test_synclauf_status_normal_unveraendert() -> None:
    log_admin: Any = SyncLogAdmin(SyncLog, admin.site)

    html = log_admin.status_badge(SyncLog(status=SyncLog.Status.SUCCESS))

    assert html.startswith('<span style="color: #16a34a; font-weight: 600;"><span class="material-symbols-outlined"')
    assert ">check_circle</span> " in html
