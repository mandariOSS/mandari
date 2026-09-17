# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Betriebsmonitor für Scraper-Quellen (Issue #53): Parse-Quote und Entitäten-Zufluss des letzten
Laufs aus ``sync_config["scraper_state"]["last_run"]`` werden bewertet; OParl-Quellen bleiben unberührt.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from insight_core.models import OParlSource
from insight_core.services.source_health import evaluate_source, scraper_run_findings


def _scraper(db: Any, **last_run: Any) -> OParlSource:
    return OParlSource.objects.create(
        name="Pilotstadt (SessionNet)",
        url="https://ris.pilotstadt.example/",
        last_sync=timezone.now() - timedelta(hours=1),
        sync_config={
            "source_type": "scraper:sessionnet",
            "scraper": {"base_url": "https://ris.pilotstadt.example/"},
            "scraper_state": {"last_run": last_run} if last_run else {},
        },
    )


@pytest.mark.django_db
def test_gute_quote_bleibt_ok(db: Any) -> None:
    quelle = _scraper(
        db, at="2026-09-18T05:00:00+00:00", full=False, parse_quota=0.99, detail_pages_attempted=40, entities_stored=12
    )
    item = evaluate_source(quelle)
    assert item["status"] == "ok" and item["scraper"]["is_scraper"] is True
    assert item["scraper"]["parse_quota"] == 0.99 and item["reasons"] == []


@pytest.mark.django_db
def test_quote_unter_pilotschwelle_warnt(db: Any) -> None:
    quelle = _scraper(db, at="x", full=False, parse_quota=0.9, detail_pages_attempted=40, entities_stored=3)
    item = evaluate_source(quelle)
    assert item["status"] == "warning"
    assert any("Pilot-Schwelle" in r for r in item["reasons"])
    assert "Stichprobe" in item["recommendation"]


@pytest.mark.django_db
def test_eingebrochene_quote_ist_kritisch(db: Any, settings: Any) -> None:
    settings.INSIGHT_SCRAPER_QUOTA_CRITICAL = 0.8
    quelle = _scraper(db, at="x", full=False, parse_quota=0.5, detail_pages_attempted=20, entities_stored=1)
    item = evaluate_source(quelle)
    assert item["status"] == "critical"
    assert any("eingebrochen" in r for r in item["reasons"])


@pytest.mark.django_db
def test_wenige_seiten_werden_nicht_bewertet(db: Any) -> None:
    """Unter fünf Detailseiten sagt die Quote nichts (Onboarding mit Seitenlimit)."""
    quelle = _scraper(db, at="x", full=False, parse_quota=0.5, detail_pages_attempted=2, entities_stored=1)
    assert evaluate_source(quelle)["status"] == "ok"


@pytest.mark.django_db
def test_volllauf_ohne_zufluss_warnt(db: Any) -> None:
    quelle = _scraper(
        db, at="x", full=True, parse_quota=1.0, detail_pages_attempted=0, entities_stored=0, unchanged_skipped=0
    )
    item = evaluate_source(quelle)
    assert item["status"] == "warning"
    assert any("ohne Entitäten-Zufluss" in r for r in item["reasons"])


@pytest.mark.django_db
def test_oparl_quelle_und_scraper_ohne_lauf_unberuehrt(db: Any) -> None:
    oparl = OParlSource.objects.create(name="OParl", url="https://oparl.example/system", last_sync=timezone.now())
    assert scraper_run_findings(oparl)["is_scraper"] is False
    frisch = _scraper(db)
    befund = scraper_run_findings(frisch)
    assert befund["is_scraper"] is True and befund["reasons"] == [] and befund["parse_quota"] is None
