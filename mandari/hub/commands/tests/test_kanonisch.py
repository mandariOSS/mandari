# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kanonisches JSON nach RFC 8785, Inhalts-Hash und Problem nach RFC 9457 (Issue #539)."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import pytest

from hub.commands import Problem, canonical_json, content_hash
from hub.commands.problems import json_pointer, validation_problem


@pytest.mark.parametrize(
    ("zahl", "text"),
    [
        # Beispiele aus RFC 8785, Anhang B, und ECMAScript Number.prototype.toString
        (0.0, "0"),
        (-0.0, "0"),
        (1.0, "1"),
        (-12.5, "-12.5"),
        (0.002, "0.002"),
        (0.000001, "0.000001"),
        (1e-7, "1e-7"),
        (1.5e-7, "1.5e-7"),
        (123456789012345680000.0, "123456789012345680000"),
        (1e21, "1e+21"),
        (1e23, "1e+23"),
        (333333333.3333333, "333333333.3333333"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (9007199254740992.0, "9007199254740992"),
    ],
)
def test_zahlen_wie_ecmascript(zahl: float, text: str) -> None:
    assert canonical_json(zahl) == text.encode()


def test_objekte_sortiert_ohne_leerzeichen() -> None:
    wert = {"b": [1, True, None, "x"], "a": {"z": 1, "y": False}}
    assert canonical_json(wert) == b'{"a":{"y":false,"z":1},"b":[1,true,null,"x"]}'


def test_schluessel_nach_utf16_codeeinheiten() -> None:
    # RFC 8785, Abschnitt 3.2.3: U+1F600 (Surrogatpaar D83D DE00) steht vor U+FB33.
    wert = {chr(0xFB33): 1, chr(0x1F600): 2, chr(0x20AC): 3, "\r": 4, "1": 5}
    erwartet = '{"\\r":4,"1":5,"' + chr(0x20AC) + '":3,"' + chr(0x1F600) + '":2,"' + chr(0xFB33) + '":1}'
    assert canonical_json(wert) == erwartet.encode("utf-8")


def test_zeichenketten_nur_mit_noetigen_escapes() -> None:
    text = 'ä "zitat" \\ ' + chr(0x2028) + "\n" + chr(0x1F) + chr(0x7F)
    erwartet = '"ä \\"zitat\\" \\\\ ' + chr(0x2028) + "\\n\\u001f" + chr(0x7F) + '"'
    assert canonical_json(text) == erwartet.encode("utf-8")


@pytest.mark.parametrize("wert", [math.nan, math.inf, {1: "zahl als schlüssel"}, {"menge": {1, 2}}, b"bytes"])
def test_kein_json(wert: Any) -> None:
    with pytest.raises(ValueError):
        canonical_json(wert)


@pytest.mark.parametrize(("zahl", "text"), [(2**53 - 1, "9007199254740991"), (-(2**53) + 1, "-9007199254740991")])
def test_ganzzahlen_bis_zur_sicheren_grenze(zahl: int, text: str) -> None:
    assert canonical_json(zahl) == text.encode()


@pytest.mark.parametrize("zahl", [2**53, -(2**53), 10**21, -(10**30)])
def test_ganzzahlen_ausserhalb_des_sicheren_bereichs_werden_abgelehnt(zahl: int) -> None:
    """RFC 8785 rechnet mit Doubles: ``10**21`` hieße dort ``1e+21``, hier stünde die Zahl ausgeschrieben."""
    with pytest.raises(ValueError):
        canonical_json({"anzahl": zahl})


@pytest.mark.parametrize("wert", ["a\ud800b", {"k": ["a\udfffb"]}, {"a\ud800": 1}])
def test_einzelnes_surrogat_wird_abgelehnt_ohne_den_wert_zu_nennen(wert: Any) -> None:
    geheim = "Erika Mustermann"
    with pytest.raises(ValueError) as info:
        canonical_json({"name": geheim, "wert": wert})
    # UnicodeEncodeError trüge die ganze Zeichenkette mit sich (exc.object).
    assert not isinstance(info.value, UnicodeEncodeError)
    assert geheim not in repr(info.value.args)
    assert info.value.__cause__ is None


def test_inhalts_hash_ist_sha256_ueber_das_kanonische_json() -> None:
    wert = {"title": "Bänke", "document": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c"}
    assert content_hash(wert) == hashlib.sha256(canonical_json(wert)).hexdigest()
    assert content_hash(wert) == content_hash(dict(reversed(list(wert.items()))))


@pytest.mark.parametrize(
    ("pfad", "zeiger"),
    [("$", ""), ("$.paper", "/paper"), ("$.changed[0]", "/changed/0"), ("$.a.b[2].c", "/a/b/2/c"), ("kein", "")],
)
def test_json_pointer(pfad: str, zeiger: str) -> None:
    assert json_pointer(pfad) == zeiger


def test_problem_nach_rfc_9457() -> None:
    problem = validation_problem(["$.document: verletzt „format“", "$: Pflichtfeld fehlt (title)"])
    daten = problem.to_dict(instance="/befehle/submission.submit/v1")
    assert daten == {
        "type": "https://docs.mandari.de/api/probleme/validierung",
        "title": "Validierung fehlgeschlagen",
        "status": 422,
        "detail": "Der Befehl entspricht nicht seinem Vertrag.",
        "instance": "/befehle/submission.submit/v1",
        "errors": [
            {"pointer": "/document", "detail": "verletzt „format“"},
            {"pointer": "", "detail": "Pflichtfeld fehlt (title)"},
        ],
    }
    zurueck = Problem.from_dict(daten, status=422)
    assert (zurueck.status, zurueck.kind, zurueck.errors) == (422, "validierung", problem.errors)
    assert not zurueck.retryable


@pytest.mark.parametrize("angabe", ["kaputt", "", None, 200, 4.5, ["x"]])
def test_status_kommt_aus_der_antwort_nicht_aus_dem_inhalt(angabe: object) -> None:
    problem = Problem.from_dict({"type": "about:blank", "status": angabe}, status=409)
    assert problem.status == 409


def test_fremdes_problem_bleibt_lesbar() -> None:
    problem = Problem.from_dict({"type": "about:blank", "title": "Oops"}, status=503)
    assert (problem.status, problem.kind, problem.detail, problem.retryable) == (
        503,
        "unbekannt",
        "Nicht erreichbar",
        True,
    )
