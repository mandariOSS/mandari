# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verwaltungsbefehle der Live-Übertragungen (Issue #915): Protokoll des Prototyps einspielen (idempotent, ohne
Ereignisse, ohne Bild-Adressen), Quelle einrichten (Prüfung von Anbieter, Kennung und Profil).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.events.models import Event
from hub.live.models import Broadcast, BroadcastLog, BroadcastSection, BroadcastSource, BroadcastSpeech, BroadcastStatus
from hub.live.tests.conftest import EMBED_ID, Welt, kennung
from insight_core.models import OParlOrganization

pytestmark = pytest.mark.django_db


def _zeilen(beginn: datetime) -> list[dict[str, Any]]:
    def zeit(sekunden: int) -> str:
        return (beginn + timedelta(seconds=sekunden)).isoformat()

    return [
        {"zeit": zeit(0), "art": "start", "stream": 63961, "hls": "https://cdn.example/live.m3u8"},
        {"zeit": zeit(1), "art": "abfrage", "online": True, "playout": "pre", "zuschauer": "0"},
        {
            "zeit": zeit(2),
            "art": "status",
            "online": True,
            "playout": "live",
            "tafel": "",
            "metadaten": {"playoutState": "live", "posterImage": "https://cdn.example/bild.jpg"},
            "rest": {"IsOnline": True},
        },
        {"zeit": zeit(10), "art": "roh", "groesse": [1920, 1080], "ms_bild": 900, "ms_gesamt": 1800, "top": "5"},
        {"zeit": zeit(20), "art": "top", "top": "5", "titel_ocr": "Neubau einer Grundschule"},
        {"zeit": zeit(30), "art": "person", "top": "5", "name": "Erika Muster", "fraktion": "Fraktion A"},
        {"zeit": zeit(40), "art": "person", "top": "5", "name": "Gisela Gast", "fraktion": "Oberburgermeisterin"},
        # nicht monoton: zurück zu TOP 1
        {"zeit": zeit(50), "art": "top", "top": "1", "titel_ocr": "Eröffnung"},
        {"zeit": zeit(55), "art": "fehler", "wo": "bild", "fehler": "Zeitgrenze"},
        {"zeit": zeit(56), "art": "lebenszeichen", "bilder": 5},
        "keine JSON-Zeile",
        {
            "zeit": zeit(60),
            "art": "status",
            "online": True,
            "playout": "post",
            "tafel": "Die Sitzung wurde um 18:25 beendet",
            "metadaten": {},
        },
    ]


def _datei(tmp_path: Path, zeilen: list[Any]) -> Path:
    datei = tmp_path / "waechter.jsonl"
    datei.write_text(
        "\n".join(z if isinstance(z, str) else json.dumps(z, ensure_ascii=False) for z in zeilen) + "\n",
        encoding="utf-8",
    )
    return datei


def _einspielen(datei: Path, welt: Welt) -> str:
    ausgabe = StringIO()
    call_command("live_protokoll_einspielen", str(datei), "--meeting", str(welt.sitzung.pk), stdout=ausgabe)
    return ausgabe.getvalue()


def test_protokoll_einspielen_ist_idempotent(welt: Welt, tmp_path: Path) -> None:
    datei = _datei(tmp_path, _zeilen(welt.jetzt))
    ausgabe = _einspielen(datei, welt)
    assert "2 neue TOP-Abschnitte, 2 neue Wortmeldungen" in ausgabe
    assert "1 fehlerhaft" in ausgabe and "Status beendet" in ausgabe

    broadcast = Broadcast.objects.get()
    assert broadcast.status == BroadcastStatus.BEENDET
    assert broadcast.started_at == welt.jetzt + timedelta(seconds=2)
    assert broadcast.ended_at == welt.jetzt + timedelta(seconds=60)
    abschnitte = list(BroadcastSection.objects.order_by("started_at"))
    assert [(a.number, a.agenda_item) for a in abschnitte] == [("5", welt.top5), ("1", welt.top1)]
    assert all(a.ended_at is not None for a in abschnitte)
    erika, gisela = BroadcastSpeech.objects.order_by("started_at")
    assert (erika.person, erika.faction_read, erika.section) == (welt.muster, "Fraktion A", abschnitte[0])
    assert (gisela.person, gisela.faction_read, gisela.function_read) == (None, "", "Oberburgermeisterin")
    assert not Event.objects.exists(), "keine Ereignisse beim Einspielen"
    daten = json.dumps(list(BroadcastLog.objects.values_list("data", flat=True)))
    assert "bild.jpg" not in daten and "posterImage" not in daten, "keine Bild-Adressen"
    anzahl = BroadcastLog.objects.count()

    zweite = _einspielen(datei, welt)
    assert "0 neue TOP-Abschnitte, 0 neue Wortmeldungen, 0 neue Protokolleinträge" in zweite
    assert BroadcastLog.objects.count() == anzahl
    assert BroadcastSection.objects.count() == 2 and BroadcastSpeech.objects.count() == 2


def test_protokoll_einspielen_ohne_quelle(welt: Welt, tmp_path: Path) -> None:
    welt.quelle.delete()
    with pytest.raises(CommandError, match="Übertragungsquelle"):
        _einspielen(_datei(tmp_path, []), welt)


def test_quelle_einrichten_und_aendern(welt: Welt) -> None:
    gremium = OParlOrganization.objects.create(external_id=kennung("organizations"), body=welt.body, name="Ausschuss")
    ausgabe = StringIO()
    call_command(
        "live_quelle_einrichten",
        "--gremium",
        gremium.external_id,
        "--anbieter",
        "3q",
        "--kennung",
        EMBED_ID,
        "--takt",
        "15",
        stdout=ausgabe,
    )
    quelle = BroadcastSource.objects.get(organization=gremium)
    assert (quelle.body, quelle.active, quelle.interval_seconds) == (welt.body, False, 15), "neu: inaktiv"
    assert quelle.overlay_profile["version"] == 1
    assert "angelegt" in ausgabe.getvalue()

    call_command("live_quelle_einrichten", "--gremium", str(gremium.pk), "--aktiv", stdout=StringIO())
    quelle.refresh_from_db()
    assert quelle.active

    with pytest.raises(CommandError, match="ungültig"):
        call_command("live_quelle_einrichten", "--gremium", str(gremium.pk), "--anbieter", "unbekannt")
    with pytest.raises(CommandError, match="ungültig"):
        call_command("live_quelle_einrichten", "--gremium", str(gremium.pk), "--takt", "1")
    with pytest.raises(CommandError, match="nicht gefunden"):
        call_command("live_quelle_einrichten", "--gremium", kennung("organizations"))
