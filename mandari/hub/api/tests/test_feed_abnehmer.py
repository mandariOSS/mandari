# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``scripts/feed_abnehmer.py`` (Issue #707): Der Abnehmer-Durchlauf, mit dem Feed und Snapshot nach dem
Einschalten geprüft werden, läuft hier gegen die Anwendung selbst – über den Test-Client statt HTTP.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from django.core.cache import cache
from django.db.models import Max
from django.test import Client, override_settings

from apps.events.models import Event
from hub.api import changes
from hub.api.tests.ereignisse import ereignis, ruecknahme
from hub.ris import retraction
from insight_core.models import OParlBody, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
RIS = "https://ris.example/oparl"
SKRIPT = Path(__file__).resolve().parents[4] / "scripts" / "feed_abnehmer.py"


def _laden() -> ModuleType:
    spec = importlib.util.spec_from_file_location("feed_abnehmer", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


abnehmer = _laden()


@pytest.fixture(autouse=True)
def _heute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "today", lambda: date(2026, 10, 2))


@pytest.fixture
def welt(settings: Any) -> Any:
    cache.clear()
    settings.INGESTOR_EVENTS_ENABLED = True
    with override_settings(
        SITE_URL=SITE,
        OPARL_BASE_URL=f"{SITE}/oparl",
        OPARL_API_RATE_LIMIT=0,
        OPARL_API_CACHE_SECONDS=0,
        OPARL_CHANGES_ENABLED=True,
    ):
        quelle = OParlSource.objects.create(name="RIS", url=f"{RIS}/system")
        body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=quelle, name="Musterstadt")
        OParlMeeting.objects.create(external_id=f"{RIS}/meeting/1", body=body, name="Rat")
        vorlagen = [
            OParlPaper.objects.create(external_id=f"{RIS}/paper/{n}", body=body, name=f"Vorlage {n}") for n in range(4)
        ]
        yield {"body": body, "vorlagen": vorlagen}
    cache.clear()


def _abruf(methode: str, adresse: str, kopf: dict[str, str]) -> Any:
    """Abruf über den Test-Client, wie ihn das Skript über HTTP macht."""
    client = Client()
    pfad = adresse.removeprefix(SITE)
    antwort: Any = client.head(pfad, headers=kopf) if methode == "HEAD" else client.get(pfad, headers=kopf)
    inhalt = b"".join(antwort.streaming_content) if antwort.streaming else antwort.content
    kopfzeilen = {name.lower(): wert for name, wert in antwort.headers.items()}
    return abnehmer.Antwort(antwort.status_code, kopfzeilen, io.BytesIO(inhalt))


def _nummerieren() -> None:
    for pk in Event.objects.filter(seq__isnull=True).order_by("id").values_list("pk", flat=True):
        Event.objects.filter(pk=pk).update(seq=(Event.objects.aggregate(h=Max("seq"))["h"] or 0) + 1)


def _lauf(welt: dict[str, Any], abruf: Any = None, *, von_vorn: bool = True) -> Any:
    durchlauf = abnehmer.Durchlauf(abruf or _abruf, pause=0, stichprobe=10, von_vorn=von_vorn)
    return durchlauf.lauf(f"{SITE}/oparl/v1/body/{welt['body'].pk}")


def test_durchlauf_von_vorn_ohne_befund(welt: dict[str, Any]) -> None:
    eins, zwei = welt["vorlagen"][:2]
    ereignis("ris.paper.changed", welt["body"].pk, eins.pk)
    retraction.retract(zwei, reason="nichtoeffentlich")
    _nummerieren()

    bericht = _lauf(welt)

    assert bericht.befunde == []
    assert bericht.zahlen["snapshot_objekte"] == 5  # Body, Sitzung und drei Vorlagen; die zurückgenommene nicht
    assert bericht.zahlen["feed_eintraege"] == {"upsert": 1, "delete": 1}
    assert bericht.zahlen["feed_304"] is True
    assert bericht.zahlen["stichprobe_geprueft"] == 7  # fünf aus dem Snapshot, zwei aus dem Feed


def test_uebergabe_vom_snapshot_an_den_feed(welt: dict[str, Any]) -> None:
    """Ab dem Cursor des Snapshots erscheint nur, was nach dem Festhalten des Cursors geschah."""
    vorher, nachher = welt["vorlagen"][:2]
    ereignis("ris.paper.changed", welt["body"].pk, vorher.pk)

    def abrufen(methode: str, adresse: str, kopf: dict[str, str]) -> Any:
        antwort = _abruf(methode, adresse, kopf)
        if methode == "GET" and adresse.endswith("/snapshot"):
            retraction.retract(nachher, reason="zurueckgenommen")
            _nummerieren()
        return antwort

    bericht = _lauf(welt, abrufen, von_vorn=False)

    assert bericht.befunde == []
    assert bericht.zahlen["feed_eintraege"] == {"delete": 1}


def test_meldet_einen_eintrag_ohne_abrufbares_objekt(welt: dict[str, Any]) -> None:
    """Ein ``upsert``, dessen Adresse nicht (mehr) als Objekt abrufbar ist, ist ein Befund."""
    vorlage = welt["vorlagen"][0]
    ereignis("ris.paper.changed", welt["body"].pk, vorlage.pk)

    bericht = _lauf(welt, _abruf_ohne(vorlage.pk))

    assert [befund for befund in bericht.befunde if str(vorlage.pk) in befund]


def test_ruecknahme_ohne_geloeschtes_objekt_ist_ein_befund(welt: dict[str, Any]) -> None:
    """Nennt der Feed ein ``delete``, muss das Objekt gelöscht ausgeliefert werden (oder fehlen)."""
    vorlage = welt["vorlagen"][1]
    ruecknahme(welt["body"].pk, "Paper", vorlage.pk, "nichtoeffentlich")

    bericht = _lauf(welt)

    assert any("ohne deleted: true" in befund for befund in bericht.befunde)


def _abruf_ohne(kennung: Any) -> Any:
    """Abruf, bei dem ein Objekt fehlt (``404``), wie nach einer harten Löschung."""

    def abrufen(methode: str, adresse: str, kopf: dict[str, str]) -> Any:
        if adresse.endswith(f"/paper/{kennung}"):
            return abnehmer.Antwort(404, {}, io.BytesIO(b"{}"))
        return _abruf(methode, adresse, kopf)

    return abrufen


def test_ausgeschalteter_feed_ist_ein_befund(welt: dict[str, Any]) -> None:
    with override_settings(OPARL_CHANGES_ENABLED=False):
        bericht = _lauf(welt)

    assert bericht.befunde and "mandari:changes" in bericht.befunde[0]
