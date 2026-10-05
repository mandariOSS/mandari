# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aktualität je Quelle im Admin (Issue #556).

Der Ingestor setzt ``last_successful_sync`` bzw. ``last_successful_full_sync`` nur nach einem Abgleich
ohne Lücke; ``last_sync`` zählt jeden durchgelaufenen. Die Liste der Quellen zeigt den letzten
vollständigen Abgleich und, ob spätere Abgleiche Lücken hatten.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.admin.sites import site
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from insight_core.admin import OParlSourceAdmin
from insight_core.models import OParlSource

pytestmark = pytest.mark.django_db

URL = "https://ris.example/oparl/system"


def _admin() -> OParlSourceAdmin:
    return OParlSourceAdmin(OParlSource, site)


def test_felder_in_liste_und_detailansicht() -> None:
    admin = _admin()
    assert "last_successful_sync_ago" in admin.list_display
    assert {"last_successful_sync", "last_successful_full_sync"} <= set(admin.readonly_fields)


def test_nie_abgeglichen() -> None:
    assert _admin().last_successful_sync_ago(OParlSource(name="Quelle", url=URL)) == "-"


def test_abgeglichen_aber_nie_vollstaendig() -> None:
    quelle = OParlSource(name="Quelle", url=URL, last_sync=timezone.now())
    assert "noch nie" in str(_admin().last_successful_sync_ago(quelle))


def test_letzter_abgleich_war_vollstaendig() -> None:
    zuletzt = timezone.now() - timedelta(hours=2, minutes=5)
    quelle = OParlSource(name="Quelle", url=URL, last_sync=zuletzt, last_successful_sync=zuletzt)
    assert _admin().last_successful_sync_ago(quelle) == "vor 2 Std."


def test_spaetere_abgleiche_mit_luecken() -> None:
    jetzt = timezone.now()
    quelle = OParlSource(
        name="Quelle",
        url=URL,
        last_sync=jetzt - timedelta(minutes=10),
        last_successful_sync=jetzt - timedelta(days=3, hours=1),
    )
    assert "vor 3 Tag(en), seither mit Lücken" in str(_admin().last_successful_sync_ago(quelle))


def test_liste_der_quellen_zeigt_die_spalte(admin_client: Client) -> None:
    jetzt = timezone.now()
    OParlSource.objects.create(
        name="Quelle mit Lücken",
        url=URL,
        last_sync=jetzt,
        last_successful_sync=jetzt - timedelta(days=2, hours=1),
        last_successful_full_sync=jetzt - timedelta(days=6),
    )
    antwort = admin_client.get(reverse("admin:insight_core_oparlsource_changelist"))
    assert antwort.status_code == 200
    inhalt = antwort.content.decode()
    assert "Vollständig abgeglichen" in inhalt
    assert "vor 2 Tag(en), seither mit Lücken" in inhalt


def _scraper_quelle(**last_run: object) -> OParlSource:
    return OParlSource(
        name="Quelle",
        url=URL,
        sync_config={"source_type": "scraper:sessionnet", "scraper_state": {"last_run": last_run}},
    )


def test_scraper_status_ohne_luecke_ohne_hinweis() -> None:
    quelle = _scraper_quelle(at="x", parse_quota=1.0, incomplete={}, tombstone_braked={})
    anzeige = str(_admin().scraper_status_display(quelle))
    assert anzeige.startswith("Letzter Lauf: x")
    assert "unvollständig" not in anzeige and "gebremst" not in anzeige


def test_scraper_status_nennt_luecken_und_bremse() -> None:
    """Eine dauerhaft greifende Bremse setzt den Löschabgleich still aus: Der Admin nennt sie."""
    quelle = _scraper_quelle(
        at="x",
        parse_quota=1.0,
        incomplete={"meeting": "Kalender 09/2026 nicht lesbar"},
        tombstone_braked={"organization": 11},
    )
    anzeige = str(_admin().scraper_status_display(quelle))
    assert "unvollständig: meeting (Kalender 09/2026 nicht lesbar)" in anzeige
    assert "Löschabgleich gebremst: organization (11 fehlen)" in anzeige
