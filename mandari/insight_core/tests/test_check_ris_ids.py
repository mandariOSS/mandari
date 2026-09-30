# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prüfskript für die kanonischen Kennungen im Bestand: zählt je Quelle und Entität, ändert nichts."""

from __future__ import annotations

import uuid
from io import StringIO
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings
from mandari_oparl.ids import canonical_id

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)
from insight_core.services.ris_ids import NO_SOURCE, check_ris_ids

pytestmark = pytest.mark.django_db

FREMD = "https://ris.beispiel.example/oparl/v1/"
SITE = "https://mandari.example"
SESSION = f"{SITE}/session/nord/api/oparl/"


@pytest.fixture
def bestand(db: Any) -> dict[str, Any]:
    """Ein Fremd-RIS mit Altbestand aus Django und eine Session-Quelle mit einem Objekt unter fremdem Host."""
    fremd = OParlSource.objects.create(name="Beispiel-RIS", url=f"{FREMD}system")
    # Körperschaft aus der Zeit vor den kanonischen Kennungen (zufällige Kennung)
    body = OParlBody.objects.create(id=uuid.uuid4(), external_id=f"{FREMD}body/1", source=fremd, name="Bsp", slug="bsp")
    rat = OParlOrganization.objects.create(external_id=f"{FREMD}organization/1", body=body, name="Rat")
    person = OParlPerson.objects.create(external_id=f"{FREMD}person/1", body=body, name="Erika")
    OParlMembership.objects.create(id=uuid.uuid4(), external_id=f"{FREMD}membership/1", person=person, organization=rat)
    sitzung = OParlMeeting.objects.create(external_id=f"{FREMD}meeting/1", body=body, name="Rat")
    OParlAgendaItem.objects.create(id=uuid.uuid4(), external_id=f"{FREMD}agendaitem/1", meeting=sitzung)
    vorlage = OParlPaper.objects.create(external_id=f"{FREMD}paper/1", body=body, name="Vorlage")
    # Datei ohne eigene Körperschaft: Zuordnung über die Vorlage
    OParlFile.objects.create(external_id=f"{FREMD}file/1", paper=vorlage, name="Anlage")
    # Kollision: Die kanonische Kennung von paper/2 ist schon an ein anderes Objekt vergeben
    OParlPaper.objects.create(id=canonical_id(f"{FREMD}paper/2"), external_id=f"{FREMD}paper/2-alt", body=body)
    OParlPaper.objects.create(id=uuid.uuid4(), external_id=f"{FREMD}paper/2", body=body)
    # Ohne URI
    OParlPerson.objects.create(external_id="", body=body, name="Ohne URI")

    session = OParlSource.objects.create(
        name="Sitzungsdienst Nord", url=SESSION, sync_config={"source_type": "oparl", "session_tenant": "nord"}
    )
    session_body = OParlBody.objects.create(external_id=f"{SESSION}body/", source=session, name="Nord", slug="nord")
    OParlMeeting.objects.create(external_id=f"{SESSION}meeting/1/", body=session_body, name="Rat")
    # Früher über einen anderen Host abgerufen
    OParlMeeting.objects.create(external_id="http://intern:8000/session/nord/api/oparl/meeting/2/", body=session_body)
    # Ohne Körperschaft und ohne Quelle
    OParlFile.objects.create(external_id=f"{FREMD}file/lose")
    return {"fremd": str(fremd.pk), "session": str(session.pk)}


@override_settings(SITE_URL=SITE)
def test_zaehlt_abweichungen_je_quelle_und_entitaet(bestand: dict[str, Any]) -> None:
    report = check_ris_ids()
    fremd, session = bestand["fremd"], bestand["session"]

    def zeile(quelle: str, entitaet: str) -> tuple[int, int, int, int, int]:
        c = report.counts[(quelle, entitaet)]
        return (c.objects, c.id_deviations, c.uri_deviations, c.without_uri, c.collisions)

    assert zeile(fremd, "body") == (1, 1, 0, 0, 0)
    assert zeile(fremd, "organization") == (1, 0, 0, 0, 0)
    assert zeile(fremd, "person") == (2, 0, 0, 1, 0)
    assert zeile(fremd, "membership") == (1, 1, 0, 0, 0)
    assert zeile(fremd, "meeting") == (1, 0, 0, 0, 0)
    assert zeile(fremd, "agendaitem") == (1, 1, 0, 0, 0)
    assert zeile(fremd, "paper") == (3, 2, 0, 0, 1)
    assert zeile(fremd, "file") == (1, 0, 0, 0, 0)
    assert zeile(session, "body") == (1, 0, 0, 0, 0)
    assert zeile(session, "meeting") == (2, 0, 1, 0, 0)
    assert zeile(NO_SOURCE, "file") == (1, 0, 0, 0, 0)
    assert report.sources[session].session_base == SESSION

    total = report.total()
    assert (total.objects, total.id_deviations, total.uri_deviations, total.collisions) == (15, 5, 1, 1)


@override_settings(SITE_URL=SITE)
def test_befehl_liest_nur(bestand: dict[str, Any]) -> None:
    vorher = {m: sorted(m.objects.values_list("id", "external_id")) for m in (OParlBody, OParlPaper, OParlMeeting)}
    out = StringIO()

    call_command("check_ris_ids", "--dry-run", "--examples", "2", stdout=out)

    ausgabe = out.getvalue()
    assert "Quelle: Beispiel-RIS (Fremd-RIS)" in ausgabe
    assert "Quelle: Sitzungsdienst Nord (Session-Mandant nord)" in ausgabe
    assert "Ohne Quelle" in ausgabe
    assert "URI: http://intern:8000/session/nord/api/oparl/meeting/2/" in ausgabe
    assert "15 Objekte, davon 5 mit abweichender Kennung, 1 mit abweichender URI" in ausgabe
    assert "1 Kollision(en)" in ausgabe
    assert {m: sorted(m.objects.values_list("id", "external_id")) for m in vorher} == vorher


@override_settings(SITE_URL=SITE)
def test_einzelne_quelle(bestand: dict[str, Any]) -> None:
    out = StringIO()
    call_command("check_ris_ids", "--source", SESSION, stdout=out)
    ausgabe = out.getvalue()
    assert "Sitzungsdienst Nord" in ausgabe
    assert "Beispiel-RIS" not in ausgabe
    assert "3 Objekte, davon 0 mit abweichender Kennung, 1 mit abweichender URI" in ausgabe

    report = check_ris_ids(only_source=bestand["fremd"])
    assert {quelle for quelle, _ in report.counts} == {bestand["fremd"]}


@pytest.mark.parametrize("quelle", ["https://unbekannt.example/system", str(uuid.uuid4()), "keine-uuid"])
def test_unbekannte_quelle(db: Any, quelle: str) -> None:
    with pytest.raises(CommandError, match="Quelle nicht gefunden"):
        call_command("check_ris_ids", "--source", quelle, stdout=StringIO())


def test_leerer_bestand(db: Any) -> None:
    out = StringIO()
    call_command("check_ris_ids", stdout=out)
    assert "Ergebnis: 0 Objekte" in out.getvalue()
