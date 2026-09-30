# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ereignishülle (Issue #517): Schema, Python-Form und Gleichlauf mit dem Journal."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from apps.events.models import Event, Operation, Visibility
from hub.contracts import (
    ENVELOPE_VERSION,
    OPERATIONS,
    VISIBILITIES,
    ContractViolationError,
    Envelope,
    envelope_schema,
    validate_envelope,
)
from hub.contracts.naming import NAME_PATTERN
from hub.contracts.tests.hilfen import PAPER_ID, TENANT_REF, huelle
from hub.contracts.validation import instance_problems, schema_problems, validator_for


def _envelope(**abweichend: Any) -> Envelope:
    werte: dict[str, Any] = {
        "type": "ris.paper.released",
        "version": 1,
        "aggregate_type": "Paper",
        "aggregate_id": uuid.UUID(PAPER_ID),
        "tenant_ref": TENANT_REF,
        "visibility": "oeffentlich",
        "payload": {"paper": PAPER_ID, "changed": ["status"]},
    }
    werte.update(abweichend)
    return Envelope(**werte)


def test_schema_der_huelle_ist_gueltig_und_beispiele_passen() -> None:
    schema = envelope_schema(ENVELOPE_VERSION)
    assert schema_problems(schema) == []
    assert schema["$id"] == "urn:mandari:envelope:v1"
    validator = validator_for(schema)
    for beispiel in schema["examples"]:
        assert instance_problems(validator, beispiel) == []


def test_huelle_folgt_den_namensregeln() -> None:
    assert envelope_schema()["properties"]["type"]["pattern"] == NAME_PATTERN


def test_klassen_und_operationen_wie_im_journal() -> None:
    schema = envelope_schema()
    assert tuple(schema["properties"]["visibility"]["enum"]) == VISIBILITIES
    assert tuple(schema["properties"]["operation"]["enum"]) == OPERATIONS
    assert set(VISIBILITIES) == set(Visibility.values)
    assert set(OPERATIONS) == set(Operation.values)


def test_felder_der_huelle_sind_spalten_des_journals() -> None:
    spalten = {feld.name for feld in Event._meta.get_fields()}
    felder = set(envelope_schema()["properties"])
    assert felder <= spalten
    # Diese Spalten vergeben Datenbank bzw. Sequenzierer, sie gehören nicht zur Hülle.
    assert spalten - felder == {"id", "recorded_at", "xid", "seq"}


def test_neue_huelle_bekommt_kennungen_und_zeitpunkt() -> None:
    vorher = datetime.now(UTC)
    ereignis = _envelope()
    assert ereignis.event_id != ereignis.correlation_id
    assert ereignis.operation == "upsert"
    assert ereignis.occurred_at >= vorher
    assert ereignis.occurred_at.tzinfo is not None
    validate_envelope(ereignis.to_dict())


def test_hin_und_zurueck() -> None:
    ereignis = _envelope(
        body_id=uuid.uuid4(),
        causation_id=uuid.uuid4(),
        actor_ref=f"user:{uuid.uuid4()}",
        operation="redact",
        occurred_at=datetime(2026, 9, 30, 10, 15, tzinfo=timezone(timedelta(hours=2))),
    )
    assert Envelope.from_dict(ereignis.to_dict()) == ereignis


def test_beispiel_aus_dem_schema_laesst_sich_lesen() -> None:
    beispiel = envelope_schema()["examples"][0]
    assert Envelope.from_dict(beispiel).to_dict() == beispiel


def test_huelle_ohne_zeitzone_wird_abgelehnt() -> None:
    with pytest.raises(ValueError, match="Zeitzone"):
        _envelope(occurred_at=datetime(2026, 9, 30, 10, 15))


def test_nutzlast_ist_von_aussen_nicht_veraenderbar() -> None:
    nutzlast: dict[str, Any] = {"paper": PAPER_ID, "changed": ["status"]}
    ereignis = _envelope(payload=nutzlast)
    nutzlast["changed"].append("title")
    ereignis.to_dict()["payload"]["changed"].append("name")
    assert ereignis.payload == {"paper": PAPER_ID, "changed": ["status"]}


@pytest.mark.parametrize(
    ("abweichung", "stelle"),
    [
        ({"visibility": "geheim"}, "$.visibility"),
        ({"operation": "update"}, "$.operation"),
        ({"type": "Ris.Paper"}, "$.type"),
        ({"version": 0}, "$.version"),
        ({"aggregate_type": "paper"}, "$.aggregate_type"),
        ({"aggregate_id": "123"}, "$.aggregate_id"),
        ({"aggregate_id": PAPER_ID.upper()}, "$.aggregate_id"),
        ({"tenant_ref": "organization:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"}, "$.tenant_ref"),
        ({"actor_ref": "user:erika@example.org"}, "$.actor_ref"),
        ({"actor_ref": "Erika Mustermann"}, "$.actor_ref"),
        ({"occurred_at": "2026-09-30T10:15:00"}, "$.occurred_at"),
        ({"occurred_at": "gestern"}, "$.occurred_at"),
        ({"correlation_id": None}, "$.correlation_id"),
        ({"payload": []}, "$.payload"),
        ({"visiblity": "oeffentlich"}, "$: nicht vorgesehene Felder (visiblity)"),
    ],
)
def test_ungueltige_huelle(abweichung: dict[str, Any], stelle: str) -> None:
    with pytest.raises(ContractViolationError) as info:
        validate_envelope(huelle(**abweichung))
    assert info.value.subject == "Ereignishülle"
    assert any(problem.startswith(stelle) for problem in info.value.problems), info.value.problems


@pytest.mark.parametrize("feld", ["event_id", "type", "version", "visibility", "tenant_ref", "payload"])
def test_pflichtfelder_der_huelle(feld: str) -> None:
    daten = huelle()
    del daten[feld]
    with pytest.raises(ContractViolationError) as info:
        validate_envelope(daten)
    assert info.value.problems == (f"$: Pflichtfeld fehlt ({feld})",)


def test_meldungen_nennen_keine_werte() -> None:
    geheim = "Erika Mustermann"
    with pytest.raises(ContractViolationError) as info:
        validate_envelope(huelle(actor_ref=geheim, aggregate_type=geheim))
    assert geheim not in str(info.value)


def test_system_als_ausloeser() -> None:
    validate_envelope(huelle(actor_ref="system:events_worker"))
    validate_envelope(huelle(actor_ref=None, body_id=None, causation_id=None))
