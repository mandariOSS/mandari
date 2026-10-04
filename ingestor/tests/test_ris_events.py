# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS-Ereignisse des Ingestors (Issue #513): aus altem und neuem Stand eines Objekts wird ein Ereignis.

Hier die Abbildung ohne Datenbank. Dass die Nutzlasten zu den Verträgen passen, prüft die Drehscheibe
(``mandari/hub/contracts/tests/test_ingestor_events.py``); dass Ereignis und Datenänderung in einer
Transaktion stehen, ``test_events_journal.py``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from mandari_oparl.ids import IdBases, canonical_id

from src.scrapers.base import VOLATILE_HASH_FIELDS
from src.storage import ris_events
from src.storage.database import _ENTITY_MODEL_MAP
from src.storage.ris_events import Prior

BASE = "https://ris.example.org/oparl"
MEETING = f"{BASE}/meeting/1"
PAPER = f"{BASE}/paper/1"
ITEM = f"{BASE}/agendaitem/1"
FILE = f"{BASE}/file/1"
CONSULTATION = f"{BASE}/consultation/1"
ORG = f"{BASE}/organization/1"
GEHEIM = "Personalangelegenheit Erika Mustermann"


def cid(url: str) -> UUID:
    return canonical_id(url)


def meeting(**felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": MEETING,
        "type": "https://schema.oparl.org/1.1/Meeting",
        "name": "Rat",
        "start": "2026-10-01T17:00:00+02:00",
        "organization": [ORG],
        "created": "2026-09-01T10:00:00+02:00",
        "modified": "2026-09-01T10:00:00+02:00",
    }
    daten.update(felder)
    return daten


def paper(**felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": PAPER,
        "type": "https://schema.oparl.org/1.1/Paper",
        "name": "Mehr Bänke im Park",
        "paperType": "Antrag",
        "modified": "2026-09-01T10:00:00+02:00",
    }
    daten.update(felder)
    return daten


def item(**felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": ITEM,
        "type": "https://schema.oparl.org/1.1/AgendaItem",
        "number": "1",
        "name": "Mehr Bänke im Park",
        "public": True,
    }
    daten.update(felder)
    return daten


# --- Vergleich: nur echte Änderungen ------------------------------------------------------------------


def test_unveraendertes_objekt_ergibt_kein_ereignis() -> None:
    assert ris_events.paper_events(cid(PAPER), paper(), Prior(paper())) == []
    assert ris_events.meeting_events(cid(MEETING), meeting(), Prior(meeting())) == []


def test_zeitstempel_und_content_hash_sind_keine_aenderung() -> None:
    neu = paper(modified="2026-09-30T00:00:00+02:00", created="2026-09-30T00:00:00+02:00")
    neu["mandari:contentHash"] = "sha256:abc"
    assert ris_events.paper_events(cid(PAPER), neu, Prior(paper())) == []
    # wie beim Content-Hash der Scraper-Quellen: dieselben Felder zählen nicht
    assert set(VOLATILE_HASH_FIELDS) == ris_events.VOLATILE_FIELDS


def test_zeitstempel_eingebetteter_objekte_sind_keine_aenderung() -> None:
    """OParl 1.0 stempelt jedes Objekt mit dem Abrufdatum; ein Vollabgleich darf daraus nichts melden."""
    alt = meeting(agendaItem=[item(modified="2026-09-29T00:00:00+02:00")])
    neu = meeting(agendaItem=[item(modified="2026-09-30T00:00:00+02:00")])
    assert ris_events.meeting_events(cid(MEETING), neu, Prior(alt)) == []
    neu = meeting(agendaItem=[item(name="Mehr Bänke", modified="2026-09-30T00:00:00+02:00")])
    (ereignis,) = ris_events.meeting_events(cid(MEETING), neu, Prior(alt))
    assert ereignis.payload["changed"] == ["agendaItem"]


def personen(*nummern: int) -> list[str]:
    return [f"{BASE}/person/{nummer}" for nummer in nummern]


def test_andere_reihenfolge_der_teilnehmer_ist_keine_aenderung() -> None:
    """
    Befund aus dem Betrieb (Issue #553): Eine Quelle liefert ``participant`` von Abruf zu Abruf anders
    sortiert (dieselben Personen, zwei Einträge vertauscht). Jeder Abgleich meldete die Sitzung als
    geändert, obwohl sich fachlich nichts geändert hatte.
    """
    alt = meeting(participant=personen(254, 274, 250, 330, 1285, 853))
    neu = meeting(participant=personen(254, 274, 250, 1285, 330, 853))
    assert ris_events.meeting_events(cid(MEETING), neu, Prior(alt)) == []
    # Kommt eine Person dazu oder fällt eine weg, ist das weiterhin eine Änderung.
    for geaendert in (personen(254, 274, 250, 1285, 330, 853, 7), personen(254, 274, 250, 1285, 330)):
        (ereignis,) = ris_events.meeting_events(cid(MEETING), meeting(participant=geaendert), Prior(alt))
        assert ereignis.payload["changed"] == ["participant"]


#: Platzhalter: Das Feld fehlt im Objekt der Quelle.
FEHLT = object()


def _ohne_fehlende(daten: dict[str, Any]) -> dict[str, Any]:
    return {feld: wert for feld, wert in daten.items() if wert is not FEHLT}


def _ereignisse_je_typ(typ: str, alt: dict[str, Any], neu: dict[str, Any]) -> list[ris_events.Draft]:
    """Ereignisse des Abgleichs eines Objekts dieser Art vom Stand ``alt`` auf den Stand ``neu``."""
    bauen = {"meeting": meeting, "paper": paper, "agendaitem": item, "consultation": consultation, "file": datei}[typ]
    vorher, nachher = _ohne_fehlende(bauen(**alt)), _ohne_fehlende(bauen(**neu))
    if typ == "meeting":
        return ris_events.meeting_events(cid(MEETING), nachher, Prior(vorher))
    if typ == "paper":
        return ris_events.paper_events(cid(PAPER), nachher, Prior(vorher))
    if typ == "agendaitem":
        prior = Prior(vorher, meeting_id=cid(MEETING))
        return ris_events.agenda_item_events(cid(ITEM), nachher, prior, meeting_id=cid(MEETING), public=True)
    if typ == "consultation":
        return ris_events.consultation_events(cid(CONSULTATION), nachher, Prior(vorher), paper_id=cid(PAPER))
    return ris_events.file_events(cid(FILE), nachher, Prior(vorher))


#: Je Objektart eine Liste, die Quellen in wechselnder Reihenfolge liefern können
LISTEN_JE_TYP = [
    ("meeting", "participant", personen(1, 2, 3)),
    ("meeting", "organization", [ORG, f"{BASE}/organization/2"]),
    ("meeting", "auxiliaryFile", [{"id": f"{BASE}/file/{i}", "name": f"Anlage {i}"} for i in (1, 2)]),
    ("meeting", "agendaItem", [item(), item(id=f"{BASE}/agendaitem/2", number="2", name="Haushalt")]),
    ("paper", "originatorPerson", personen(1, 2)),
    ("paper", "underDirectionOf", [ORG, f"{BASE}/organization/2"]),
    ("paper", "keyword", ["Park", "Bänke"]),
    ("paper", "consultation", [{"id": f"{BASE}/consultation/{i}", "role": "Vorberatung"} for i in (1, 2)]),
    ("agendaitem", "auxiliaryFile", [{"id": f"{BASE}/file/{i}", "name": f"Anlage {i}"} for i in (1, 2)]),
    ("agendaitem", "keyword", ["Park", "Bänke"]),
    ("consultation", "organization", [ORG, f"{BASE}/organization/2"]),
    ("file", "derivativeFile", [f"{BASE}/file/2", f"{BASE}/file/3"]),
]


@pytest.mark.parametrize(("typ", "feld", "liste"), LISTEN_JE_TYP, ids=[f"{t}-{f}" for t, f, _ in LISTEN_JE_TYP])
def test_listen_vergleichen_jede_objektart_ohne_reihenfolge(typ: str, feld: str, liste: list[Any]) -> None:
    assert _ereignisse_je_typ(typ, {feld: liste}, {feld: list(reversed(liste))}) == []
    # Doppelte Einträge sagen nichts Neues.
    assert _ereignisse_je_typ(typ, {feld: liste}, {feld: [*liste, liste[0]]}) == []
    # Eine andere Menge bleibt eine Änderung.
    (ereignis,) = _ereignisse_je_typ(typ, {feld: liste}, {feld: liste[:1]})
    assert ereignis.payload.get("changed", [feld]) == [feld]


#: Je Objektart ein Feld, dessen Änderung ein Ereignis ergibt
FELD_JE_TYP = {
    "meeting": "participant",
    "paper": "keyword",
    "agendaitem": "auxiliaryFile",
    "consultation": "organization",
    "file": "derivativeFile",
}


@pytest.mark.parametrize("typ", sorted(FELD_JE_TYP))
@pytest.mark.parametrize(
    ("alt", "neu"),
    [(None, []), ([], None), (None, ""), ({}, None), (FEHLT, []), ([], FEHLT)],
    ids=["null-liste", "liste-null", "null-text", "objekt-null", "fehlt-liste", "liste-fehlt"],
)
def test_leere_werte_gelten_als_fehlend(typ: str, alt: Any, neu: Any) -> None:
    """``null``, leere Liste, leeres Objekt, leerer Text und ein fehlendes Feld sind fachlich dasselbe."""
    feld = FELD_JE_TYP[typ]
    assert _ereignisse_je_typ(typ, {feld: alt}, {feld: neu}) == []
    # Ein Wert statt keinem bleibt eine Änderung.
    assert _ereignisse_je_typ(typ, {feld: alt}, {feld: [f"{BASE}/file/9"]}) != []


def test_reihenfolge_eingebetteter_objekte_zaehlt_nicht_ihr_inhalt_schon() -> None:
    zweiter = item(id=f"{BASE}/agendaitem/2", number="2", name="Haushalt")
    alt = meeting(agendaItem=[item(auxiliaryFile=personen(1, 2)), zweiter])
    neu = meeting(agendaItem=[zweiter, item(auxiliaryFile=personen(2, 1), modified="2026-09-30T00:00:00+02:00")])
    assert ris_events.meeting_events(cid(MEETING), neu, Prior(alt)) == []
    neu = meeting(agendaItem=[zweiter, item(name="Mehr Bänke", auxiliaryFile=personen(2, 1))])
    (ereignis,) = ris_events.meeting_events(cid(MEETING), neu, Prior(alt))
    assert ereignis.payload["changed"] == ["agendaItem"]


def test_koordinaten_bleiben_geordnet() -> None:
    """In GeoJSON trägt die Reihenfolge die Bedeutung (Länge vor Breite, Verlauf einer Linie)."""

    def ort(*koordinaten: list[float]) -> dict[str, Any]:
        geojson = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": list(koordinaten)}}
        return {"id": f"{BASE}/location/1", "geojson": geojson}

    alt = paper(location=[ort([7.62, 51.96], [7.63, 51.97])])
    assert ris_events.paper_events(cid(PAPER), paper(location=[ort([7.62, 51.96], [7.63, 51.97])]), Prior(alt)) == []
    for neu in (ort([7.63, 51.97], [7.62, 51.96]), ort([51.96, 7.62], [7.63, 51.97])):
        (ereignis,) = ris_events.paper_events(cid(PAPER), paper(location=[neu]), Prior(alt))
        assert ereignis.payload["changed"] == ["location"]


def test_geaenderte_felder_heissen_wie_in_oparl() -> None:
    neu = paper(name="Mehr Bänke", paperType="Anfrage", reference="A/1")
    (ereignis,) = ris_events.paper_events(cid(PAPER), neu, Prior(paper()))
    assert ereignis.type == "ris.paper.changed"
    assert ereignis.payload == {"paper": str(cid(PAPER)), "changed": ["name", "paperType", "reference"]}


def test_entferntes_feld_ist_eine_aenderung() -> None:
    (ereignis,) = ris_events.paper_events(cid(PAPER), paper(), Prior(paper(reference="A/1")))
    assert ereignis.payload["changed"] == ["reference"]


def test_erweiterungen_mit_namensraum_erscheinen_mit_unterstrich() -> None:
    neu = meeting(**{"mandari:meetingFormat": "hybrid"})
    (ereignis,) = ris_events.meeting_events(cid(MEETING), neu, Prior(meeting()))
    assert ereignis.payload["changed"] == ["mandari_meetingFormat"]


@pytest.mark.parametrize("schluessel", ["Web-Adresse", "x.y", "9lives", "a" * 65, "Name", "mit leerzeichen"])
def test_nicht_darstellbare_feldnamen_werden_nicht_genannt(schluessel: str) -> None:
    assert ris_events.field_name(schluessel) is None
    # Nur ein solches Feld geändert: kein Ereignis, denn changed braucht mindestens einen Namen.
    assert ris_events.paper_events(cid(PAPER), paper(**{schluessel: "neu"}), Prior(paper())) == []
    # Zusammen mit einem darstellbaren Feld bleibt nur dieses.
    (ereignis,) = ris_events.paper_events(cid(PAPER), paper(**{schluessel: "neu", "name": "Neu"}), Prior(paper()))
    assert ereignis.payload["changed"] == ["name"]


def test_hoechstens_64_feldnamen() -> None:
    neu = paper(**{f"feld{i:03d}": i for i in range(100)})
    (ereignis,) = ris_events.paper_events(cid(PAPER), neu, Prior(paper()))
    assert len(ereignis.payload["changed"]) == ris_events.MAX_CHANGED == 64


def test_nutzlast_enthaelt_keine_inhalte() -> None:
    """Nur Kennungen, Codes und Feldnamen: Kein Wert eines geänderten Felds gelangt in das Ereignis."""
    neu = item(name=GEHEIM, public=False, resolutionText=GEHEIM)
    ereignisse = ris_events.agenda_item_events(
        cid(ITEM), neu, Prior(item(), meeting_id=cid(MEETING)), meeting_id=cid(MEETING), public=False
    )
    ereignisse += ris_events.paper_events(cid(PAPER), paper(name=GEHEIM), Prior(paper()))
    ereignisse += ris_events.meeting_events(cid(MEETING), meeting(name=GEHEIM), None)
    assert ereignisse
    for ereignis in ereignisse:
        assert "Mustermann" not in repr(ereignis)


# --- Sitzung und Vorlage ---------------------------------------------------------------------------------


def test_neue_sitzung_ist_angesetzt() -> None:
    (ereignis,) = ris_events.meeting_events(cid(MEETING), meeting(), None)
    assert (ereignis.type, ereignis.aggregate_type, ereignis.aggregate_id) == (
        "ris.meeting.scheduled",
        "Meeting",
        cid(MEETING),
    )
    assert (ereignis.visibility, ereignis.operation, ereignis.version) == ("oeffentlich", "upsert", 1)
    assert ereignis.payload == {"meeting": str(cid(MEETING)), "organizations": [str(cid(ORG))]}


def test_geaenderte_sitzung_nennt_felder_absage_und_gremien() -> None:
    neu = meeting(cancelled=True, organization=[{"id": ORG, "name": "Rat"}, ORG])
    (ereignis,) = ris_events.meeting_events(cid(MEETING), neu, Prior(meeting()))
    assert ereignis.type == "ris.meeting.changed"
    assert ereignis.payload == {
        "meeting": str(cid(MEETING)),
        "changed": ["cancelled", "organization"],
        "cancelled": True,
        "organizations": [str(cid(ORG))],
    }


def test_sitzung_ohne_gremien_und_mit_zu_vielen() -> None:
    (ohne,) = ris_events.meeting_events(cid(MEETING), meeting(organization=[]), None)
    assert "organizations" not in ohne.payload
    viele = meeting(organization=[f"{BASE}/organization/{i}" for i in range(51)])
    (zu_viele,) = ris_events.meeting_events(cid(MEETING), viele, None)
    assert "organizations" not in zu_viele.payload


def test_neu_zugeordnete_gremien_sind_eine_aenderung_der_sitzung() -> None:
    """Die Quelle liefert dasselbe Objekt, aber ein Gremium steht erst jetzt im Bestand."""
    (ereignis,) = ris_events.meeting_events(cid(MEETING), meeting(), Prior(meeting()), organizations_changed=True)
    assert ereignis.type == "ris.meeting.changed"
    assert ereignis.payload == {
        "meeting": str(cid(MEETING)),
        "changed": ["organization"],
        "cancelled": False,
        "organizations": [str(cid(ORG))],
    }
    # Ändert sich das Feld ohnehin, steht es einmal in der Liste.
    (beides,) = ris_events.meeting_events(
        cid(MEETING),
        meeting(organization=[ORG, f"{BASE}/organization/2"]),
        Prior(meeting()),
        organizations_changed=True,
    )
    assert beides.payload["changed"] == ["organization"]
    # Eine neue Sitzung bleibt angesetzt, ihre Gremien gehören dazu.
    (neu,) = ris_events.meeting_events(cid(MEETING), meeting(), None, organizations_changed=True)
    assert neu.type == "ris.meeting.scheduled"


def test_neu_zugeordneter_ort_ist_eine_aenderung_der_vorlage() -> None:
    ort = f"{BASE}/location/1"
    vorlage = paper(location=[ort])
    assert ris_events.paper_events(cid(PAPER), vorlage, Prior(vorlage)) == []
    (ereignis,) = ris_events.paper_events(cid(PAPER), vorlage, Prior(vorlage), locations_changed=True)
    assert ereignis.type == "ris.paper.changed"
    assert ereignis.payload == {"paper": str(cid(PAPER)), "changed": ["location"]}
    (neu,) = ris_events.paper_events(cid(PAPER), vorlage, None, locations_changed=True)
    assert neu.type == "ris.paper.released"


def test_neue_vorlage_ist_veroeffentlicht() -> None:
    (ereignis,) = ris_events.paper_events(cid(PAPER), paper(), None)
    assert (ereignis.type, ereignis.aggregate_type) == ("ris.paper.released", "Paper")
    assert ereignis.payload == {"paper": str(cid(PAPER))}


def test_nach_loeschmarkierung_wieder_geliefert_gilt_als_neu() -> None:
    (vorlage,) = ris_events.paper_events(cid(PAPER), paper(), Prior(paper(), deleted=True))
    assert vorlage.type == "ris.paper.released"
    (sitzung,) = ris_events.meeting_events(cid(MEETING), meeting(), Prior(meeting(), deleted=True))
    assert sitzung.type == "ris.meeting.scheduled"


# --- Tagesordnungspunkt ------------------------------------------------------------------------------------


def _top(neu: dict[str, Any], prior: Prior | None, *, meeting_url: str = MEETING) -> list[ris_events.Draft]:
    return ris_events.agenda_item_events(
        cid(ITEM), neu, prior, meeting_id=cid(meeting_url), public=neu.get("public", True) is not False
    )


def test_neuer_tagesordnungspunkt() -> None:
    (ereignis,) = _top(item(), None)
    assert (ereignis.type, ereignis.aggregate_type, ereignis.visibility) == (
        "ris.agendaitem.changed",
        "AgendaItem",
        "oeffentlich",
    )
    assert ereignis.payload == {"agenda_item": str(cid(ITEM)), "meeting": str(cid(MEETING)), "change": "added"}


def test_geaenderter_tagesordnungspunkt() -> None:
    (ereignis,) = _top(item(name="Bänke", result="vertagt"), Prior(item(), meeting_id=cid(MEETING)))
    assert ereignis.payload["change"] == "changed"
    assert ereignis.payload["changed"] == ["name", "result"]


def test_rueckverweis_auf_die_sitzung_ist_keine_aenderung() -> None:
    """Eingebettet fehlt ``meeting``, in der eigenen Liste steht es: Beide Fassungen sind derselbe Stand."""
    eingebettet, einzeln = item(), item(meeting=MEETING)
    assert _top(einzeln, Prior(eingebettet, meeting_id=cid(MEETING))) == []
    assert _top(eingebettet, Prior(einzeln, meeting_id=cid(MEETING))) == []


def test_verschobener_tagesordnungspunkt_nennt_die_alte_sitzung() -> None:
    andere = f"{BASE}/meeting/2"
    (ereignis,) = _top(item(), Prior(item(), meeting_id=cid(MEETING)), meeting_url=andere)
    assert ereignis.payload == {
        "agenda_item": str(cid(ITEM)),
        "meeting": str(cid(andere)),
        "change": "moved",
        "previous_meeting": str(cid(MEETING)),
    }


def test_nichtoeffentlicher_tagesordnungspunkt_ist_nichtoeffentlich() -> None:
    (ereignis,) = _top(item(public=False), None)
    assert ereignis.visibility == "nichtoeffentlich"
    alt = Prior(item(public=False), meeting_id=cid(MEETING), public=False)
    (geaendert,) = _top(item(public=False, number="2"), alt)
    assert (geaendert.visibility, geaendert.payload["changed"]) == ("nichtoeffentlich", ["number"])


def test_punkt_wird_nichtoeffentlich_oeffentliche_empfaenger_erfahren_die_ruecknahme() -> None:
    ruecknahme, aenderung = _top(item(public=False), Prior(item(), meeting_id=cid(MEETING), public=True))
    assert (ruecknahme.type, ruecknahme.visibility, ruecknahme.operation) == (
        "ris.object.depublished",
        "oeffentlich",
        "delete",
    )
    assert ruecknahme.payload == {"object_type": "AgendaItem", "object": str(cid(ITEM)), "reason": "nichtoeffentlich"}
    assert (aenderung.type, aenderung.visibility) == ("ris.agendaitem.changed", "nichtoeffentlich")
    assert aenderung.payload["changed"] == ["public"]


def test_punkt_wird_oeffentlich_und_erscheint_als_neu() -> None:
    alt = Prior(item(public=False), meeting_id=cid(MEETING), public=False)
    (ereignis,) = _top(item(public=True), alt)
    assert (ereignis.visibility, ereignis.payload["change"]) == ("oeffentlich", "added")


# --- Beratung ------------------------------------------------------------------------------------------------


def consultation(**felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": CONSULTATION,
        "type": "https://schema.oparl.org/1.1/Consultation",
        "role": "Vorberatung",
        "organization": [ORG],
    }
    daten.update(felder)
    return daten


def test_neue_beratung_mit_bezuegen() -> None:
    neu = consultation(meeting=MEETING, agendaItem=ITEM)
    (ereignis,) = ris_events.consultation_events(cid(CONSULTATION), neu, None, paper_id=cid(PAPER))
    assert (ereignis.type, ereignis.aggregate_type) == ("ris.consultation.changed", "Consultation")
    assert ereignis.payload == {
        "consultation": str(cid(CONSULTATION)),
        "paper": str(cid(PAPER)),
        "change": "added",
        "organization": str(cid(ORG)),
        "meeting": str(cid(MEETING)),
        "agenda_item": str(cid(ITEM)),
    }


def test_beratung_vorlage_aus_dem_verweis_der_quelle() -> None:
    (aus_feld,) = ris_events.consultation_events(cid(CONSULTATION), consultation(paper=PAPER), None)
    (aus_angabe,) = ris_events.consultation_events(cid(CONSULTATION), consultation(), None, paper_external_id=PAPER)
    assert aus_feld.payload["paper"] == aus_angabe.payload["paper"] == str(cid(PAPER))


def test_beratung_ohne_vorlage_laesst_sich_nicht_melden() -> None:
    assert ris_events.consultation_events(cid(CONSULTATION), consultation(), None) == []


def test_beratung_terminiert_und_geaendert() -> None:
    alt = Prior(consultation())
    (terminiert,) = ris_events.consultation_events(
        cid(CONSULTATION), consultation(meeting=MEETING), alt, paper_id=cid(PAPER)
    )
    assert (terminiert.payload["change"], terminiert.payload["changed"]) == ("scheduled", ["meeting"])
    (geaendert,) = ris_events.consultation_events(
        cid(CONSULTATION), consultation(role="Entscheidung", authoritative=True), alt, paper_id=cid(PAPER)
    )
    assert (geaendert.payload["change"], geaendert.payload["changed"]) == ("changed", ["authoritative", "role"])


def test_rueckverweis_auf_die_vorlage_ist_keine_aenderung() -> None:
    alt = Prior(consultation())
    assert ris_events.consultation_events(cid(CONSULTATION), consultation(paper=PAPER), alt, paper_id=cid(PAPER)) == []


# --- Datei ---------------------------------------------------------------------------------------------------


def datei(**felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": FILE,
        "type": "https://schema.oparl.org/1.1/File",
        "name": "Anlage 1",
        "fileName": "anlage1.pdf",
        "accessUrl": f"{BASE}/file/1/download",
        "size": 1000,
    }
    daten.update(felder)
    return daten


def test_neue_datei_an_der_vorlage() -> None:
    (ereignis,) = ris_events.file_events(cid(FILE), datei(), None, paper_id=cid(PAPER))
    assert (ereignis.type, ereignis.aggregate_type) == ("ris.file.changed", "File")
    assert ereignis.payload == {"file": str(cid(FILE)), "change": "added", "paper": str(cid(PAPER))}


def test_datei_bezuege_aus_den_rueckverweisen_der_quelle() -> None:
    neu = datei(paper=[PAPER], meeting=[MEETING], agendaItem=[ITEM])
    (ereignis,) = ris_events.file_events(cid(FILE), neu, None)
    assert ereignis.payload == {
        "file": str(cid(FILE)),
        "change": "added",
        "paper": str(cid(PAPER)),
        "meeting": str(cid(MEETING)),
        "agenda_item": str(cid(ITEM)),
    }


@pytest.mark.parametrize(
    ("aenderung", "erwartet"),
    [
        ({"size": 2000}, "replaced"),
        ({"accessUrl": f"{BASE}/file/1/v2"}, "replaced"),
        ({"sha512Checksum": "abc"}, "replaced"),
        ({"name": "Anlage 1 (neu)", "size": 2000}, "replaced"),
        ({"name": "Anlage 1 (neu)"}, "renamed"),
        ({"fileName": "anlage-1.pdf"}, "renamed"),
        ({"license": "CC0"}, None),
        ({"paper": [PAPER]}, None),
        ({"modified": "2026-09-30T00:00:00+02:00"}, None),
    ],
)
def test_geaenderte_datei(aenderung: dict[str, Any], erwartet: str | None) -> None:
    ereignisse = ris_events.file_events(cid(FILE), datei(**aenderung), Prior(datei()))
    assert [e.payload["change"] for e in ereignisse] == ([erwartet] if erwartet else [])


def test_datei_haengt_erstmals_an_vorlage_oder_sitzung() -> None:
    """Zuerst einzeln geliefert, später eingebettet: Die Quelle liefert dasselbe Objekt, die Zuordnung ist neu."""
    einzeln = Prior(datei())
    (an_vorlage,) = ris_events.file_events(cid(FILE), datei(), einzeln, paper_id=cid(PAPER))
    assert an_vorlage.payload == {"file": str(cid(FILE)), "change": "added", "paper": str(cid(PAPER))}
    (an_sitzung,) = ris_events.file_events(cid(FILE), datei(), einzeln, meeting_id=cid(MEETING))
    assert an_sitzung.payload == {"file": str(cid(FILE)), "change": "added", "meeting": str(cid(MEETING))}
    # Hängt sie schon an der Sitzung und kommt die Vorlage dazu, ist auch das neu.
    (dazu,) = ris_events.file_events(cid(FILE), datei(), Prior(datei(), meeting_id=cid(MEETING)), paper_id=cid(PAPER))
    assert dazu.payload["change"] == "added"


def test_bekannte_zuordnung_der_datei_ist_keine_aenderung() -> None:
    an_vorlage = Prior(datei(), paper_id=cid(PAPER), meeting_id=cid(MEETING))
    assert ris_events.file_events(cid(FILE), datei(), an_vorlage, paper_id=cid(PAPER)) == []
    assert ris_events.file_events(cid(FILE), datei(), an_vorlage, meeting_id=cid(MEETING)) == []
    # Einzeln abgeglichen (ohne Zuordnung) bleibt die bisherige stehen.
    assert ris_events.file_events(cid(FILE), datei(), an_vorlage) == []
    # Eine Datei an mehreren Vorlagen trägt die zuletzt abgeglichene; der Wechsel zählt nicht.
    assert ris_events.file_events(cid(FILE), datei(), an_vorlage, paper_id=cid(f"{BASE}/paper/2")) == []
    # Eine neue Fassung bleibt eine neue Fassung, auch wenn die Zuordnung dazukommt.
    (ersetzt,) = ris_events.file_events(cid(FILE), datei(size=2000), Prior(datei()), paper_id=cid(PAPER))
    assert ersetzt.payload["change"] == "replaced"


# --- Löschmarkierung -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("entity_type", sorted(_ENTITY_MODEL_MAP))
def test_loeschmarkierung_jedes_typs(entity_type: str) -> None:
    """Jeder Typ, den der Ingestor als gelöscht markieren kann, meldet seine Rücknahme."""
    kennung = cid(f"{BASE}/{entity_type}/1")
    (ereignis,) = ris_events.depublished_events(entity_type, kennung)
    assert (ereignis.type, ereignis.operation, ereignis.visibility) == (
        "ris.object.depublished",
        "delete",
        "oeffentlich",
    )
    assert ereignis.aggregate_type == ris_events.AGGREGATE_TYPES[entity_type]
    assert ereignis.aggregate_id == kennung
    assert ereignis.payload == {
        "object_type": ereignis.aggregate_type,
        "object": str(kennung),
        "reason": "quelle_geloescht",
    }


def test_loeschmarkierung_eines_nichtoeffentlichen_punkts_bleibt_nichtoeffentlich() -> None:
    """Öffentliche Empfänger haben den Punkt nie gesehen; eine Rücknahme nennte ihnen erstmals seine Kennung."""
    (ereignis,) = ris_events.depublished_events("agendaitem", cid(ITEM), public=False, meeting_id=cid(MEETING))
    assert (ereignis.type, ereignis.visibility, ereignis.operation) == (
        "ris.agendaitem.changed",
        "nichtoeffentlich",
        "delete",
    )
    assert (ereignis.aggregate_type, ereignis.aggregate_id) == ("AgendaItem", cid(ITEM))
    assert ereignis.payload == {"agenda_item": str(cid(ITEM)), "meeting": str(cid(MEETING)), "change": "deleted"}
    # Ohne Sitzung lässt sich die Nutzlast nicht bilden; öffentlich wird trotzdem nichts gemeldet.
    assert ris_events.depublished_events("agendaitem", cid(ITEM), public=False) == []
    # Ein öffentlicher Punkt meldet die Rücknahme wie jeder andere Typ.
    (oeffentlich,) = ris_events.depublished_events("agendaitem", cid(ITEM), public=True, meeting_id=cid(MEETING))
    assert (oeffentlich.type, oeffentlich.visibility) == ("ris.object.depublished", "oeffentlich")
    # Das Kennzeichen gilt nur für Tagesordnungspunkte.
    (vorlage,) = ris_events.depublished_events("paper", cid(PAPER), public=False)
    assert vorlage.type == "ris.object.depublished"


def test_unbekannter_typ_meldet_nichts() -> None:
    assert ris_events.depublished_events("unbekannt", cid(PAPER)) == []


def test_bezug_aus_url_oder_eingebettetem_objekt() -> None:
    assert ris_events.reference(ORG) == ris_events.reference({"id": ORG}) == str(cid(ORG))
    assert ris_events.reference("") is None
    assert ris_events.reference({"name": "ohne Kennung"}) is None
    assert ris_events.references([ORG, {"id": ORG}, None, ""]) == [str(cid(ORG))]


def test_verweise_umgezogener_quellen_tragen_die_kennungen_der_basis() -> None:
    """Issue #733: Verweise unter der neuen Adresse nennen dieselben Kennungen wie die Objekte selbst."""
    neu = "https://neu.example.org/oparl/"
    ids = IdBases({neu: f"{BASE}/"})

    def umgezogen(url: str) -> str:
        return url.replace(f"{BASE}/", neu)

    assert ris_events.reference(umgezogen(ORG), ids) == str(cid(ORG))
    assert ris_events.reference(umgezogen(ORG)) == str(cid(umgezogen(ORG)))

    (sitzung,) = ris_events.meeting_events(cid(MEETING), meeting(organization=[umgezogen(ORG)]), None, ids=ids)
    assert sitzung.payload["organizations"] == [str(cid(ORG))]

    beratung = {"paper": umgezogen(PAPER), "meeting": umgezogen(MEETING), "agendaItem": umgezogen(ITEM)}
    (ereignis,) = ris_events.consultation_events(cid(CONSULTATION), beratung, None, ids=ids)
    assert ereignis.payload == {
        "consultation": str(cid(CONSULTATION)),
        "paper": str(cid(PAPER)),
        "change": "added",
        "meeting": str(cid(MEETING)),
        "agenda_item": str(cid(ITEM)),
    }

    datei = {"paper": [umgezogen(PAPER)], "meeting": [umgezogen(MEETING)], "agendaItem": [umgezogen(ITEM)]}
    (anlage,) = ris_events.file_events(cid(FILE), datei, None, ids=ids)
    assert anlage.payload == {
        "file": str(cid(FILE)),
        "change": "added",
        "paper": str(cid(PAPER)),
        "meeting": str(cid(MEETING)),
        "agenda_item": str(cid(ITEM)),
    }
