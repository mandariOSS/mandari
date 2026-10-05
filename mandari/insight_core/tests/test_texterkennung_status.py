# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prüfung ``texterkennung`` für die Statusseite und Handlungsbedarf im Betriebsmonitor (Issue #817)."""

from __future__ import annotations

from datetime import timedelta
from itertools import count
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.events import status
from insight_core.models import OParlFile
from insight_core.services.source_health import collect_action_items
from insight_core.services.text_extraction_health import check_text_extraction, extraction_health

_nummer = count()


def _datei(**felder: Any) -> OParlFile:
    aktualisiert = felder.pop("updated_at", None)
    datei = OParlFile.objects.create(external_id=f"https://ris.example.org/file/{next(_nummer)}", **felder)
    if aktualisiert is not None:
        OParlFile.objects.filter(pk=datei.pk).update(updated_at=aktualisiert)
    return datei


@pytest.fixture(autouse=True)
def _ohne_zwischenspeicher() -> Any:
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_ohne_abbrueche_in_ordnung() -> None:
    jetzt = timezone.now()
    _datei(text_extraction_status="processing", text_extraction_started_at=jetzt - timedelta(minutes=10))
    _datei(text_extraction_status="completed")
    _datei(text_extraction_status="failed")  # gewöhnlicher Fehler, kein Abbruch

    ergebnis = check_text_extraction()

    assert ergebnis.ok
    assert ergebnis.detail == (
        "0 Dateien hängen, 0 nach Abbruch erneut eingeplant, 0 nach wiederholtem Abbruch aufgegeben (24 h)"
    )


@pytest.mark.django_db
def test_haengende_dateien_ueber_der_zeitgrenze() -> None:
    alt = timezone.now() - timedelta(hours=2)
    _datei(text_extraction_status="processing", text_extraction_started_at=alt)
    # Aus der Zeit vor dem Zähler: ohne Beginn zählt die letzte Änderung
    _datei(text_extraction_status="processing", updated_at=alt)

    assert extraction_health().haengend == 2
    assert not check_text_extraction().ok


@pytest.mark.django_db
def test_aufgegebene_dateien_ab_der_schwelle_24_stunden_rot(settings: Any) -> None:
    settings.TEXT_EXTRACTION_GIVE_UP_ALERT = 2
    _datei(text_extraction_status="pending", text_extraction_attempts=1)
    erste = _datei(text_extraction_status="failed", text_extraction_attempts=3, text_extracted_at=timezone.now())

    stand = extraction_health()
    assert (stand.abgebrochen, stand.aufgegeben) == (1, 1)
    assert check_text_extraction().ok

    zweite = _datei(text_extraction_status="failed", text_extraction_attempts=3, text_extracted_at=timezone.now())
    assert not check_text_extraction().ok

    OParlFile.objects.filter(pk__in=[erste.pk, zweite.pk]).update(
        text_extracted_at=timezone.now() - timedelta(hours=25)
    )
    assert check_text_extraction().ok


@pytest.mark.django_db
def test_einzelne_aufgegebene_datei_ohne_alarm_aber_im_detail() -> None:
    """Standard 5: ein übergroßer Scan macht die Statusseite nicht rot (#842), die Zahl bleibt sichtbar."""
    _datei(text_extraction_status="failed", text_extraction_attempts=3, text_extracted_at=timezone.now())

    ergebnis = check_text_extraction()
    assert ergebnis.ok
    assert "1 nach wiederholtem Abbruch aufgegeben (24 h)" in ergebnis.detail


@pytest.mark.django_db
def test_spaetere_aenderung_der_zeile_verlaengert_das_fenster_nicht() -> None:
    """Maßgeblich ist der Zeitpunkt der Aufgabe, nicht updated_at (Dokumentablage, Abgleich, Sync ändern die Zeile)."""
    _datei(
        text_extraction_status="failed",
        text_extraction_attempts=3,
        text_extracted_at=timezone.now() - timedelta(hours=25),
        updated_at=timezone.now(),
    )

    assert extraction_health().aufgegeben == 0
    assert check_text_extraction().ok


@pytest.mark.django_db
def test_statusseite_je_pruefung(client: Client, settings: Any) -> None:
    settings.TEXT_EXTRACTION_GIVE_UP_ALERT = 1
    assert "texterkennung" in status.CHECKS
    assert client.get("/health/worker/?pruefung=texterkennung").status_code == 200

    _datei(text_extraction_status="failed", text_extraction_attempts=3, text_extracted_at=timezone.now())
    cache.clear()
    antwort = client.get("/health/worker/?pruefung=texterkennung")

    assert antwort.status_code == 503
    assert set(antwort.json()["checks"]) == {"texterkennung"}


@pytest.mark.django_db
def test_handlungsbedarf_im_betriebsmonitor() -> None:
    _datei(text_extraction_status="failed", text_extraction_attempts=3)

    eintraege = {e["label"]: e for e in collect_action_items()}

    eintrag = eintraege["Texterkennung nach wiederholtem Abbruch aufgegeben (Speichergrenze)"]
    assert eintrag["count"] == 1
    assert "text_extraction_attempts__gt=0" in eintrag["url"]
