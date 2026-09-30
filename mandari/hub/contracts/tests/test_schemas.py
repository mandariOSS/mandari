# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ausgelieferte Schemas des Startumfangs (Issue #518): 27 Ereignistypen und vier Befehle mit
Eigentümer, Sichtbarkeit und Beispielen; keine Freitextfelder bei nichtöffentlichen und
personenbezogenen Daten.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest

from hub.contracts import COMMAND, EVENT, Contract, Envelope, get_registry
from hub.contracts.envelope import RESTRICTED_VISIBILITIES
from hub.contracts.registry import Registry
from hub.contracts.rules import content_paths, free_text_paths

OE = "oeffentlich"
NOE = "nichtoeffentlich"
INTERN = "intern"
PB = "personenbezogen"

#: Startumfang laut Ereigniskatalog und ADR Befehle: Art, Eigentümer und Sichtbarkeit je Typ.
STARTUMFANG: dict[str, tuple[str, str, frozenset[str]]] = {
    "ris.meeting.scheduled": (EVENT, "hub.ris", frozenset({OE, NOE})),
    "ris.meeting.changed": (EVENT, "hub.ris", frozenset({OE, NOE})),
    "ris.meeting.invited": (EVENT, "hub.ris", frozenset({NOE})),
    "ris.agendaitem.changed": (EVENT, "hub.ris", frozenset({OE, NOE})),
    "ris.paper.created": (EVENT, "hub.ris", frozenset({NOE})),
    "ris.paper.released": (EVENT, "hub.ris", frozenset({OE, NOE})),
    "ris.paper.changed": (EVENT, "hub.ris", frozenset({OE, NOE})),
    "ris.consultation.changed": (EVENT, "hub.ris", frozenset({OE, NOE})),
    "ris.file.changed": (EVENT, "hub.ris", frozenset({OE, NOE})),
    "ris.file.text_extracted": (EVENT, "hub.ris", frozenset({INTERN})),
    "ris.voting.recorded": (EVENT, "hub.ris", frozenset({OE, NOE})),
    "ris.resolution.adopted": (EVENT, "hub.ris", frozenset({OE, NOE})),
    "ris.resolution.implementation_changed": (EVENT, "hub.ris", frozenset({OE})),
    "ris.protocol.approved": (EVENT, "hub.ris", frozenset({NOE})),
    "ris.protocol.published": (EVENT, "hub.ris", frozenset({OE})),
    "ris.object.depublished": (EVENT, "hub.ris", frozenset({OE})),
    "ris.source.published": (EVENT, "hub.ris", frozenset({OE})),
    "submission.received": (EVENT, "apps.session", frozenset({NOE})),
    "submission.status_changed": (EVENT, "apps.session", frozenset({NOE})),
    "attendance.response_recorded": (EVENT, "apps.session", frozenset({PB})),
    "session.allowance.approved": (EVENT, "apps.session", frozenset({PB})),
    "session.payment.exported": (EVENT, "apps.session", frozenset({PB})),
    "work.document.status_changed": (EVENT, "apps.work", frozenset({INTERN})),
    "work.factionmeeting.invited": (EVENT, "apps.work", frozenset({INTERN})),
    "work.task.assigned": (EVENT, "apps.work", frozenset({INTERN})),
    "core.membership.changed": (EVENT, "apps.tenants", frozenset({PB})),
    "core.user.registered": (EVENT, "apps.accounts", frozenset({PB})),
    "submission.submit": (COMMAND, "apps.session", frozenset({NOE})),
    "submission.withdraw": (COMMAND, "apps.session", frozenset({NOE})),
    "attendance.respond": (COMMAND, "apps.session", frozenset({PB})),
    "invitation.acknowledge": (COMMAND, "apps.session", frozenset({PB})),
}

#: Die einzigen Inhaltsfelder (x-content). Ein neues Inhaltsfeld ist eine bewusste Entscheidung.
INHALTSFELDER: dict[str, list[str]] = {
    "submission.submit": [
        "#/properties/title",
        "#/properties/resolution_proposal",
        "#/properties/justification",
        "#/properties/financial_impact",
        "#/properties/urgency_reason",
        "#/properties/submitter",
        "#/properties/co_signers",
    ],
    "attendance.respond": ["#/properties/reason"],
}

_FELDNAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
_ALLE = sorted(STARTUMFANG)


@pytest.fixture(scope="module")
def register() -> Registry:
    get_registry.cache_clear()
    return get_registry()


def _vertrag(register: Registry, name: str) -> Contract:
    return register.latest(name)


def test_register_enthaelt_genau_den_startumfang(register: Registry) -> None:
    assert len(register.names(EVENT)) == 27
    assert len(register.names(COMMAND)) == 4
    assert set(register.names()) == set(STARTUMFANG)
    assert all(register.versions(name) == (1,) for name in STARTUMFANG)


@pytest.mark.parametrize("name", _ALLE)
def test_art_eigentuemer_und_sichtbarkeit_je_typ(register: Registry, name: str) -> None:
    art, eigentuemer, sichtbarkeit = STARTUMFANG[name]
    vertrag = _vertrag(register, name)
    assert (vertrag.kind, vertrag.owner, vertrag.visibility) == (art, eigentuemer, sichtbarkeit)


@pytest.mark.parametrize("name", _ALLE)
def test_keine_freitextfelder_bei_nichtoeffentlich_und_personenbezogen(register: Registry, name: str) -> None:
    vertrag = _vertrag(register, name)
    if vertrag.visibility & RESTRICTED_VISIBILITIES:
        assert free_text_paths(vertrag.schema, skip_content=vertrag.kind == COMMAND) == []
    assert content_paths(vertrag.schema) == INHALTSFELDER.get(name, [])


@pytest.mark.parametrize("name", [name for name in _ALLE if STARTUMFANG[name][0] == EVENT])
def test_ereignisse_enthalten_nur_kennungen_codes_und_feldnamen(register: Registry, name: str) -> None:
    """Auch öffentliche und interne Ereignisse tragen keine Inhalte (Nutzlast minimal)."""
    assert free_text_paths(_vertrag(register, name).schema) == []


@pytest.mark.parametrize("name", _ALLE)
def test_aufbau_der_nutzlast(register: Registry, name: str) -> None:
    schema = _vertrag(register, name).schema
    felder = schema["properties"]
    assert set(schema["required"]) <= set(felder), "Pflichtfelder müssen beschrieben sein"
    # Feldnamen erscheinen so auch in Fehlermeldungen (validation._SAFE_KEY).
    assert all(_FELDNAME.fullmatch(feld) for feld in felder)
    assert all(isinstance(definition.get("description"), str) for definition in felder.values())


@pytest.mark.parametrize("name", _ALLE)
def test_beispiele_sind_gueltig(register: Registry, name: str) -> None:
    vertrag = _vertrag(register, name)
    assert vertrag.examples
    for beispiel in vertrag.examples:
        if vertrag.kind == COMMAND:
            register.validate_command(name, vertrag.version, beispiel)
        else:
            register.validate_payload(name, vertrag.version, beispiel)


@pytest.mark.parametrize("name", [name for name in _ALLE if STARTUMFANG[name][0] == EVENT])
def test_beispiele_ergeben_gueltige_ereignisse_in_jeder_sichtbarkeit(register: Registry, name: str) -> None:
    vertrag = _vertrag(register, name)
    for sichtbarkeit in sorted(vertrag.visibility):
        for beispiel in vertrag.examples:
            register.validate_event(_ereignis(vertrag, sichtbarkeit, beispiel))


def _ereignis(vertrag: Contract, sichtbarkeit: str, nutzlast: dict[str, Any]) -> Envelope:
    return Envelope(
        type=vertrag.name,
        version=vertrag.version,
        aggregate_type="Paper",
        aggregate_id=uuid.uuid4(),
        tenant_ref=f"session:{uuid.uuid4()}",
        visibility=sichtbarkeit,
        payload=nutzlast,
    )
