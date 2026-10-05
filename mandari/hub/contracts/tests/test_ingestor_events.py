# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vertragstest für den Ingestor als Produzenten von ``ris.*``-Ereignissen (Issue #513).

Der Ingestor ist ein eigenes Programm ohne Zugriff auf das Register. Welche Ereignisse er aus einer
Änderung am RIS-Bestand bildet, steht in ``ingestor/src/storage/ris_events.py``; das Modul braucht nur
die Standardbibliothek und ``mandari_oparl`` und wird hier direkt geladen. Jedes Ereignis, das er
bilden kann, muss zum ausgelieferten Vertrag passen: Typ und Version, Sichtbarkeit, Nutzlast. Ändert
sich ein ``ris.*``-Schema unverträglich, scheitert dieser Test, nicht erst der Betrieb.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from mandari_oparl.ids import canonical_id

from hub.contracts import EVENT, Envelope, get_registry

INGESTOR_DIR = Path(__file__).resolve().parents[4] / "ingestor"
TENANT = "source:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"
BODY = uuid.UUID("7e8f9a0b-1c2d-5e3f-8a4b-5c6d7e8f9a0b")
BASE = "https://ris.example.org/oparl"
MEETING, PAPER, ITEM, FILE = (f"{BASE}/{art}/1" for art in ("meeting", "paper", "agendaitem", "file"))
CONSULTATION, ORG, PERSON = f"{BASE}/consultation/1", f"{BASE}/organization/1", f"{BASE}/person/1"
MEMBERSHIP, LOCATION, TERM = f"{BASE}/membership/1", f"{BASE}/location/1", f"{BASE}/legislativeterm/1"
KOMMUNE = f"{BASE}/body/1"


def _laden() -> ModuleType:
    pfad = INGESTOR_DIR / "src" / "storage" / "ris_events.py"
    spec = importlib.util.spec_from_file_location("ingestor_ris_events", pfad)
    assert spec is not None and spec.loader is not None, f"nicht gefunden: {pfad}"
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


ris_events = _laden()
Prior = ris_events.Prior


def cid(url: str) -> uuid.UUID:
    return canonical_id(url)


def _alle_ereignisse() -> list[tuple[str, Any]]:
    """Je ein Beispiel für jeden Weg, auf dem der Ingestor ein Ereignis bildet."""
    sitzung = {"id": MEETING, "name": "Rat", "organization": [ORG], "start": "2026-10-01T17:00:00+02:00"}
    vorlage = {"id": PAPER, "name": "Mehr Bänke im Park", "paperType": "Antrag"}
    punkt = {"id": ITEM, "number": "1", "name": "Mehr Bänke im Park", "public": True}
    beratung = {"id": CONSULTATION, "role": "Vorberatung", "organization": [ORG]}
    datei = {"id": FILE, "name": "Anlage 1", "accessUrl": f"{FILE}/download", "size": 1000}
    gremium = {"id": ORG, "name": "Rat", "organizationType": "Gremium", "membership": [f"{BASE}/membership/1"]}
    person = {"id": PERSON, "name": "Ratsmitglied", "familyName": "Muster", "membership": [f"{BASE}/membership/1"]}
    mitgliedschaft = {"id": MEMBERSHIP, "person": PERSON, "organization": ORG, "role": "Mitglied"}
    ort = {"id": LOCATION, "description": "Stadtpark", "geojson": {"type": "Point", "coordinates": [7.6, 51.9]}}
    wahlperiode = {"id": TERM, "name": "2025 bis 2030", "startDate": "2025-11-01", "body": KOMMUNE}
    kommune = {"id": KOMMUNE, "name": "Musterstadt", "legislativeTerm": [wahlperiode]}
    viele_felder = vorlage | {f"feld{i:03d}": i for i in range(100)} | {"mandari:publicAccess": "stream"}
    sitzung_id, vorlage_id, punkt_id = cid(MEETING), cid(PAPER), cid(ITEM)
    gruppen: dict[str, list[Any]] = {
        "Sitzung neu": ris_events.meeting_events(sitzung_id, sitzung, None),
        "Sitzung ohne Gremien": ris_events.meeting_events(sitzung_id, {"id": MEETING}, None),
        "Sitzung abgesagt": ris_events.meeting_events(sitzung_id, sitzung | {"cancelled": True}, Prior(sitzung)),
        "Sitzung mit Erweiterung": ris_events.meeting_events(
            sitzung_id, sitzung | {"mandari:meetingFormat": "hybrid"}, Prior(sitzung)
        ),
        "Sitzung mit neu zugeordnetem Gremium": ris_events.meeting_events(
            sitzung_id, sitzung, Prior(sitzung), organizations_changed=True
        ),
        "Vorlage neu": ris_events.paper_events(vorlage_id, vorlage, None),
        "Vorlage wieder geliefert": ris_events.paper_events(vorlage_id, vorlage, Prior(vorlage, deleted=True)),
        "Vorlage geändert": ris_events.paper_events(vorlage_id, vorlage | {"name": "Bänke"}, Prior(vorlage)),
        "Vorlage mit vielen Feldern": ris_events.paper_events(vorlage_id, viele_felder, Prior(vorlage)),
        "Vorlage mit neu zugeordnetem Ort": ris_events.paper_events(
            vorlage_id, vorlage, Prior(vorlage), locations_changed=True
        ),
        "Punkt neu": ris_events.agenda_item_events(punkt_id, punkt, None, meeting_id=sitzung_id, public=True),
        "Punkt neu, nichtöffentlich": ris_events.agenda_item_events(
            punkt_id, punkt | {"public": False}, None, meeting_id=sitzung_id, public=False
        ),
        "Punkt geändert": ris_events.agenda_item_events(
            punkt_id,
            punkt | {"result": "vertagt"},
            Prior(punkt, meeting_id=sitzung_id),
            meeting_id=sitzung_id,
            public=True,
        ),
        "Punkt verschoben": ris_events.agenda_item_events(
            punkt_id, punkt, Prior(punkt, meeting_id=sitzung_id), meeting_id=cid(f"{BASE}/meeting/2"), public=True
        ),
        "Punkt wird nichtöffentlich": ris_events.agenda_item_events(
            punkt_id,
            punkt | {"public": False},
            Prior(punkt, meeting_id=sitzung_id, public=True),
            meeting_id=sitzung_id,
            public=False,
        ),
        "Punkt wird öffentlich": ris_events.agenda_item_events(
            punkt_id,
            punkt,
            Prior(punkt | {"public": False}, meeting_id=sitzung_id, public=False),
            meeting_id=sitzung_id,
            public=True,
        ),
        "Beratung neu": ris_events.consultation_events(
            cid(CONSULTATION), beratung | {"meeting": MEETING, "agendaItem": ITEM}, None, paper_id=vorlage_id
        ),
        "Beratung terminiert": ris_events.consultation_events(
            cid(CONSULTATION), beratung | {"meeting": MEETING}, Prior(beratung), paper_external_id=PAPER
        ),
        "Beratung geändert": ris_events.consultation_events(
            cid(CONSULTATION), beratung | {"role": "Entscheidung"}, Prior(beratung), paper_id=vorlage_id
        ),
        "Datei neu": ris_events.file_events(
            cid(FILE), datei | {"agendaItem": [ITEM]}, None, paper_id=vorlage_id, meeting_id=sitzung_id
        ),
        "Datei ersetzt": ris_events.file_events(cid(FILE), datei | {"size": 2000}, Prior(datei)),
        "Datei umbenannt": ris_events.file_events(cid(FILE), datei | {"name": "Anlage A"}, Prior(datei)),
        "Datei erstmals an einer Vorlage": ris_events.file_events(cid(FILE), datei, Prior(datei), paper_id=vorlage_id),
        "Text erkannt": ris_events.text_extracted_events(cid(FILE), "tesseract", 18342),
        "Text erkannt, Verfahren nicht darstellbar": ris_events.text_extracted_events(cid(FILE), "OCR (alt)", None),
        "Gremium neu": ris_events.organization_events(cid(ORG), gremium, None),
        "Gremium wieder geliefert": ris_events.organization_events(cid(ORG), gremium, Prior(gremium, deleted=True)),
        "Gremium umbenannt": ris_events.organization_events(
            cid(ORG), gremium | {"name": "Rat der Stadt"}, Prior(gremium)
        ),
        "Person neu": ris_events.person_events(cid(PERSON), person, None),
        "Person geändert": ris_events.person_events(
            cid(PERSON), person | {"title": ["Dr."], "membership": [f"{BASE}/membership/2"]}, Prior(person)
        ),
        "Mitgliedschaft neu": ris_events.membership_events(
            cid(MEMBERSHIP), mitgliedschaft, None, person_id=cid(PERSON), organization_id=cid(ORG)
        ),
        "Mitgliedschaft beendet": ris_events.membership_events(
            cid(MEMBERSHIP),
            mitgliedschaft | {"endDate": "2026-09-30"},
            Prior(mitgliedschaft),
            person_id=cid(PERSON),
            organization_id=cid(ORG),
        ),
        "Ort neu": ris_events.location_events(cid(LOCATION), ort, None),
        "Ort verlegt": ris_events.location_events(
            cid(LOCATION), ort | {"geojson": {"type": "Point", "coordinates": [7.7, 51.9]}}, Prior(ort)
        ),
        "Wahlperiode neu": ris_events.legislative_term_events(cid(TERM), wahlperiode, None),
        "Wahlperiode geändert": ris_events.legislative_term_events(
            cid(TERM), wahlperiode | {"endDate": "2030-10-31"}, Prior(wahlperiode)
        ),
        "Kommune neu": ris_events.body_events(BODY, kommune, None),
        "Kommune geändert": ris_events.body_events(
            BODY, kommune | {"website": "https://musterstadt.example.org"}, Prior(kommune)
        ),
        "Löschmarkierung eines nichtöffentlichen Punkts": ris_events.depublished_events(
            "agendaitem", punkt_id, public=False, meeting_id=sitzung_id
        ),
    }
    for entity_type in ris_events.AGGREGATE_TYPES:
        gruppen[f"Löschmarkierung {entity_type}"] = ris_events.depublished_events(
            entity_type, cid(f"{BASE}/{entity_type}/1")
        )
    return [(name, ereignis) for name, ereignisse in gruppen.items() for ereignis in ereignisse]


EREIGNISSE = _alle_ereignisse()


def test_jeder_weg_bildet_ein_ereignis() -> None:
    namen = {name for name, _ in EREIGNISSE}
    assert len(namen) == 39 + len(ris_events.AGGREGATE_TYPES)
    # Wechsel in den nichtöffentlichen Teil: Rücknahme für öffentliche Empfänger und die Änderung selbst
    assert [e.type for name, e in EREIGNISSE if name == "Punkt wird nichtöffentlich"] == [
        "ris.object.depublished",
        "ris.agendaitem.changed",
    ]
    # Löschmarkierung eines nichtöffentlichen Punkts: keine öffentliche Rücknahme, nur die Änderung
    assert [
        (e.type, e.visibility, e.operation, e.payload["change"])
        for name, e in EREIGNISSE
        if name == "Löschmarkierung eines nichtöffentlichen Punkts"
    ] == [("ris.agendaitem.changed", "nichtoeffentlich", "delete", "deleted")]


@pytest.mark.parametrize(("name", "ereignis"), EREIGNISSE, ids=[f"{name}: {e.type}" for name, e in EREIGNISSE])
def test_ereignis_des_ingestors_entspricht_dem_vertrag(name: str, ereignis: Any) -> None:
    get_registry().validate_event(
        Envelope(
            type=ereignis.type,
            version=ereignis.version,
            aggregate_type=ereignis.aggregate_type,
            aggregate_id=ereignis.aggregate_id,
            tenant_ref=TENANT,
            body_id=BODY,
            visibility=ereignis.visibility,
            operation=ereignis.operation,
            actor_ref="system:ingestor",
            payload=ereignis.payload,
        )
    )


def test_ingestor_meldet_nur_vertraege_der_drehscheibe() -> None:
    """``ris.*`` gehört ``hub.ris``: Session und Ingestor erzeugen dieselben Typen."""
    register = get_registry()
    typen = {(ereignis.type, ereignis.version) for _, ereignis in EREIGNISSE}
    assert typen == {
        ("ris.meeting.scheduled", 1),
        ("ris.meeting.changed", 1),
        ("ris.paper.released", 1),
        ("ris.paper.changed", 1),
        ("ris.agendaitem.changed", 1),
        ("ris.consultation.changed", 1),
        ("ris.file.changed", 1),
        ("ris.file.text_extracted", 1),
        ("ris.organization.changed", 1),
        ("ris.person.changed", 1),
        ("ris.membership.changed", 1),
        ("ris.location.changed", 1),
        ("ris.legislativeterm.changed", 1),
        ("ris.body.changed", 1),
        ("ris.object.depublished", 1),
    }
    for typ, version in typen:
        vertrag = register.get(typ, version)
        assert (vertrag.kind, vertrag.owner) == (EVENT, "hub.ris")


def test_loeschmarkierung_kennt_jeden_typ_des_vertrags_ausser_der_abstimmung() -> None:
    """Der Ingestor schreibt die OParl-1.1-Typen; ``Voting`` ist eine Erweiterung aus Session."""
    erlaubt = set(get_registry().schema("ris.object.depublished", 1)["properties"]["object_type"]["enum"])
    assert set(ris_events.AGGREGATE_TYPES.values()) == erlaubt - {"Voting"}


def test_feldnamen_wie_im_vertrag() -> None:
    """Das Muster für ``changed`` ist im Ingestor dasselbe wie in den Verträgen."""
    register = get_registry()
    for typ in (
        "ris.paper.changed",
        "ris.meeting.changed",
        "ris.agendaitem.changed",
        "ris.consultation.changed",
        "ris.organization.changed",
        "ris.person.changed",
        "ris.membership.changed",
        "ris.location.changed",
        "ris.legislativeterm.changed",
        "ris.body.changed",
    ):
        changed = register.schema(typ, 1)["properties"]["changed"]
        assert changed["items"]["pattern"] == ris_events._FIELD_NAME.pattern
        assert changed["maxItems"] == ris_events.MAX_CHANGED
    organisationen = register.schema("ris.meeting.changed", 1)["properties"]["organizations"]
    assert organisationen["maxItems"] == ris_events.MAX_ORGANIZATIONS


def test_gremium_und_person_nur_bei_fachlicher_aenderung() -> None:
    """Derselbe Vergleich wie für die übrigen Typen (#822): Reihenfolge, leere Werte und Zeitstempel zählen nicht."""
    gremium = {"id": ORG, "name": "Rat", "membership": [f"{BASE}/membership/1", f"{BASE}/membership/2"]}
    gleich = gremium | {"membership": gremium["membership"][::-1], "website": "", "modified": "2026-10-05T08:00:00Z"}
    assert ris_events.organization_events(cid(ORG), gleich, Prior(gremium)) == []
    person = {"id": PERSON, "name": "Ratsmitglied", "title": []}
    assert ris_events.person_events(cid(PERSON), person | {"title": None}, Prior(person)) == []
    (umbenannt,) = ris_events.organization_events(cid(ORG), gremium | {"name": "Rat der Stadt"}, Prior(gremium))
    assert umbenannt.payload == {"organization": str(cid(ORG)), "change": "changed", "changed": ["name"]}


def test_texterkennung_wie_im_auftrag_der_anwendung() -> None:
    """Ingestor und Auftrag ``file.extract_text`` (``hub.ris.text_extraction``) melden denselben Text gleich."""
    from hub.ris import text_extraction

    schema = get_registry().schema("ris.file.text_extracted", 1)
    vertrag = schema["properties"]["method"]
    assert vertrag["pattern"] == ris_events._METHOD.pattern == text_extraction._METHOD.pattern
    # Der Vertrag nennt beide Erzeuger (nicht mehr „nur der Auftrag“)
    assert "OCR-Worker des Ingestors" in schema["description"] and "file.extract_text" in schema["description"]
    assert ris_events.METHOD_UNKNOWN == text_extraction.METHOD_UNKNOWN
    for verfahren, zeichen in (("pypdf", 18342), ("Tesseract", 12), ("OCR (alt)", 0), (None, None), ("x" * 40, 5)):
        (ereignis,) = ris_events.text_extracted_events(cid(FILE), verfahren, zeichen)
        assert ereignis.payload == text_extraction.payload(cid(FILE), verfahren, zeichen)
        assert (ereignis.type, ereignis.visibility, ereignis.aggregate_type) == (
            text_extraction.TEXT_EXTRACTED,
            "intern",
            "File",
        )


def test_uebrige_typen_nur_bei_fachlicher_aenderung() -> None:
    """Mitgliedschaft, Ort, Wahlperiode, Kommune (#553): Vergleich wie bei den übrigen, Rückverweise zählen nicht."""
    mitgliedschaft = {"id": MEMBERSHIP, "person": PERSON, "organization": ORG, "role": "Mitglied"}
    # In der Person eingebettet fehlt der Rückverweis auf die Person
    eingebettet = {key: wert for key, wert in mitgliedschaft.items() if key != "person"}
    assert ris_events.membership_events(cid(MEMBERSHIP), eingebettet, Prior(mitgliedschaft)) == []
    assert ris_events.membership_events(cid(MEMBERSHIP), mitgliedschaft, Prior(eingebettet)) == []
    ort = {"id": LOCATION, "description": "Stadtpark", "papers": [PAPER], "meetings": [MEETING]}
    assert ris_events.location_events(cid(LOCATION), {"id": LOCATION, "description": "Stadtpark"}, Prior(ort)) == []
    wahlperiode = {"id": TERM, "name": "2025 bis 2030", "body": f"{BASE}/body/1"}
    assert ris_events.legislative_term_events(cid(TERM), wahlperiode | {"body": None}, Prior(wahlperiode)) == []
    kommune = {"id": f"{BASE}/body/1", "name": "Musterstadt", "legislativeTerm": [wahlperiode, {"id": "x"}]}
    umsortiert = kommune | {"legislativeTerm": kommune["legislativeTerm"][::-1], "modified": "2026-10-05"}
    assert ris_events.body_events(BODY, umsortiert, Prior(kommune)) == []
    (rolle,) = ris_events.membership_events(
        cid(MEMBERSHIP), mitgliedschaft | {"role": "Vorsitz"}, Prior(mitgliedschaft), person_id=cid(PERSON)
    )
    assert rolle.payload == {
        "membership": str(cid(MEMBERSHIP)),
        "change": "changed",
        "changed": ["role"],
        "person": str(cid(PERSON)),
    }
