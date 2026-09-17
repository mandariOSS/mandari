# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Service-Level-Alarme (Issue #231): Schwellen für Speicherplatz, TLS-Laufzeit und Fehlerquote,
Differenzbildung der Zähler zwischen zwei Läufen, 24-h-Wiederholsperre und Sammelmail.
Netz und TLS sind gemockt.
"""

from __future__ import annotations

import shutil
from collections import namedtuple
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command

from apps.common import service_levels as sl

DiskUsage = namedtuple("DiskUsage", "total used free")
GB = 1024**3


@pytest.fixture(autouse=True)
def leerer_cache() -> None:
    cache.clear()


def _metriken(anfragen_2xx: int, anfragen_5xx: int) -> str:
    return (
        "# TYPE mandari_http_requests_total counter\n"
        f'mandari_http_requests_total{{status_class="2xx",view="a"}} {anfragen_2xx}\n'
        f'mandari_http_requests_total{{status_class="5xx",view="a"}} {anfragen_5xx}\n'
    )


# ---------------------------------------------------------------------------
# Schwellen
# ---------------------------------------------------------------------------


def test_speicherplatz_unter_schwelle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda pfad: DiskUsage(100 * GB, 95 * GB, 5 * GB))

    [befund] = sl.pruefe_speicherplatz([("Medien", Path("/daten"))], min_percent=10, min_gb=2)

    assert not befund.ok
    assert "5.0 GB frei (5.0 %)" in befund.detail


def test_speicherplatz_ausreichend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda pfad: DiskUsage(100 * GB, 50 * GB, 50 * GB))

    [befund] = sl.pruefe_speicherplatz([("Medien", Path("/daten"))], min_percent=10, min_gb=2)

    assert befund.ok


def test_tls_laufzeit(monkeypatch: pytest.MonkeyPatch) -> None:
    laufzeiten = {"kurz.example": 5.0, "lang.example": 60.0}
    monkeypatch.setattr(sl, "zertifikat_restlaufzeit_tage", lambda host, **kw: laufzeiten[host])

    kurz, lang = sl.pruefe_tls(["kurz.example", "lang.example"], min_days=14)

    assert not kurz.ok and "5 Tagen" in kurz.detail
    assert lang.ok


def test_tls_nicht_pruefbar_ist_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    def kaputt(host: str, **kw: Any) -> float:
        raise OSError("Verbindung abgelehnt")

    monkeypatch.setattr(sl, "zertifikat_restlaufzeit_tage", kaputt)

    [befund] = sl.pruefe_tls(["weg.example"], min_days=14)

    assert not befund.ok
    assert "nicht prüfbar" in befund.detail


def test_tls_hosts_aus_site_url_und_zusatzliste(settings: Any) -> None:
    settings.SITE_URL = "https://portal.example/"
    settings.MONITOR_TLS_HOSTS = ["work.example", "portal.example"]

    assert sl.tls_hosts() == ["portal.example", "work.example"]


def test_fehlerquote_ueber_schwelle() -> None:
    [befund] = sl.pruefe_fehlerquote(_metriken(190, 10), max_percent=1.0, min_requests=100)

    assert not befund.ok
    assert "5.00 %" in befund.detail
    assert "seit Prozessstart" in befund.detail


def test_fehlerquote_differenz_seit_letztem_lauf() -> None:
    sl.pruefe_fehlerquote(_metriken(1000, 100), max_percent=1.0, min_requests=100)

    [befund] = sl.pruefe_fehlerquote(_metriken(1200, 100), max_percent=1.0, min_requests=100)

    assert befund.ok  # 0 neue Fehler bei 200 neuen Anfragen
    assert "0 von 200 Anfragen seit letztem Lauf" in befund.detail


def test_fehlerquote_nach_neustart_zaehlt_seit_start() -> None:
    sl.pruefe_fehlerquote(_metriken(1000, 0), max_percent=1.0, min_requests=100)

    [befund] = sl.pruefe_fehlerquote(_metriken(100, 50), max_percent=1.0, min_requests=100)

    assert not befund.ok
    assert "seit Prozessstart" in befund.detail


def test_fehlerquote_zu_wenige_anfragen() -> None:
    [befund] = sl.pruefe_fehlerquote(_metriken(5, 5), max_percent=1.0, min_requests=100)

    assert befund.ok
    assert "zu wenige" in befund.detail


def test_fehlerquote_metriken_nicht_abrufbar(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.METRICS_URL = "http://127.0.0.1:1/metrics/"

    def weg(url: str, token: str = "", timeout: float = 5.0) -> str:
        raise OSError("Verbindung abgelehnt")

    monkeypatch.setattr(sl, "lade_metriken", weg)

    [befund] = sl.pruefe_fehlerquote()

    assert not befund.ok
    assert "nicht abrufbar" in befund.detail


@pytest.mark.django_db
def test_warteschlange_leer_ist_ok() -> None:
    [befund] = sl.pruefe_warteschlange(max_age_minutes=60)

    assert befund.ok


# ---------------------------------------------------------------------------
# Wiederholsperre und Versand
# ---------------------------------------------------------------------------

ALARM = sl.Befund("disk:/daten", False, "Speicherplatz Medien", "1.0 GB frei")
OK = sl.Befund("tls:example", True, "TLS example", "läuft in 60 Tagen ab")


def test_alarm_hoechstens_einmal_je_24h() -> None:
    assert sl.faellige_alarme([ALARM, OK]) == [ALARM]
    assert sl.faellige_alarme([ALARM, OK]) == []


def test_dry_run_setzt_keine_sperre() -> None:
    assert sl.faellige_alarme([ALARM], dry_run=True) == [ALARM]
    assert sl.faellige_alarme([ALARM], dry_run=True) == [ALARM]
    assert sl.faellige_alarme([ALARM]) == [ALARM]


@pytest.mark.django_db
def test_sammelmail_an_alarm_empfaenger(settings: Any) -> None:
    settings.INSIGHT_ALERT_EMAILS = ["betrieb@example.org"]

    gesendet = sl.sende_alarme([ALARM, OK])

    assert gesendet == [ALARM]
    assert len(mail.outbox) == 1
    nachricht = mail.outbox[0]
    assert nachricht.to == ["betrieb@example.org"]
    assert "Service-Level-Alarm: 1 Befund" in nachricht.subject
    assert "Speicherplatz Medien: 1.0 GB frei" in nachricht.body
    assert "[OK] TLS example" in nachricht.body


@pytest.mark.django_db
def test_command_report_sendet_nichts(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    settings.INSIGHT_ALERT_EMAILS = ["betrieb@example.org"]
    monkeypatch.setattr(sl, "alle_pruefungen", lambda: [ALARM, OK])
    out = StringIO()

    call_command("check_service_levels", "--report", stdout=out)

    assert "[ALARM] Speicherplatz Medien" in out.getvalue()
    assert "[OK   ] TLS example" in out.getvalue()
    assert mail.outbox == []


@pytest.mark.django_db
def test_command_sendet_und_wiederholt_nicht(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    settings.INSIGHT_ALERT_EMAILS = ["betrieb@example.org"]
    monkeypatch.setattr(sl, "alle_pruefungen", lambda: [ALARM])
    out = StringIO()

    call_command("check_service_levels", stdout=out)
    call_command("check_service_levels", stdout=out)

    assert len(mail.outbox) == 1
    assert "Alarme gesendet: 1" in out.getvalue()
    assert "Alarme gesendet: 0" in out.getvalue()
