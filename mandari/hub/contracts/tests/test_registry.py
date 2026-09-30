# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vertragsregister (Issue #517): Laden, Regeln je Schema, Schema je Typ und Version, Prüfen von
Ereignissen und Befehlen.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from hub.contracts import (
    SCHEMA_ROOT,
    ContractError,
    ContractViolationError,
    Envelope,
    UnknownContractError,
    get_registry,
    load_registry,
)
from hub.contracts.registry import Registry
from hub.contracts.tests.hilfen import PAPER_ID, TENANT_REF, ablegen, befehl_schema, ereignis_schema, huelle


def _probleme(wurzel: Path) -> tuple[str, ...]:
    with pytest.raises(ContractError) as info:
        load_registry(wurzel)
    return info.value.problems


def _eines(wurzel: Path, schema: object, name: str = "ris.paper.released", version: int = 1) -> tuple[str, ...]:
    ablegen(wurzel, name, version, schema)
    return _probleme(wurzel)


@pytest.fixture
def register(tmp_path: Path) -> Registry:
    ablegen(tmp_path, "ris.paper.released", 1, ereignis_schema())
    ablegen(
        tmp_path,
        "ris.paper.released",
        2,
        ereignis_schema("ris.paper.released", 2, required=["paper"], examples=[{"paper": PAPER_ID}]),
    )
    ablegen(tmp_path, "submission.submit", 1, befehl_schema())
    return load_registry(tmp_path)


# --- Register liefert Schema je Typ und Version -------------------------------------------------


def test_register_liefert_schema_je_typ_und_version(register: Registry) -> None:
    assert register.schema("ris.paper.released", 1)["required"] == ["paper", "changed"]
    assert register.schema("ris.paper.released", 2)["required"] == ["paper"]
    assert register.versions("ris.paper.released") == (1, 2)
    assert register.latest("ris.paper.released").version == 2
    assert ("ris.paper.released", 1) in register
    assert len(register) == 3


def test_register_trennt_ereignisse_und_befehle(register: Registry) -> None:
    assert register.names("event") == ("ris.paper.released",)
    assert register.names("command") == ("submission.submit",)
    assert [(c.name, c.version) for c in register] == [
        ("ris.paper.released", 1),
        ("ris.paper.released", 2),
        ("submission.submit", 1),
    ]


def test_vertrag_traegt_eigentuemer_und_sichtbarkeit(register: Registry) -> None:
    vertrag = register.get("ris.paper.released", 1)
    assert vertrag.kind == "event"
    assert vertrag.owner == "apps.session"
    assert vertrag.visibility == frozenset({"oeffentlich", "nichtoeffentlich"})
    assert vertrag.title == "Vorlage freigegeben"
    assert vertrag.examples == [{"paper": PAPER_ID, "changed": ["status"]}]
    assert register.get("submission.submit", 1).visibility == frozenset({"nichtoeffentlich"})


def test_schema_ist_eine_kopie(register: Registry) -> None:
    register.schema("ris.paper.released", 1)["required"].append("fremd")
    assert register.schema("ris.paper.released", 1)["required"] == ["paper", "changed"]


def test_unbekannter_vertrag(register: Registry) -> None:
    with pytest.raises(UnknownContractError):
        register.schema("ris.paper.released", 3)
    with pytest.raises(UnknownContractError):
        register.versions("ris.meeting.scheduled")
    with pytest.raises(UnknownContractError):
        register.latest("ris.meeting.scheduled")


def test_ausgelieferte_schemas_halten_die_regeln_ein() -> None:
    get_registry.cache_clear()
    register = get_registry()
    for vertrag in register:
        for beispiel in vertrag.examples:
            register.validate_payload(vertrag.name, vertrag.version, beispiel)


def test_leerer_oder_fehlender_ordner_ergibt_leeres_register(tmp_path: Path) -> None:
    assert len(load_registry(tmp_path)) == 0
    assert len(load_registry(tmp_path / "fehlt")) == 0


def test_andere_dateien_im_ordner_werden_ignoriert(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Hinweise", encoding="utf-8")
    assert len(load_registry(tmp_path)) == 0


# --- Ablage und Aufbau ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relativ", ["v1.json", "ris.paper.released/v01.json", "ris.paper.released/eins.json", "a/b/v1.json"]
)
def test_ablage_muss_typ_und_version_nennen(tmp_path: Path, relativ: str) -> None:
    pfad = tmp_path / relativ
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text("{}", encoding="utf-8")
    assert _probleme(tmp_path) == (f"{relativ}: Ablage muss schemas/<typ>/v<n>.json sein",)


def test_kaputtes_json(tmp_path: Path) -> None:
    pfad = tmp_path / "ris.paper.released" / "v1.json"
    pfad.parent.mkdir()
    pfad.write_text("{nicht json", encoding="utf-8")
    assert _probleme(tmp_path) == ("ris.paper.released/v1.json: kein lesbares JSON",)


def test_alle_fehler_werden_gesammelt(tmp_path: Path) -> None:
    ablegen(tmp_path, "ris.paper.released", 1, ereignis_schema(**{"x-owner": "fremd"}))
    ablegen(tmp_path, "submission.submit", 1, befehl_schema(title=""))
    probleme = _probleme(tmp_path)
    assert any(p.startswith("ris.paper.released v1: x-owner") for p in probleme)
    assert "submission.submit v1: title fehlt" in probleme


@pytest.mark.parametrize(
    ("abweichung", "erwartet"),
    [
        ({"$schema": "http://json-schema.org/draft-07/schema#"}, "$schema muss"),
        ({"$id": "urn:mandari:event:ris.paper.released:v2"}, "$id muss urn:mandari:event:ris.paper.released:v1 sein"),
        ({"title": " "}, "title fehlt"),
        ({"description": None}, "description fehlt"),
        ({"type": "array"}, "type: object"),
        ({"x-kind": "query"}, "x-kind muss"),
        ({"x-owner": None}, "x-owner fehlt"),
        ({"x-owner": "apps.unbekannt"}, "kein bekanntes Modul"),
        ({"x-visibility": None}, "x-visibility fehlt"),
        ({"x-visibility": []}, "x-visibility fehlt"),
        ({"x-visibility": ["geheim"]}, "unbekannte Klasse „geheim“"),
        ({"x-visibility": ["intern", "intern"]}, "doppelt"),
        ({"examples": []}, "examples fehlt"),
        ({"examples": [{"paper": "keine-uuid", "changed": []}]}, "examples[0] $.paper: verletzt „format“"),
        ({"examples": [{"paper": PAPER_ID}]}, "examples[0] $: Pflichtfeld fehlt (changed)"),
        ({"properties": {"paper": {"type": "zahl"}}}, "kein gültiges JSON Schema 2020-12"),
    ],
)
def test_regelverstoesse_je_schema(tmp_path: Path, abweichung: dict[str, Any], erwartet: str) -> None:
    probleme = _eines(tmp_path, ereignis_schema(**abweichung))
    assert any(erwartet in p for p in probleme), probleme
    assert all(p.startswith("ris.paper.released v1: ") for p in probleme)


def test_schema_muss_objekt_sein(tmp_path: Path) -> None:
    assert _eines(tmp_path, ["kein", "objekt"]) == ("ris.paper.released v1: Schema muss ein JSON-Objekt sein",)


def test_name_muss_zur_art_passen(tmp_path: Path) -> None:
    probleme = _eines(tmp_path, befehl_schema("submission.submitted"), name="submission.submitted")
    assert any("Imperativ" in p for p in probleme)
    probleme = _eines(tmp_path / "b", ereignis_schema("ris.paper.release"), name="ris.paper.release")
    assert any("Vergangenheitsform" in p for p in probleme)


def test_doppelter_vertrag_im_register(register: Registry) -> None:
    vertrag = register.get("ris.paper.released", 1)
    with pytest.raises(ContractError) as info:
        Registry([vertrag, vertrag])
    assert info.value.problems == ("ris.paper.released v1: doppelt im Register",)


def test_versionen_eines_typs_haben_dieselbe_art(tmp_path: Path) -> None:
    ablegen(tmp_path, "core.password.reset", 1, ereignis_schema("core.password.reset", **{"x-owner": "apps.accounts"}))
    ablegen(tmp_path, "core.password.reset", 2, befehl_schema("core.password.reset", 2, **{"x-owner": "apps.accounts"}))
    assert _probleme(tmp_path) == ("core.password.reset: Versionen sind teils Ereignis, teils Befehl",)


# --- Eigentümer und Sichtbarkeit ----------------------------------------------------------------


@pytest.mark.parametrize(
    "owner", ["apps.session", "apps.session.commands", "hub", "hub.ris", "ingestor", "insight_core"]
)
def test_bekannte_eigentuemer(tmp_path: Path, owner: str) -> None:
    ablegen(tmp_path, "ris.paper.released", 1, ereignis_schema(**{"x-owner": owner}))
    assert load_registry(tmp_path).get("ris.paper.released", 1).owner == owner


@pytest.mark.parametrize(
    ("name", "owner", "erwartet"),
    [
        ("session.allowance.approved", "apps.work", "Bereich „session“ gehört apps.session"),
        ("work.task.assigned", "apps.session", "Bereich „work“ gehört apps.work"),
        ("portal.question.answered", "insight_core", "Bereich „portal“ gehört apps.portal"),
        ("core.user.registered", "apps.session", "Bereich „core“ gehört der Plattform"),
        ("ris.paper.released", "apps.sessionx", "kein bekanntes Modul"),
    ],
)
def test_eigentuemer_passt_zum_bereich(tmp_path: Path, name: str, owner: str, erwartet: str) -> None:
    schema = ereignis_schema(name, **{"x-owner": owner, "x-visibility": "intern"})
    probleme = _eines(tmp_path, schema, name=name)
    assert any(erwartet in p for p in probleme), probleme


@pytest.mark.parametrize("name", ["session.allowance.approved", "work.task.assigned", "portal.question.answered"])
def test_interne_bereiche_sind_nie_oeffentlich(tmp_path: Path, name: str) -> None:
    owner = {"session": "apps.session", "work": "apps.work", "portal": "apps.portal"}[name.split(".")[0]]
    schema = ereignis_schema(name, **{"x-owner": owner, "x-visibility": ["intern", "oeffentlich"]})
    probleme = _eines(tmp_path, schema, name=name)
    assert any("ist intern und darf nicht oeffentlich sein" in p for p in probleme), probleme


# --- Keine Freitextfelder bei nichtöffentlichen und personenbezogenen Daten ---------------------


def _mit_feld(feld: object, sichtbarkeit: str = "personenbezogen", beispiel: object = PAPER_ID) -> dict[str, Any]:
    return ereignis_schema(
        "attendance.response_recorded",
        **{
            "x-visibility": sichtbarkeit,
            "required": ["feld"],
            "properties": {"feld": feld},
            "examples": [{"feld": beispiel}],
        },
    )


@pytest.mark.parametrize(
    "feld",
    [
        {"type": "string", "format": "uuid"},
        {"type": "string", "enum": ["zugesagt", "abgesagt"]},
        {"const": "zugesagt"},
        {"type": "string", "pattern": "^[a-z_]+$"},
        {"type": ["string", "null"], "format": "date-time"},
        {"type": "integer"},
        {"type": "array", "items": {"type": "string", "format": "uuid"}},
        {"type": "object", "additionalProperties": False, "properties": {"id": {"type": "string", "format": "uuid"}}},
        {"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]},
        {"$ref": "#/$defs/kennung"},
    ],
)
def test_kennungen_und_codes_sind_erlaubt(tmp_path: Path, feld: dict[str, Any]) -> None:
    schema = _mit_feld(feld)
    schema["$defs"] = {"kennung": {"type": "string", "format": "uuid"}}
    schema["examples"] = []  # Beispiele sind hier nicht Gegenstand
    probleme = _eines(tmp_path, schema, name="attendance.response_recorded")
    assert probleme == ("attendance.response_recorded v1: examples fehlt (mindestens ein Beispiel)",)


@pytest.mark.parametrize(
    ("feld", "stelle"),
    [
        ({"type": "string"}, "#/properties/feld (Zeichenkette"),
        ({"type": "string", "maxLength": 20}, "#/properties/feld (Zeichenkette"),
        ({"type": "string", "format": "email"}, "#/properties/feld (Zeichenkette"),
        ({"type": "string", "pattern": "^.*$"}, "#/properties/feld (Zeichenkette"),
        ({"type": "string", "pattern": "[a-z]"}, "#/properties/feld (Zeichenkette"),
        ({}, "#/properties/feld (ohne Typ"),
        (True, "#/properties/feld (true"),
        ({"type": "array"}, "#/properties/feld (Liste"),
        ({"type": "array", "items": {"type": "string"}}, "#/properties/feld/items (Zeichenkette"),
        ({"type": "object"}, "#/properties/feld (Objekt"),
        ({"anyOf": [{"type": "string"}, {"type": "null"}]}, "#/properties/feld/anyOf/0 (Zeichenkette"),
    ],
)
def test_freitext_ist_bei_personenbezogenen_daten_verboten(tmp_path: Path, feld: object, stelle: str) -> None:
    schema = _mit_feld(feld)
    schema["examples"] = []
    probleme = _eines(tmp_path, schema, name="attendance.response_recorded")
    assert any(f"Freitext bei Sichtbarkeit personenbezogen: {stelle}" in p for p in probleme), probleme


def test_offenes_objekt_ist_bei_nichtoeffentlich_verboten(tmp_path: Path) -> None:
    schema = befehl_schema()
    del schema["additionalProperties"]
    probleme = _eines(tmp_path, schema, name="submission.submit")
    assert probleme == (
        "submission.submit v1: Freitext bei Sichtbarkeit nichtoeffentlich: # (Objekt ohne additionalProperties: false)",
    )


def test_freitext_ist_bei_oeffentlichen_und_internen_daten_erlaubt(tmp_path: Path) -> None:
    schema = _mit_feld({"type": "string"}, sichtbarkeit="oeffentlich", beispiel="Sitzung verlegt")
    schema["x-visibility"] = ["oeffentlich", "intern"]
    ablegen(tmp_path, "attendance.response_recorded", 1, schema)
    assert load_registry(tmp_path).get("attendance.response_recorded", 1).visibility == {"oeffentlich", "intern"}


# --- Ereignisse und Befehle prüfen --------------------------------------------------------------


def test_gueltiges_ereignis(register: Registry) -> None:
    register.validate_event(huelle())
    register.validate_event(
        Envelope(
            type="ris.paper.released",
            version=2,
            aggregate_type="Paper",
            aggregate_id=uuid.UUID(PAPER_ID),
            tenant_ref=TENANT_REF,
            visibility="nichtoeffentlich",
            payload={"paper": PAPER_ID},
        )
    )


def test_ereignis_mit_ungueltiger_nutzlast_nennt_keine_werte(register: Registry) -> None:
    geheim = "Erika Mustermann, Musterweg 1"
    with pytest.raises(ContractViolationError) as info:
        register.validate_event(huelle(payload={"paper": geheim, "changed": [geheim], "notiz": geheim}))
    assert info.value.subject == "ris.paper.released v1"
    assert geheim not in str(info.value)
    assert set(info.value.problems) == {
        "$: nicht vorgesehene Felder (notiz)",
        "$.paper: verletzt „format“",
        "$.changed[0]: verletzt „pattern“",
    }


def test_ereignis_mit_ungueltiger_huelle(register: Registry) -> None:
    with pytest.raises(ContractViolationError) as info:
        register.validate_event(huelle(visibility=None))
    assert info.value.subject == "Ereignishülle"


def test_ereignis_ohne_vertrag(register: Registry) -> None:
    with pytest.raises(UnknownContractError):
        register.validate_event(huelle(type="ris.meeting.scheduled"))
    with pytest.raises(UnknownContractError):
        register.validate_event(huelle(version=3))


def test_ereignis_mit_nicht_vorgesehener_sichtbarkeit(register: Registry) -> None:
    with pytest.raises(ContractViolationError) as info:
        register.validate_event(huelle(visibility="personenbezogen"))
    assert info.value.problems == (
        "Sichtbarkeit personenbezogen ist nicht vorgesehen (erlaubt: nichtoeffentlich, oeffentlich)",
    )


def test_befehl_ist_kein_ereignis(register: Registry) -> None:
    with pytest.raises(ContractViolationError) as info:
        register.validate_event(huelle(type="submission.submit", payload={"document": PAPER_ID}))
    assert info.value.problems == ("ist ein Befehl, kein Ereignis",)
    with pytest.raises(ContractViolationError) as info:
        register.validate_command("ris.paper.released", 1, {"paper": PAPER_ID, "changed": []})
    assert info.value.problems == ("ist ein Ereignis, kein Befehl",)


def test_befehl_pruefen(register: Registry) -> None:
    register.validate_command("submission.submit", 1, {"document": PAPER_ID})
    with pytest.raises(ContractViolationError) as info:
        register.validate_command("submission.submit", 1, {})
    assert info.value.problems == ("$: Pflichtfeld fehlt (document)",)


def test_ausgeliefertes_register_liegt_im_paket() -> None:
    assert SCHEMA_ROOT.name == "schemas"
    assert SCHEMA_ROOT.parent.name == "contracts"
    assert (SCHEMA_ROOT / "README.md").is_file()
