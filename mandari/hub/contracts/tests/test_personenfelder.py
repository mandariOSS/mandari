# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Personenfelder (``"x-person": true``) für die DSGVO-Löschung im Journal (Issue #511).

Ein Feld, das eine Person nennt, ist im Vertrag gekennzeichnet; nach einem ``redact`` leert die Plattform die
Nutzlast personenbezogener Journaleinträge, die diese Person nennen (``apps.events.datenschutz``). Die Liste
steht hier fest: Ein neues personenbezogenes Ereignis muss sich entscheiden.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from hub.contracts import EVENT, get_registry
from hub.contracts.compatibility import breaking_changes
from hub.contracts.rules import document_problems, person_fields
from hub.contracts.tests.hilfen import befehl_schema, ereignis_schema

PERSON = "01234567-89ab-4cde-8f01-23456789abcd"

#: Die einzigen Personenfelder der ausgelieferten Verträge
PERSONENFELDER: dict[tuple[str, int], tuple[str, ...]] = {
    ("attendance.response_recorded", 1): ("person",),
    ("core.membership.changed", 1): ("user",),
    ("core.user.registered", 1): ("user",),
    ("session.allowance.approved", 1): ("person",),
}
#: Personenbezogene Ereignisse ohne Personenfeld, mit Grund
OHNE_PERSONENFELD: dict[str, str] = {
    "session.payment.exported": "nennt nur Abrechnungspositionen, deren Person steht in session.allowance.approved",
}


def _personenbezogen(**felder: Any) -> dict[str, Any]:
    eigenschaften = {"user": {"type": "string", "format": "uuid", "x-person": True}, **felder}
    return ereignis_schema(
        "core.user.registered",
        1,
        **{
            "x-owner": "apps.accounts",
            "x-visibility": "personenbezogen",
            "required": ["user"],
            "properties": eigenschaften,
            "examples": [{"user": PERSON}],
        },
    )


def test_personenfelder_der_ausgelieferten_vertraege_stehen_fest() -> None:
    assert get_registry().person_fields() == PERSONENFELDER


def test_jedes_personenbezogene_ereignis_hat_ein_personenfeld_oder_einen_grund() -> None:
    register = get_registry()
    personenbezogen = {v.name for v in register.contracts(EVENT) if "personenbezogen" in v.visibility}
    mit_feld = {name for name, _ in PERSONENFELDER}
    assert personenbezogen == mit_feld | set(OHNE_PERSONENFELD)
    assert not mit_feld & set(OHNE_PERSONENFELD)


def test_gueltiges_personenfeld() -> None:
    schema = _personenbezogen()
    assert document_problems("core.user.registered", 1, schema) == []
    assert person_fields(schema) == ("user",)


@pytest.mark.parametrize(
    ("schema", "meldung"),
    [
        (
            _personenbezogen(step={"type": "string", "enum": ["created"], "x-person": True}),
            "nur an Zeichenketten im Format uuid",
        ),
        (_personenbezogen(user={"type": "string", "format": "uuid", "x-person": "ja"}), "muss true sein"),
        (
            _personenbezogen(
                beteiligte={
                    "type": "array",
                    "items": {"type": "string", "format": "uuid", "x-person": True},
                    "maxItems": 10,
                }
            ),
            "nur an Feldern der obersten Ebene",
        ),
        (
            ereignis_schema(
                properties={
                    "paper": {"type": "string", "format": "uuid", "x-person": True},
                    "changed": {"type": "array", "items": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"}},
                }
            ),
            "nur in Ereignissen der Klasse personenbezogen",
        ),
    ],
)
def test_regeln_fuer_personenfelder(schema: dict[str, Any], meldung: str) -> None:
    name = "ris.paper.released" if schema["x-visibility"] != "personenbezogen" else "core.user.registered"
    assert any(meldung in problem for problem in document_problems(name, 1, schema))


def test_personenfeld_nur_in_ereignissen() -> None:
    schema = befehl_schema()
    feld = next(iter(schema["properties"]))
    schema["properties"][feld]["x-person"] = True
    assert any("nur in Ereignissen" in problem for problem in document_problems("submission.submit", 1, schema))


def test_kennzeichnung_ist_eine_ergaenzung() -> None:
    alt = _personenbezogen()
    alt["properties"]["user"].pop("x-person")
    assert breaking_changes(alt, copy.deepcopy(_personenbezogen())) == []
