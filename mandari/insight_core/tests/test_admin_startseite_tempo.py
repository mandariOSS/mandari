# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin-Startseite und Betriebsmonitor ohne Zählungen über alle Dateien im Seitenaufruf (Issue #1409).

In Produktion (1,66 Mio. Dateien) dauerte die Admin-Startseite rund 68 s: Der Check „Dokument-Cache“ rechnete bei
jedem Aufruf die volle ``cache_stats``, die Kennzahlen zählten die großen OParl-Tabellen. Beides rechnet jetzt der
stündliche Prüflauf ``check_source_health`` vor, die Seiten lesen nur den Stand.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from insight_core.admin_dashboard import dashboard_kennzahlen
from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource
from insight_core.services import file_cache
from insight_core.services.source_health import collect_system_health

pytestmark = pytest.mark.django_db


@pytest.fixture
def ablage(tmp_path: Path, monkeypatch: Any) -> Path:
    monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(file_cache, "disk_free_bytes", lambda: 500 * 1024**3)
    return tmp_path


def _kommune(nummer: int, **quelle: Any) -> OParlBody:
    source = OParlSource.objects.create(name=f"Quelle {nummer}", url=f"https://ris{nummer}.example/oparl", **quelle)
    return OParlBody.objects.create(
        source=source, external_id=f"https://ris{nummer}.example/oparl/body/1", name=f"Stadt {nummer}"
    )


def _datei(body: OParlBody, nummer: int, **felder: Any) -> OParlFile:
    return OParlFile.objects.create(
        body=body, external_id=f"{body.external_id}/file/{nummer}", name=f"Datei {nummer}", **felder
    )


def _dokument_cache() -> dict[str, Any]:
    return next(c for c in collect_system_health() if c["name"] == "Dokument-Cache")


def test_statistik_in_einem_durchlauf(ablage: Path) -> None:
    jetzt = timezone.now()
    geschont = _kommune(1, consecutive_failures=5)
    _datei(geschont, 1)
    _datei(geschont, 2)
    _datei(geschont, 3, local_status="ok", size=100)
    _datei(geschont, 4, local_status="retry", fetch_next_at=jetzt - timedelta(hours=1))
    _datei(geschont, 5, local_status="retry", fetch_next_at=jetzt + timedelta(hours=1))
    normal = _kommune(2)
    _datei(normal, 1)
    _datei(normal, 2, local_status="ok", size=999, local_size=50)
    _datei(normal, 3, local_status="ok", local_size=70, deleted=True)

    stats = file_cache.cache_stats()

    assert stats["total"] == 7, "gelöschte Dateien zählen nicht"
    assert (stats["ok"], stats["retry"], stats["retry_due"]) == (2, 2, 1)
    assert stats["paused"] == 2, "nur offene Dateien der Quelle in Schonung"
    assert stats["without_size"] == 1
    assert stats["cached_bytes"] == 150, "gemessene Größe vor der Angabe der Quelle"
    assert stats["per_body"] == [
        {"body": "Stadt 1", "files": 5, "cached_bytes": 100},
        {"body": "Stadt 2", "files": 2, "cached_bytes": 50},
    ]


def test_seitenaufruf_rechnet_die_statistik_nicht(ablage: Path, monkeypatch: Any) -> None:
    def verboten() -> dict[str, Any]:
        raise AssertionError("cache_stats im Seitenaufruf")

    monkeypatch.setattr(file_cache, "cache_stats", verboten)
    check = _dokument_cache()
    assert check["status"] == "ok"
    assert "folgen mit dem nächsten stündlichen Prüflauf" in check["detail"]
    assert "500 GB frei" in check["detail"], "den freien Speicher misst jeder Aufruf"


def test_pruefllauf_legt_den_stand_ab(ablage: Path, monkeypatch: Any) -> None:
    _datei(_kommune(1), 1, local_status="ok", local_size=10)
    _datei(_kommune(2), 1)

    call_command("check_source_health", "--report", stdout=StringIO())

    monkeypatch.setattr(file_cache, "cache_stats", lambda: pytest.fail("Stand liegt vor"))
    check = _dokument_cache()
    assert "1 von 2 Dokumenten lokal" in check["detail"]
    assert "(Stand " in check["detail"]


def test_kennzahlen_aus_dem_stand(django_assert_num_queries: Any) -> None:
    body = _kommune(1)
    OParlPaper.objects.create(external_id=f"{body.external_id}/paper/1", body=body, name="Erste")
    assert dashboard_kennzahlen()["stats"]["papers"] == 1

    OParlPaper.objects.create(external_id=f"{body.external_id}/paper/2", body=body, name="Zweite")
    with django_assert_num_queries(0):
        assert dashboard_kennzahlen()["stats"]["papers"] == 1
    assert dashboard_kennzahlen(refresh=True)["stats"]["papers"] == 2


def test_startseite_nennt_den_stand(ablage: Path, admin_client: Client) -> None:
    response = admin_client.get("/admin/")
    assert response.status_code == 200
    assert "stündlich aktualisiert" in response.content.decode()
