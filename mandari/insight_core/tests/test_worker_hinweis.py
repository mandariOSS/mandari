# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin-Hinweis zum Worker (Issue #509): Startseite und Betriebsmonitor zeigen ihn nur, wenn die
Installation einen Worker braucht und keiner die nötigen Rollen bedient.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.test import Client

from apps.events import presence
from insight_core.services.source_health import collect_system_health


@pytest.fixture
def ohne_bedarf(settings: Any) -> Any:
    settings.EVENTS_WORKER_REQUIRED = ""
    settings.INGESTOR_EVENTS_ENABLED = False
    return settings


def _worker(checks: list[dict[str, Any]]) -> dict[str, Any]:
    return next(check for check in checks if check["name"] == "Worker")


@pytest.mark.django_db
def test_ohne_bedarf_kein_hinweis(ohne_bedarf: Any) -> None:
    assert _worker(collect_system_health())["status"] == "inactive"


@pytest.mark.django_db
def test_fehlender_worker_bei_bedarf_ist_kritisch(ohne_bedarf: Any) -> None:
    ohne_bedarf.INGESTOR_EVENTS_ENABLED = True

    check = _worker(collect_system_health())

    assert check["status"] == "critical"
    assert check["detail"].startswith("Kein Worker für sequencer")


@pytest.mark.django_db
def test_laufender_worker_ist_in_ordnung(ohne_bedarf: Any) -> None:
    ohne_bedarf.EVENTS_WORKER_REQUIRED = "true"
    presence.announce("w1", sorted(presence.ALL_ROLES), [])

    check = _worker(collect_system_health())

    assert check["status"] == "ok"
    assert check["detail"] == "1 Worker (dispatch, scheduler, sequencer, tasks)"


@pytest.mark.django_db
def test_admin_startseite_und_monitor_zeigen_den_hinweis(ohne_bedarf: Any, admin_client: Client) -> None:
    ohne_bedarf.INGESTOR_EVENTS_ENABLED = True

    for pfad in ("/admin/", "/admin/monitoring/"):
        response = admin_client.get(pfad)
        assert response.status_code == 200
        assert "Kein Worker für sequencer" in response.content.decode(), pfad


@pytest.mark.django_db
def test_admin_ohne_bedarf_ohne_hinweis(ohne_bedarf: Any, admin_client: Client) -> None:
    response = admin_client.get("/admin/")
    assert response.status_code == 200
    assert "Kein Worker" not in response.content.decode()
