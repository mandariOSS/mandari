# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ereigniskatalog aus dem Register (Issue #519, ``hub/contracts/catalog.py``)."""

from __future__ import annotations

from pathlib import Path

from hub.contracts import envelope_schema, get_registry, load_registry
from hub.contracts.catalog import describe_type, render_catalog
from hub.contracts.tests.hilfen import ablegen, befehl_schema, ereignis_schema


def test_katalog_nennt_jeden_vertrag_mit_eigentuemer_sichtbarkeit_und_schema() -> None:
    register = get_registry()
    katalog = render_catalog(register, envelope_schema())

    for vertrag in register:
        assert f"### {vertrag.name} v{vertrag.version}" in katalog
        assert f"`{vertrag.owner}`" in katalog
        assert f"(../mandari/hub/contracts/schemas/{vertrag.name}/v{vertrag.version}.json)" in katalog
    assert "## Ereignishülle v1" in katalog
    # Ereignisse vor Befehlen, jeweils nach Namen
    assert katalog.index("## Ereignisse") < katalog.index("## Befehle")


def test_katalog_ist_bei_gleichem_stand_gleich(tmp_path: Path) -> None:
    ablegen(tmp_path, "ris.paper.released", 1, ereignis_schema())
    ablegen(tmp_path, "submission.submit", 1, befehl_schema())
    erster = render_catalog(load_registry(tmp_path), envelope_schema())

    assert render_catalog(load_registry(tmp_path), envelope_schema()) == erster
    assert erster.endswith("\n") and not erster.endswith("\n\n")
    assert "\r" not in erster


def test_neue_version_erscheint_neben_der_alten(tmp_path: Path) -> None:
    ablegen(tmp_path, "ris.paper.released", 1, ereignis_schema())
    ablegen(tmp_path, "ris.paper.released", 2, ereignis_schema("ris.paper.released", 2, title="Neu gefasst"))
    katalog = render_catalog(load_registry(tmp_path), envelope_schema())

    assert "### ris.paper.released v1" in katalog and "### ris.paper.released v2" in katalog
    zeile = "| `ris.paper.released` | Ereignis | 1, 2 | `apps.session` | nichtoeffentlich, oeffentlich | Neu gefasst |"
    assert zeile in katalog


def test_felder_mit_typ_pflicht_und_maskiertem_text(tmp_path: Path) -> None:
    schema = ereignis_schema(description="Typ <bereich>.<objekt>")
    schema["properties"]["reason"] = {"description": "a | b", "type": "string", "enum": ["a", "b"]}
    ablegen(tmp_path, "ris.paper.released", 1, schema)
    katalog = render_catalog(load_registry(tmp_path), envelope_schema())

    assert "| `paper` | ja | Zeichenkette (uuid) |" in katalog
    assert "| `reason` | nein | Code: `a`, `b` | a \\| b |" in katalog
    assert "Typ &lt;bereich&gt;.&lt;objekt&gt;" in katalog


def test_typbeschreibung() -> None:
    wurzel = {"$defs": {"uuid": {"type": "string", "format": "uuid"}}}

    assert describe_type({"$ref": "#/$defs/uuid"}, wurzel) == "Zeichenkette (uuid)"
    assert describe_type({"anyOf": [{"$ref": "#/$defs/uuid"}, {"type": "null"}]}, wurzel) == (
        "Zeichenkette (uuid) oder null"
    )
    assert describe_type({"type": "array", "items": {"type": "string", "format": "date"}, "maxItems": 5}, wurzel) == (
        "Liste aus Zeichenkette (date) (0 bis 5 Einträge)"
    )
    assert describe_type({"type": "integer", "minimum": 1}, wurzel) == "Ganzzahl (1 bis …)"
    assert describe_type({"const": "x"}, wurzel) == 'Konstante `"x"`'
