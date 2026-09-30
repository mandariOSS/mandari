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
CONSULTATION, ORG = f"{BASE}/consultation/1", f"{BASE}/organization/1"


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
    assert len(namen) == 24 + len(ris_events.AGGREGATE_TYPES)
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
    for typ in ("ris.paper.changed", "ris.meeting.changed", "ris.agendaitem.changed", "ris.consultation.changed"):
        changed = register.schema(typ, 1)["properties"]["changed"]
        assert changed["items"]["pattern"] == ris_events._FIELD_NAME.pattern
        assert changed["maxItems"] == ris_events.MAX_CHANGED
    organisationen = register.schema("ris.meeting.changed", 1)["properties"]["organizations"]
    assert organisationen["maxItems"] == ris_events.MAX_ORGANIZATIONS
