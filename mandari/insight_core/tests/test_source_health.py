# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Betriebsmonitor: Sperre (User-Agent) und 5xx-Serie als Grund mit Handlungsempfehlung (Issue #123).

Der Ingestor setzt ``OParlSource.last_error_kind`` und legt die Statistik in ``last_error`` ab;
die Django-Seite bewertet, zeigt Grund und Empfehlung und nimmt beides in die Alarmmail auf.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.core import mail
from django.test import Client
from django.utils import timezone

from insight_core.models import OParlSource
from insight_core.services.source_health import collect_source_health, evaluate_source, send_health_alerts


@pytest.fixture
def blocked_source(db: Any) -> OParlSource:
    now = timezone.now()
    return OParlSource.objects.create(
        name="Beispielstadt",
        url="https://ris.beispielstadt.example/oparl/system",
        last_sync=now - timedelta(hours=3),
        last_error="User-Agent gesperrt: ris.beispielstadt.example antwortet auf unseren User-Agent mit HTTP 403",
        last_error_at=now - timedelta(minutes=10),
        last_error_kind=OParlSource.ERROR_KIND_UA_BLOCKED,
        consecutive_failures=1,
    )


@pytest.fixture
def flaky_source(db: Any) -> OParlSource:
    now = timezone.now()
    return OParlSource.objects.create(
        name="Wackelstadt",
        url="https://ris.wackelstadt.example/oparl/system",
        last_sync=now - timedelta(hours=1),
        last_error=(
            "5xx-Serie: ris.wackelstadt.example lieferte 7 Serverfehler in Folge zwischen 10:00 und 10:04 UTC "
            "(4 min), insgesamt 7 in diesem Lauf; letzte Statuscodes: 500, 500, 502; "
            "betroffene Listen: https://ris.wackelstadt.example/oparl/bodies/1/meetings"
        ),
        last_error_at=now - timedelta(minutes=5),
        last_error_kind=OParlSource.ERROR_KIND_SERVER_ERROR_SERIES,
        consecutive_failures=1,
    )


def test_ua_block_is_critical_with_reason_and_recommendation(blocked_source: OParlSource) -> None:
    item = evaluate_source(blocked_source)

    assert item["status"] == "critical"
    assert item["error_kind"] == "ua_blocked"
    assert item["error_kind_label"] == "User-Agent gesperrt"
    assert item["paused"] is True
    assert any(reason.startswith("User-Agent gesperrt") for reason in item["reasons"])
    assert any("Quellen-Schonung" in reason for reason in item["reasons"])
    assert "Betreiber der Quelle kontaktieren" in item["recommendation"]
    # Statistik des Ingestors bleibt als „Letzter Fehler“ sichtbar
    assert any("HTTP 403" in reason for reason in item["reasons"])


def test_server_error_series_is_critical_with_statistics(flaky_source: OParlSource) -> None:
    item = evaluate_source(flaky_source)

    assert item["status"] == "critical"
    assert item["error_kind_label"] == "5xx-Serie"
    assert any(reason.startswith("5xx-Serie") for reason in item["reasons"])
    assert any("betroffene Listen" in reason for reason in item["reasons"])
    assert "30 Minuten" in item["recommendation"]


def test_single_failure_without_kind_stays_a_warning(db: Any) -> None:
    source = OParlSource.objects.create(
        name="Normalstadt",
        url="https://ris.normalstadt.example/oparl/system",
        last_sync=timezone.now() - timedelta(hours=1),
        last_error="Timeout",
        last_error_at=timezone.now(),
        consecutive_failures=1,
    )
    item = evaluate_source(source)
    assert item["status"] == "warning"
    assert item["error_kind"] == ""
    assert item["recommendation"] == ""
    assert item["paused"] is False


def test_unknown_kind_does_not_break_evaluation(db: Any) -> None:
    source = OParlSource.objects.create(
        name="Fremdstadt",
        url="https://ris.fremdstadt.example/oparl/system",
        last_sync=timezone.now(),
        last_error_kind="etwas_neues",
    )
    item = evaluate_source(source)
    assert item["status"] == "ok"
    assert item["error_kind_label"] == ""


def test_collect_source_health_sorts_blocked_source_first(blocked_source: OParlSource, db: Any) -> None:
    OParlSource.objects.create(name="Aachen-OK", url="https://ris.ok.example/oparl/system", last_sync=timezone.now())
    health = collect_source_health()
    assert health["overall"] == "critical"
    assert health["items"][0]["source"] == blocked_source
    assert health["problems"][0]["error_kind_label"] == "User-Agent gesperrt"


def test_alert_mail_contains_reason_and_recommendation(blocked_source: OParlSource, settings: Any) -> None:
    settings.INSIGHT_ALERT_EMAILS = ["betrieb@example.org"]
    settings.MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend", "OPTIONS": {}}}

    result = send_health_alerts()

    assert result["alerts"] == ["Beispielstadt"]
    assert len(mail.outbox) == 1
    body = mail.outbox[0].body
    html = "".join(str(alt[0]) for alt in getattr(mail.outbox[0], "alternatives", []))
    text = body + html
    assert "User-Agent gesperrt" in text
    assert "Empfehlung" in text
    assert "Betreiber der Quelle kontaktieren" in text
    blocked_source.refresh_from_db()
    assert blocked_source.health_alert_sent_at is not None


def test_check_source_health_report_prints_recommendation(blocked_source: OParlSource, capsys: Any) -> None:
    from django.core.management import call_command

    call_command("check_source_health", "--report")
    out = capsys.readouterr().out
    assert "Beispielstadt" in out
    assert "User-Agent gesperrt" in out
    assert "Empfehlung:" in out


def test_monitoring_page_shows_reason_and_recommendation(blocked_source: OParlSource, admin_client: Client) -> None:
    response = admin_client.get("/admin/monitoring/")
    assert response.status_code == 200
    page = response.content.decode()
    assert "Grund: User-Agent gesperrt" in page
    assert "Empfehlung:" in page
    assert "Betreiber der Quelle kontaktieren" in page


def test_admin_change_form_offers_user_agent_and_shows_kind(blocked_source: OParlSource, admin_client: Client) -> None:
    response = admin_client.get(f"/admin/insight_core/oparlsource/{blocked_source.pk}/change/")
    assert response.status_code == 200
    page = response.content.decode()
    assert 'name="user_agent"' in page
    assert "User-Agent gesperrt" in page  # Fehlerklasse als Nur-Lese-Feld


def test_reset_health_action_clears_kind(blocked_source: OParlSource, admin_client: Client) -> None:
    response = admin_client.post(
        "/admin/insight_core/oparlsource/",
        {"action": "reset_health", "_selected_action": [str(blocked_source.pk)]},
    )
    assert response.status_code in (200, 302)
    blocked_source.refresh_from_db()
    assert blocked_source.last_error_kind is None
    assert blocked_source.consecutive_failures == 0
