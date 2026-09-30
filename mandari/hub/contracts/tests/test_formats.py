# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kennungs- und Zeitformate (Issue #517): Das Freitextverbot lässt ``uuid``, ``date``, ``date-time``,
``time`` und ``duration`` als Kennung gelten. Das trägt nur, wenn diese Formate auch wirklich geprüft
werden – unabhängig davon, ob die optionalen Pakete von ``jsonschema`` installiert sind.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hub.contracts import ContractError, ContractViolationError, load_registry
from hub.contracts.formats import FORMAT_CHECKS, IDENTIFIER_FORMATS
from hub.contracts.tests.hilfen import ablegen, ereignis_schema, huelle
from hub.contracts.validation import format_checker, instance_problems, validator_for

FREITEXT = "Erika Mustermann, Musterweg 1"
#: Formate, die ``jsonschema`` nur mit optionalen Paketen prüft (rfc3339-validator, isoduration).
OPTIONAL_GEPRUEFT = ("date-time", "time", "duration")


@pytest.fixture
def ohne_optionale_pakete(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Umgebung wie in CI und Image: ``jsonschema`` kennt keine Prüfung für diese Formate."""
    from jsonschema import Draft202012Validator, FormatChecker

    for name in OPTIONAL_GEPRUEFT:
        monkeypatch.delitem(Draft202012Validator.FORMAT_CHECKER.checkers, name, raising=False)
        monkeypatch.delitem(FormatChecker.checkers, name, raising=False)
    format_checker.cache_clear()
    yield
    format_checker.cache_clear()


def _fehler(format_: str, wert: object) -> list[str]:
    return instance_problems(validator_for({"type": ["string", "integer"], "format": format_}), wert)


def test_kennungsformate_werden_alle_selbst_geprueft() -> None:
    assert sorted(IDENTIFIER_FORMATS) == sorted(FORMAT_CHECKS) == ["date", "date-time", "duration", "time", "uuid"]


@pytest.mark.usefixtures("ohne_optionale_pakete")
@pytest.mark.parametrize("format_", sorted(IDENTIFIER_FORMATS))
def test_freitext_besteht_kein_kennungsformat(format_: str) -> None:
    assert _fehler(format_, FREITEXT) == ["$: verletzt „format“"]


@pytest.mark.parametrize(
    ("format_", "wert"),
    [
        ("uuid", "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c"),
        ("uuid", "5B2D7C1E-8F3A-5E9B-A4C6-1D2E3F4A5B6C"),
        ("date", "2026-09-30"),
        ("date", "2028-02-29"),
        ("time", "10:15:00Z"),
        ("time", "10:15:00.123456+02:00"),
        ("time", "23:59:60-01:30"),
        ("date-time", "2026-09-30T10:15:00+02:00"),
        ("date-time", "2026-09-30t08:15:00.5z"),
        ("duration", "P1D"),
        ("duration", "P1Y2M3DT4H5M6S"),
        ("duration", "PT36H"),
        ("duration", "P2W"),
        ("duration", "PT1M30S"),
        ("date-time", 20260930),
    ],
)
def test_gueltige_werte(format_: str, wert: object) -> None:
    assert _fehler(format_, wert) == []


@pytest.mark.parametrize(
    ("format_", "wert"),
    [
        ("uuid", "5b2d7c1e8f3a5e9ba4c61d2e3f4a5b6c"),
        ("uuid", "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6"),
        ("date", "2026-02-30"),
        ("date", "30.09.2026"),
        ("date", "2026-09-30\n"),
        ("time", "10:15:00"),
        ("time", "24:00:00Z"),
        ("time", "10:61:00Z"),
        ("time", "10:15:00+24:00"),
        ("date-time", "2026-09-30T10:15:00"),
        ("date-time", "2026-09-30 10:15:00Z"),
        ("date-time", "2026-13-01T10:15:00Z"),
        ("date-time", "gestern"),
        ("duration", "P"),
        ("duration", "PT"),
        ("duration", "P1H"),
        ("duration", "P1W2D"),
        ("duration", "1 Tag"),
    ],
)
def test_ungueltige_werte(format_: str, wert: str) -> None:
    assert _fehler(format_, wert) == ["$: verletzt „format“"]


# --- Freitext in einem Zeitfeld bei personenbezogenen Daten ------------------------------------


def _mit_zeitfeld(format_: str, beispiel: object) -> dict[str, Any]:
    return ereignis_schema(
        "attendance.response_recorded",
        **{
            "x-owner": "apps.session",
            "x-visibility": "personenbezogen",
            "required": ["paper", "changed", "zeit"],
            "properties": {
                **ereignis_schema()["properties"],
                "zeit": {"type": "string", "format": format_},
            },
            "examples": [{"paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c", "changed": [], "zeit": beispiel}],
        },
    )


@pytest.mark.usefixtures("ohne_optionale_pakete")
@pytest.mark.parametrize("format_", OPTIONAL_GEPRUEFT)
def test_beispiel_mit_freitext_im_zeitfeld_wird_abgelehnt(tmp_path: Path, format_: str) -> None:
    ablegen(tmp_path, "attendance.response_recorded", 1, _mit_zeitfeld(format_, FREITEXT))
    with pytest.raises(ContractError) as info:
        load_registry(tmp_path)
    assert info.value.problems == ("attendance.response_recorded v1: examples[0] $.zeit: verletzt „format“",)


@pytest.mark.usefixtures("ohne_optionale_pakete")
def test_nutzlast_mit_freitext_im_zeitfeld_wird_abgelehnt(tmp_path: Path) -> None:
    ablegen(tmp_path, "attendance.response_recorded", 1, _mit_zeitfeld("date-time", "2026-09-30T10:15:00Z"))
    register = load_registry(tmp_path)
    nutzlast = {"paper": "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c", "changed": [], "zeit": FREITEXT}
    with pytest.raises(ContractViolationError) as info:
        register.validate_event(
            huelle(type="attendance.response_recorded", visibility="personenbezogen", payload=nutzlast)
        )
    assert info.value.problems == ("$.zeit: verletzt „format“",)
    assert FREITEXT not in str(info.value)
