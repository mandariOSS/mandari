# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nur additive Änderungen an einer bestehenden Schemaversion (Issue #519, ``hub/contracts/compatibility.py``).

Erlaubt sind neue optionale Felder, neue Codes, neue Sichtbarkeitsklassen und geänderte Erläuterungen;
alles andere bricht Empfänger oder ältere Ereignisse im Journal und braucht eine neue Version.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from hub.contracts import envelope_schema, get_registry
from hub.contracts.compatibility import breaking_changes, contract_changes
from hub.contracts.tests.hilfen import ereignis_schema


def _schema() -> dict[str, Any]:
    schema = ereignis_schema()
    schema["properties"]["reason"] = {"description": "Grund.", "type": "string", "enum": ["a", "b"]}
    schema["properties"]["changed"] = {
        "description": "Felder.",
        "type": "array",
        "items": {"type": "string", "pattern": "^[a-z][A-Za-z0-9_]{0,63}$"},
        "maxItems": 64,
    }
    return schema


def _geaendert(**aenderungen: Any) -> dict[str, Any]:
    schema = copy.deepcopy(_schema())
    for pfad, wert in aenderungen.items():
        knoten = schema
        *weg, letzter = pfad.split("__")
        for schritt in weg:
            knoten = knoten[schritt]
        if wert is None:
            del knoten[letzter]
        else:
            knoten[letzter] = wert
    return schema


def test_unveraenderte_schemas_sind_vertraeglich() -> None:
    assert breaking_changes(_schema(), _schema()) == []


@pytest.mark.parametrize(
    "aenderung",
    [
        {"title": "Neuer Titel"},
        {"description": "Neue Beschreibung."},
        {"examples": []},
        {"x-owner": "hub.ris"},
        {"properties__reason__description": "Anders erläutert."},
        # neues optionales Feld
        {"properties__neu": {"description": "Neu.", "type": "string", "format": "uuid"}},
        # neuer Code in einer Codeliste
        {"properties__reason__enum": ["a", "b", "c"]},
        # Codes in anderer Reihenfolge
        {"properties__reason__enum": ["b", "a"]},
        # weitere Sichtbarkeitsklasse
        {"x-visibility": ["oeffentlich", "nichtoeffentlich", "intern"]},
    ],
)
def test_ergaenzungen_sind_erlaubt(aenderung: dict[str, Any]) -> None:
    assert breaking_changes(_schema(), _geaendert(**aenderung)) == []


@pytest.mark.parametrize(
    ("aenderung", "meldung"),
    [
        ({"properties__reason": None}, "$.reason: Feld entfernt"),
        ({"required": ["paper", "changed", "reason"]}, "neue Pflichtfelder (reason)"),
        ({"required": ["paper"]}, "nicht mehr Pflicht (changed)"),
        ({"properties__paper__format": "date"}, "$.paper.format: geändert"),
        ({"properties__paper__format": None}, "$.paper.format: entfernt"),
        ({"properties__paper__type": "integer"}, "$.paper.type: Typ geändert"),
        ({"properties__paper__type": ["string", "null"]}, "$.paper.type: Typ geändert"),
        ({"properties__reason__enum": ["a"]}, "$.reason.enum: Werte entfernt (b)"),
        ({"properties__reason__enum": None}, "$.reason.enum: entfernt"),
        ({"properties__paper__maxLength": 36}, "$.paper.maxLength: neu eingeführt"),
        ({"properties__changed__maxItems": 32}, "$.changed.maxItems: geändert"),
        ({"properties__changed__items__pattern": "^[a-z]{1,8}$"}, "$.changed.items.pattern: geändert"),
        ({"additionalProperties": True}, "$.additionalProperties: Teilschema geändert"),
        ({"x-visibility": "oeffentlich"}, "$.x-visibility: Sichtbarkeit entfernt (nichtoeffentlich)"),
        ({"x-kind": "command"}, "$.x-kind: geändert"),
    ],
)
def test_brechende_aenderungen_werden_gemeldet(aenderung: dict[str, Any], meldung: str) -> None:
    probleme = breaking_changes(_schema(), _geaendert(**aenderung))

    assert any(meldung in problem for problem in probleme), probleme


def test_neues_pflichtfeld_auch_wenn_es_neu_ist() -> None:
    neu = _geaendert(properties__grund={"description": "Neu.", "type": "string", "format": "uuid"})
    neu["required"] = [*neu["required"], "grund"]

    assert breaking_changes(_schema(), neu) == ["$: neue Pflichtfelder (grund)"]


def test_huelle_wird_mit_denselben_regeln_verglichen() -> None:
    huelle = envelope_schema()
    geaendert = copy.deepcopy(huelle)
    geaendert["$defs"]["uuid"]["pattern"] = "^[0-9a-f]{32}$"
    gelockert = copy.deepcopy(huelle)
    gelockert["properties"]["body_id"]["anyOf"].append({"type": "integer"})

    assert breaking_changes(huelle, huelle) == []
    assert any(p.startswith("$.$defs.uuid.pattern") for p in breaking_changes(huelle, geaendert))
    assert breaking_changes(huelle, gelockert) == ["$.body_id.anyOf: Zahl der Teilschemas geändert"]


def test_ablagen_entfernte_version_bricht_neue_sind_frei() -> None:
    alt = {"ris.paper.changed/v1": _schema(), "envelope/v1": envelope_schema()}
    neu = {
        "ris.paper.changed/v1": _schema(),
        "ris.paper.changed/v2": _geaendert(required=["paper", "changed", "reason"]),
        "ris.paper.archived/v1": _schema(),
        "envelope/v1": envelope_schema(),
    }

    assert contract_changes(alt, neu) == []
    assert contract_changes(neu, alt) == [
        "ris.paper.archived/v1: Version entfernt (das Journal kann noch Ereignisse dieser Version enthalten)",
        "ris.paper.changed/v2: Version entfernt (das Journal kann noch Ereignisse dieser Version enthalten)",
    ]
    assert contract_changes(alt, {**alt, "ris.paper.changed/v1": _geaendert(properties__paper=None)}) == [
        "ris.paper.changed/v1: $.paper: Feld entfernt"
    ]


@pytest.mark.parametrize("name", get_registry().names())
def test_ausgelieferte_schemas_ein_entferntes_pflichtfeld_bricht(name: str) -> None:
    """Jedes ausgelieferte Schema: mit sich selbst verträglich, ein Pflichtfeld weniger bricht."""
    schema = get_registry().latest(name).schema
    ohne = copy.deepcopy(schema)
    feld = ohne["required"][0]
    del ohne["properties"][feld]
    ohne["required"].remove(feld)

    assert breaking_changes(schema, schema) == []
    assert f"$.{feld}: Feld entfernt" in breaking_changes(schema, ohne)
