# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Externe Listen der Objekte, die sonst eingebettet in ihrem Träger stehen (Issue #1441).

OParl 1.1 sieht am Body neben den Pflichtlisten die Listen ``membership``, ``agendaItem``, ``consultation``, ``file``
und ``legislativeTermList`` vor. Der Aggregator nannte sie nicht; wer alle Dateien oder Tagesordnungspunkte einer
Kommune brauchte, musste sie aus Sitzungen und Vorlagen zusammensuchen.

Geprüft wird, dass jede Liste genau die Objekte der Kommune nennt, dieselben wie die Objekt-Endpunkte (nichts, was
dort nur gekürzt erscheint), dass Dateien ohne den erkannten Text kommen und dass eine Seite nicht mehr Abfragen
braucht, wenn sie mehr Objekte hat.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from hub.api.tests.konformitaet import pruefe, pruefe_liste
from insight_core.models import (
    REASON_WITHDRAWN,
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLegislativeTerm,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
API = f"{SITE}/oparl/v1"
RIS = "https://ris.example/oparl"
FREMD = "https://ris.nachbar.example/oparl"

#: Segment der Adresse -> (Feld am Body, Typ nach OParl, Schlüssel der Objekte in ``welt``)
LISTEN: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "memberships": ("membership", "Membership", ("mitgliedschaft",)),
    "agendaitems": ("agendaItem", "AgendaItem", ("punkt",)),
    "consultations": ("consultation", "Consultation", ("beratung",)),
    "files": ("file", "File", ("datei", "einladung")),
    "legislativeterms": ("legislativeTermList", "LegislativeTerm", ("wahlzeit",)),
}


@pytest.fixture(autouse=True)
def _einstellungen() -> Any:
    cache.clear()
    with override_settings(
        SITE_URL=SITE, OPARL_BASE_URL=f"{SITE}/oparl", OPARL_API_RATE_LIMIT=0, OPARL_API_CACHE_SECONDS=0
    ):
        yield
    cache.clear()


def _kommune(basis: str, name: str) -> dict[str, Any]:
    """Eine Kommune mit je einem Objekt jeder Art, dazu einer zweiten Datei an der Sitzung."""
    source = OParlSource.objects.create(name=name, url=f"{basis}/system")
    body = OParlBody.objects.create(external_id=f"{basis}/body/1", source=source, name=name, slug=name.lower())
    gremium = OParlOrganization.objects.create(external_id=f"{basis}/organization/1", body=body, name="Rat")
    person = OParlPerson.objects.create(external_id=f"{basis}/person/1", body=body, name="Erika Muster")
    mitgliedschaft = OParlMembership.objects.create(
        external_id=f"{basis}/membership/1", person=person, organization=gremium, role="Mitglied"
    )
    sitzung = OParlMeeting.objects.create(
        external_id=f"{basis}/meeting/1", body=body, name="Rat", start=datetime(2026, 10, 1, 17, 0, tzinfo=UTC)
    )
    punkt = OParlAgendaItem.objects.create(
        external_id=f"{basis}/agendaitem/1", meeting=sitzung, name="Radweg", number="1", order=1
    )
    vorlage = OParlPaper.objects.create(external_id=f"{basis}/paper/1", body=body, name="Radweg", reference="V/1")
    beratung = OParlConsultation.objects.create(
        external_id=f"{basis}/consultation/1",
        body=body,
        paper=vorlage,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=punkt.external_id,
    )
    datei = OParlFile.objects.create(
        external_id=f"{basis}/file/1",
        body=body,
        paper=vorlage,
        name="Begründung",
        access_url=f"{basis}/file/1/download",
        text_content="Der Radweg wird gebaut.",
    )
    einladung = OParlFile.objects.create(
        external_id=f"{basis}/file/2",
        body=body,
        meeting=sitzung,
        name="Einladung",
        access_url=f"{basis}/file/2/download",
    )
    wahlzeit = OParlLegislativeTerm.objects.create(
        external_id=f"{basis}/legislativeterm/1",
        body=body,
        name="17. Wahlzeit",
        start_date=date(2025, 11, 1),
        end_date=date(2030, 10, 31),
    )
    return {
        "body": body,
        "gremium": gremium,
        "person": person,
        "mitgliedschaft": mitgliedschaft,
        "sitzung": sitzung,
        "punkt": punkt,
        "vorlage": vorlage,
        "beratung": beratung,
        "datei": datei,
        "einladung": einladung,
        "wahlzeit": wahlzeit,
    }


@pytest.fixture
def welt() -> dict[str, Any]:
    return _kommune(RIS, "Musterstadt")


@pytest.fixture
def nachbar() -> dict[str, Any]:
    return _kommune(FREMD, "Nachbarstadt")


def _json(pfad: str) -> dict[str, Any]:
    antwort = Client().get(pfad)
    assert antwort.status_code == 200, (pfad, antwort.status_code, antwort.content[:300])
    return cast(dict[str, Any], antwort.json())


def _liste(kommune: dict[str, Any], segment: str, query: str = "") -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _json(f"/oparl/v1/body/{kommune['body'].id}/{segment}{query}")["data"])


def _kennungen(kommune: dict[str, Any], segment: str, query: str = "") -> set[str]:
    return {eintrag["id"] for eintrag in _liste(kommune, segment, query)}


def _adresse(segment: str, objekt: Any) -> str:
    typ = {"memberships": "membership", "agendaitems": "agendaitem", "legislativeterms": "legislativeterm"}.get(
        segment, segment.rstrip("s")
    )
    return f"{API}/{typ}/{objekt.id}"


def test_body_nennt_die_listen(welt: dict[str, Any]) -> None:
    body = _json(f"/oparl/v1/body/{welt['body'].id}")

    for segment, (feld, _typ, _objekte) in LISTEN.items():
        assert body[feld] == f"{API}/body/{welt['body'].id}/{segment}"
    # Die eingebetteten Wahlzeiten bleiben daneben Pflichtfeld
    assert [term["name"] for term in body["legislativeTerm"]] == ["17. Wahlzeit"]
    assert pruefe(body, "Body") == []


@pytest.mark.parametrize("segment", sorted(LISTEN))
def test_liste_nennt_genau_die_objekte_der_kommune(welt: dict[str, Any], nachbar: dict[str, Any], segment: str) -> None:
    _feld, typ, objekte = LISTEN[segment]
    antwort = _json(f"/oparl/v1/body/{welt['body'].id}/{segment}")

    assert {eintrag["id"] for eintrag in antwort["data"]} == {_adresse(segment, welt[name]) for name in objekte}
    assert pruefe_liste(antwort, typ) == []
    # Die Liste enthält dasselbe wie der Objekt-Endpunkt (Dateien dort zusätzlich mit Text)
    for eintrag in antwort["data"]:
        einzeln = _json(eintrag["id"].removeprefix(SITE))
        einzeln.pop("text", None)
        assert eintrag == einzeln


def test_dateien_kommen_ohne_den_erkannten_text(welt: dict[str, Any]) -> None:
    dateien = {eintrag["name"]: eintrag for eintrag in _liste(welt, "files")}

    assert "text" not in dateien["Begründung"]
    assert dateien["Begründung"]["paper"] == [f"{API}/paper/{welt['vorlage'].id}"]
    assert dateien["Einladung"]["meeting"] == [f"{API}/meeting/{welt['sitzung'].id}"]
    # Am Objekt-Endpunkt steht der Text weiterhin
    assert _json(f"/oparl/v1/file/{welt['datei'].id}")["text"] == "Der Radweg wird gebaut."


def test_dateien_zurueckgenommener_vorlagen_und_sitzungen_fehlen(welt: dict[str, Any]) -> None:
    """Sie tragen keine eigene Markierung; der Objekt-Endpunkt gibt sie nur gekürzt aus, die Liste gar nicht."""
    welt["vorlage"].mark_deleted(reason=REASON_WITHDRAWN)

    assert _kennungen(welt, "files") == {_adresse("files", welt["einladung"])}
    assert set(_json(f"/oparl/v1/file/{welt['datei'].id}")) == {"id", "type", "created", "modified", "deleted"}

    welt["sitzung"].mark_deleted(reason=REASON_WITHDRAWN)

    assert _liste(welt, "files") == []
    # Auch die inkrementelle Liste nennt weder Namen noch Adressen dieser Dateien
    assert _liste(welt, "files", "?modified_since=2000-01-01T00:00:00%2B00:00") == []


def test_dateien_in_der_quelle_geloeschter_vorlagen_bleiben(welt: dict[str, Any]) -> None:
    """Löscht die Quelle nur die Vorlage, liefert der Objekt-Endpunkt die Datei weiter aus; die Liste ebenso."""
    welt["vorlage"].mark_deleted()

    assert _kennungen(welt, "files") == {_adresse("files", welt["datei"]), _adresse("files", welt["einladung"])}
    assert "name" in _json(f"/oparl/v1/file/{welt['datei'].id}")


@pytest.mark.parametrize("segment", sorted(LISTEN))
def test_geloeschtes_steht_nur_in_der_inkrementellen_liste(welt: dict[str, Any], segment: str) -> None:
    objekt = welt[LISTEN[segment][2][0]]
    adresse = _adresse(segment, objekt)
    objekt.mark_deleted()

    assert adresse not in _kennungen(welt, segment)
    inkrementell = {e["id"]: e for e in _liste(welt, segment, "?modified_since=2000-01-01T00:00:00%2B00:00")}
    assert set(inkrementell[adresse]) == {"id", "type", "created", "modified", "deleted"}
    assert inkrementell[adresse]["deleted"] is True


def test_mitgliedschaft_ueber_zwei_kommunen_steht_bei_beiden(welt: dict[str, Any], nachbar: dict[str, Any]) -> None:
    """Person der Nachbarstadt im Gremium von Musterstadt: wie im Snapshot bei beiden Kommunen."""
    gemeinsam = OParlMembership.objects.create(
        external_id=f"{RIS}/membership/2", person=nachbar["person"], organization=welt["gremium"]
    )
    adresse = _adresse("memberships", gemeinsam)

    assert adresse in _kennungen(welt, "memberships")
    assert adresse in _kennungen(nachbar, "memberships")
    assert _adresse("memberships", welt["mitgliedschaft"]) not in _kennungen(nachbar, "memberships")


def test_beratung_nennt_sitzung_und_tagesordnungspunkt(welt: dict[str, Any]) -> None:
    (beratung,) = _liste(welt, "consultations")

    assert beratung["paper"] == f"{API}/paper/{welt['vorlage'].id}"
    assert beratung["meeting"] == f"{API}/meeting/{welt['sitzung'].id}"
    assert beratung["agendaItem"] == f"{API}/agendaitem/{welt['punkt'].id}"


def test_tagesordnungspunkt_nennt_seine_beratung(welt: dict[str, Any]) -> None:
    (punkt,) = _liste(welt, "agendaitems")

    assert punkt["meeting"] == f"{API}/meeting/{welt['sitzung'].id}"
    assert punkt["consultation"] == f"{API}/consultation/{welt['beratung'].id}"


def test_unbekannte_liste_nennt_die_verfuegbaren(welt: dict[str, Any]) -> None:
    antwort = Client().get(f"/oparl/v1/body/{welt['body'].id}/votes")

    assert antwort.status_code == 404
    for segment in LISTEN:
        assert segment in antwort.content.decode()


def test_liste_einer_unbekannten_kommune_ergibt_404() -> None:
    antwort = Client().get("/oparl/v1/body/00000000-0000-4000-8000-000000000000/files")

    assert antwort.status_code == 404


def _abfragen(kommune: dict[str, Any], segment: str) -> int:
    with CaptureQueriesContext(connection) as erfasst:
        _liste(kommune, segment)
    return len(erfasst)


@pytest.mark.parametrize("segment", sorted(LISTEN))
def test_seite_braucht_nicht_mehr_abfragen_fuer_mehr_objekte(welt: dict[str, Any], segment: str) -> None:
    _liste(welt, segment)  # einmalige Abfragen des ersten Aufrufs (Veröffentlichungsstand) nicht mitzählen
    vorher = _abfragen(welt, segment)
    body, sitzung, vorlage, gremium = welt["body"], welt["sitzung"], welt["vorlage"], welt["gremium"]
    for nummer in range(10, 16):
        person = OParlPerson.objects.create(external_id=f"{RIS}/person/{nummer}", body=body, name=f"Person {nummer}")
        OParlMembership.objects.create(external_id=f"{RIS}/membership/{nummer}", person=person, organization=gremium)
        punkt = OParlAgendaItem.objects.create(
            external_id=f"{RIS}/agendaitem/{nummer}", meeting=sitzung, name=f"Punkt {nummer}", order=nummer
        )
        OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/{nummer}",
            body=body,
            paper=vorlage,
            meeting_external_id=sitzung.external_id,
            agenda_item_external_id=punkt.external_id,
        )
        OParlFile.objects.create(external_id=f"{RIS}/file/{nummer}", body=body, paper=vorlage, name=f"Anlage {nummer}")
        OParlLegislativeTerm.objects.create(external_id=f"{RIS}/legislativeterm/{nummer}", body=body, name=str(nummer))

    assert len(_liste(welt, segment)) >= 7
    assert _abfragen(welt, segment) == vorher


def test_snapshot_der_aelteren_form_bleibt_bei_den_bisherigen_listen() -> None:
    """Die neuen Listen kämen dort doppelt: ihre Objekte stehen schon eingebettet in ihren Trägern."""
    from hub.api import aggregator

    assert [spec.kind for spec in aggregator._SNAPSHOT_LISTS] == [
        "organization",
        "person",
        "meeting",
        "paper",
        "location",
    ]
    assert set(LISTEN) <= set(aggregator.BODY_LISTS)
