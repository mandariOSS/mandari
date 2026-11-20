# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dateien direkt am Tagesordnungspunkt (Issue #1528).

OParl erlaubt Dateien am AgendaItem (``auxiliaryFile``, ``resolutionFile``), etwa Anträge ohne Vorlage. Der Ingestor
schreibt sie mit ``OParlFile.agenda_item`` und der Sitzung des Punkts. Die Sitzungsseiten von Insight und Work zeigen sie
am Punkt (``files_of_agenda_items``), nicht unter den Sitzungsdateien; die Sitzungsvorbereitung zeigt sie als erste
Unterlagen des Punkts. Ein zurückgenommener Punkt sperrt seine Dateien wie eine zurückgenommene Sitzung.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.work.meetings import neu as sitzung_neu
from apps.work.meetings.selectors import PreparedItem
from apps.work.meetings.serializers import _ris_files_of
from hub.ris import selectors as ris
from insight_core.models import OParlAgendaItem, OParlBody, OParlFile, OParlMeeting, OParlPaper, OParlSource
from insight_core.services import file_reconcile

pytestmark = pytest.mark.django_db

RIS = "https://oparl.top-dateien.example/bodies/0001"
#: Kennung der Session-Schnittstelle: gelöscht und mit dieser Kennung gilt ein Objekt als zurückgenommen
SESSION = "https://mandari.example/session/stadt/api/oparl"


def _kennung(art: str, basis: str = RIS) -> str:
    return f"{basis}/{art}/{uuid.uuid4()}"


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="TOP-Dateien-RIS", url=f"{RIS}/system")
    kommune: OParlBody = OParlBody.objects.create(external_id=_kennung("bodies"), source=source, name="Teststadt")
    return kommune


@pytest.fixture
def sitzung(body: OParlBody) -> OParlMeeting:
    return OParlMeeting.objects.create(
        external_id=_kennung("meetings"), body=body, name="Rat", start=timezone.now() + timedelta(days=5)
    )


def _punkt(sitzung: OParlMeeting, nummer: str, **felder: Any) -> OParlAgendaItem:
    basis = felder.pop("basis", RIS)
    return OParlAgendaItem.objects.create(
        external_id=_kennung("agendaitems", basis), meeting=sitzung, number=nummer, name=f"Punkt {nummer}", **felder
    )


def _datei(
    body: OParlBody, sitzung: OParlMeeting, name: str, punkt: OParlAgendaItem | None = None, **felder: Any
) -> OParlFile:
    return OParlFile.objects.create(
        external_id=_kennung("files"),
        body=body,
        meeting=sitzung,
        agenda_item=punkt,
        name=name,
        access_url=f"{RIS}/files/{name}.pdf",
        **felder,
    )


def test_dateien_stehen_am_punkt_und_nicht_unter_den_sitzungsdateien(body: OParlBody, sitzung: OParlMeeting) -> None:
    eins, zwei = _punkt(sitzung, "5.1.1"), _punkt(sitzung, "5.1.2")
    antrag_b = _datei(body, sitzung, "Antrag B", eins)
    antrag_a = _datei(body, sitzung, "Antrag A", eins)
    _datei(body, sitzung, "Weg", eins, deleted=True)
    antrag_c = _datei(body, sitzung, "Antrag C", zwei)
    einladung = _datei(body, sitzung, "Einladung")

    assert ris.files_of_agenda_items([eins, zwei, _punkt(sitzung, "6")]) == {
        eins.pk: [antrag_a, antrag_b],
        zwei.pk: [antrag_c],
    }
    assert ris.files_of_agenda_items([]) == {}
    assert ris.files_of_meeting(sitzung) == [einladung]


def test_insight_zeigt_die_dateien_am_punkt(body: OParlBody, sitzung: OParlMeeting, client: Any) -> None:
    punkt = _punkt(sitzung, "5.1.1")
    antrag = _datei(body, sitzung, "Antrag der Fraktion zum Radverkehr", punkt, size=120_000)

    antwort = client.get(f"/insight/termine/{sitzung.pk}/")

    inhalt = antwort.content.decode()
    assert antwort.status_code == 200
    assert 'data-testid="top-dateien"' in inhalt and "Antrag der Fraktion zum Radverkehr" in inhalt
    assert f"/dokumente/{antrag.pk}/preview/" in inhalt


def test_in_der_quelle_nicht_mehr_abrufbare_datei_ohne_link(
    body: OParlBody, sitzung: OParlMeeting, client: Any
) -> None:
    punkt = _punkt(sitzung, "5.1.1")
    weg = _datei(body, sitzung, "Antrag ohne Datei", punkt, source_missing_since=timezone.now())

    inhalt = client.get(f"/insight/termine/{sitzung.pk}/").content.decode()

    assert "Antrag ohne Datei (in der Quelle nicht mehr abrufbar)" in inhalt
    assert f"/dokumente/{weg.pk}/preview/" not in inhalt


def test_zurueckgenommener_punkt_sperrt_seine_dateien(body: OParlBody, sitzung: OParlMeeting) -> None:
    offen = _datei(body, sitzung, "Offen", _punkt(sitzung, "1"))
    zurueck = _datei(body, sitzung, "Zurück", _punkt(sitzung, "2", basis=SESSION, deleted=True))
    ohne_punkt = _datei(body, sitzung, "Einladung")

    assert file_reconcile.parent_withdrawn(offen) is False
    assert file_reconcile.parent_withdrawn(zurueck) is True
    assert file_reconcile.parent_withdrawn(ohne_punkt) is False
    # Die Seiten zeigen sie nicht, auch nicht in der Vorbereitung
    assert zurueck.agenda_item is not None
    assert ris.files_of_agenda_items([zurueck.agenda_item]) == {}
    vorbereitet = PreparedItem(
        item=zurueck.agenda_item,
        position=None,
        private_note=None,
        own_speech=None,
        shared_speeches=[],
        notes=[],
        documents=[],
        papers=[],
        primary_paper=None,
        has_files=True,
        files=[zurueck],
    )
    assert _ris_files_of(vorbereitet) == []


def test_work_sitzungsdetail_und_vorbereitung(body: OParlBody, sitzung: OParlMeeting) -> None:
    punkt = _punkt(sitzung, "5.2.1")
    antrag = _datei(body, sitzung, "Antrag", punkt)
    vorlage = OParlPaper.objects.create(external_id=_kennung("papers"), body=body, name="Vorlage")
    anlage = OParlFile.objects.create(external_id=_kennung("files"), body=body, paper=vorlage, name="Anlage")
    # Dieselbe Datei an Punkt und Vorlage erscheint einmal
    beides = _datei(body, sitzung, "Beides", punkt)
    beides.paper = vorlage
    beides.save(update_fields=["paper"])

    eintrag = sitzung_neu.sitzung([punkt])["tagesordnung"][0]
    assert eintrag["dateien"] == [antrag, beides]

    vorbereitet = PreparedItem(
        item=punkt,
        position=None,
        private_note=None,
        own_speech=None,
        shared_speeches=[],
        notes=[],
        documents=[],
        papers=[vorlage],
        primary_paper=vorlage,
        has_files=True,
        files=[antrag, beides],
    )
    assert [f.name for f in _ris_files_of(vorbereitet)] == ["Antrag", "Beides", "Anlage"]
    assert anlage in _ris_files_of(vorbereitet)
    # In der Quelle gelöschte Dateien nennt die Vorbereitung nicht (keine Originaladresse)
    anlage.deleted = True
    anlage.save(update_fields=["deleted"])
    assert [f.name for f in _ris_files_of(vorbereitet)] == ["Antrag", "Beides"]
