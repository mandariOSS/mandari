# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hausnummer vom Straßennamen trennen – korrekt und ohne quadratischen Regex-Aufwand (CodeQL ReDoS)."""

from __future__ import annotations

import time

import pytest

from insight_core.services.gazetteer import strip_house_number


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("Hauptstraße 12a", ("Hauptstraße", "12a")),
        ("Hauptstraße 12 a", ("Hauptstraße", "12a")),
        ("  Am Markt 3  ", ("Am Markt", "3")),
        ("Johann-Krane-Weg 1234", ("Johann-Krane-Weg", "1234")),
        ("Weg 12345", ("Weg 12345", None)),  # mehr als vier Ziffern ist keine Hausnummer
        ("B51", ("B51", None)),  # ohne Leerraum keine Hausnummer
        ("Schillerstraße", ("Schillerstraße", None)),
        ("", ("", None)),
    ],
)
def test_hausnummer_abtrennen(eingabe: str, erwartet: tuple[str, str | None]) -> None:
    assert strip_house_number(eingabe) == erwartet


def test_lange_leerzeichenfolgen_bleiben_schnell() -> None:
    boese = "a" + " " * 50_000 + "x"
    start = time.perf_counter()
    assert strip_house_number(boese) == (boese, None)
    assert time.perf_counter() - start < 0.5
