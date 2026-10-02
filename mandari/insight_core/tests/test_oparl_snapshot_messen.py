# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``manage.py oparl_snapshot_messen`` (Issue #707): Bauzeit und Größe des Snapshots messen, bevor der
Änderungsfeed eingeschaltet wird – auf demselben Weg wie ``…/snapshot``, ohne den Schalter und ohne Ausgabe an
Abnehmer.
"""

from __future__ import annotations

import io
import json
from datetime import date
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from hub.api import changes
from insight_core.management.commands import oparl_snapshot_messen
from insight_core.models import OParlBody, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
RIS = "https://ris.example/oparl"


@pytest.fixture(autouse=True)
def _heute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "today", lambda: date(2026, 10, 2))


@pytest.fixture
def kommunen() -> Any:
    cache.clear()
    with override_settings(SITE_URL=SITE, OPARL_BASE_URL=f"{SITE}/oparl", OPARL_API_RATE_LIMIT=0):
        quelle = OParlSource.objects.create(name="RIS", url=f"{RIS}/system")
        gross = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=quelle, name="Großstadt")
        klein = OParlBody.objects.create(external_id=f"{RIS}/body/2", source=quelle, name="Kleinstadt")
        verborgen = OParlBody.objects.create(
            external_id=f"{RIS}/body/3", source=quelle, name="Demostadt", is_listed=False
        )
        for nummer in range(5):
            OParlPaper.objects.create(external_id=f"{RIS}/paper/g{nummer}", body=gross, name=f"Vorlage {nummer}")
        OParlMeeting.objects.create(external_id=f"{RIS}/meeting/g1", body=gross, name="Rat")
        OParlPaper.objects.create(external_id=f"{RIS}/paper/k1", body=klein, name="Vorlage")
        for nummer in range(9):
            OParlPaper.objects.create(external_id=f"{RIS}/paper/v{nummer}", body=verborgen, name=f"Vorlage {nummer}")
        geloescht = OParlPaper.objects.create(external_id=f"{RIS}/paper/g9", body=gross, name="Gelöscht")
        geloescht.mark_deleted()
        yield {"gross": gross, "klein": klein, "verborgen": verborgen}
    cache.clear()


def _aufruf(*argumente: str) -> str:
    ausgabe = io.StringIO()
    call_command("oparl_snapshot_messen", *argumente, stdout=ausgabe)
    return ausgabe.getvalue()


def test_groesste_gelistete_kommune_ohne_eingeschalteten_feed(kommunen: dict[str, Any]) -> None:
    """Ohne Schalter: die Demo-Kommune ist größer, aber nicht gelistet; der Feed bleibt aus."""
    with override_settings(OPARL_CHANGES_ENABLED=False):
        ausgabe = _aufruf()
        assert Client().get(f"/oparl/v1/body/{kommunen['gross'].pk}/snapshot").status_code == 404

    assert "Großstadt" in ausgabe
    assert "Kleinstadt" not in ausgabe and "Demostadt" not in ausgabe
    # Body, eine Sitzung und fünf Vorlagen; Gelöschtes nicht
    assert "7 Zeilen" in ausgabe


def test_misst_dasselbe_wie_der_abruf(kommunen: dict[str, Any]) -> None:
    gross = kommunen["gross"]
    # Eingeschaltet nennt der Body zusätzlich Feed und Snapshot; sonst ist alles gleich
    with override_settings(OPARL_CHANGES_ENABLED=True):
        gemessen = oparl_snapshot_messen.messen(gross.pk)
        antwort = Client().get(f"/oparl/v1/body/{gross.pk}/snapshot")
    assert antwort.status_code == 200
    inhalt = b"".join(cast(Any, antwort).streaming_content)
    kopf, rest = inhalt.split(b"\n", 1)

    assert json.loads(kopf)["objects"] == gemessen.zeilen == 7
    assert len(rest) == gemessen.groesse
    assert gemessen.sekunden >= 0


def test_mehrere_und_bestimmte_kommunen(kommunen: dict[str, Any]) -> None:
    beide = _aufruf("--anzahl", "2")
    assert beide.index("Großstadt") < beide.index("Kleinstadt")

    bestimmt = _aufruf("--body", str(kommunen["verborgen"].pk))
    assert "Demostadt" in bestimmt and "10 Zeilen" in bestimmt


def test_unbekannte_kommune(kommunen: dict[str, Any]) -> None:
    with pytest.raises(CommandError):
        _aufruf("--body", "keine-uuid")
    with pytest.raises(CommandError):
        _aufruf("--body", "00000000-0000-0000-0000-000000000000")
