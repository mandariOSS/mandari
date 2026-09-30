# SPDX-License-Identifier: AGPL-3.0-or-later
"""Muster ohne Freitext: verankert und ohne Leerraum (Issue #518)."""

from __future__ import annotations

import re
import sys

import pytest

from hub.contracts.patterns import WHITESPACE, excludes_whitespace


@pytest.mark.parametrize(
    "muster",
    [
        r"^[a-z_]+$",
        r"^[a-z][A-Za-z0-9_]{0,63}$",
        r"^[0-9a-f]{64}$",
        r"^SG-[0-9]{4}-[0-9]{4,6}$",
        r"^(session|org|source):[0-9a-f]{8}$",
        r"^(?:a|b)$",
        r"^a$|^b$",
        r"^\w+$",
        r"^[\w.-]+$",
        r"^\S+$",
        r"^\d{4}$",
        r"\A[a-z]+\Z",
        r"^(?=[a-z])[a-z0-9]+$",
        r"(?i)^[a-z]+$",
        r"^$",
    ],
)
def test_verankerte_muster_ohne_leerraum(muster: str) -> None:
    assert excludes_whitespace(muster)


@pytest.mark.parametrize(
    ("muster", "grund"),
    [
        (r"[a-z]", "nicht verankert"),
        (r"[a-z_]+", "nicht verankert"),
        (r"^[A-Z]{2}", "nur vorn verankert: „AB freier Text“ passt"),
        (r"[0-9]{3}$", "nur hinten verankert"),
        (r"^[a-z]+$|.*", "eine Alternative ohne Anker"),
        (r"^[a-z]+$|", "leere Alternative passt überall"),
        (r"^(?:[a-z]+|[0-9]+)", "Gruppe am Ende ohne Anker"),
        (r"(?m)^[a-z]+$", "Mehrzeilenmodus: ^ und $ an jeder Zeile"),
        (r"(?m:^[a-z]+)$", "Mehrzeilenmodus in einer Gruppe"),
        (r"^[a-z ]+$", "Leerzeichen in der Klasse"),
        (r"^[a-z]+ [a-z]+$", "Leerzeichen als Zeichen"),
        (r"^.*$", "Punkt"),
        (r"^.{1,40}$", "Punkt"),
        (r"^\s*[a-z]+$", "\\s"),
        (r"^[\s]$", "\\s in der Klasse"),
        (r"^\D+$", "\\D schließt Leerraum ein"),
        (r"^\W+$", "\\W schließt Leerraum ein"),
        (r"^[^a-z]+$", "verneinte Klasse"),
        (r"^[^,]+$", "verneinte Einzelklasse"),
        (r"^a\tb$", "Tabulator"),
        (r"^a\u00a0b$", "geschütztes Leerzeichen"),
        (r"^a\u3000b$", "ideografisches Leerzeichen"),
        (r"^[\x00-\x7f]+$", "Bereich mit Leerzeichen"),
        (r"^(?=(.*))\1$", "Rückverweis auf eine Vorausschau"),
        (r"^(a)?(?(1)b|c)$", "bedingte Gruppe"),
        ("^[", "kein gültiges Muster"),
        (None, "kein Muster"),
        (42, "kein Muster"),
    ],
)
def test_muster_mit_freitext(muster: object, grund: str) -> None:
    assert not excludes_whitespace(muster), grund


def test_liste_der_leerraumzeichen_ist_vollstaendig() -> None:
    erwartet = "".join(zeichen for zeichen in map(chr, range(sys.maxunicode + 1)) if zeichen.isspace())
    assert erwartet == WHITESPACE
    assert all(re.fullmatch(r"\s", zeichen) for zeichen in WHITESPACE)


@pytest.mark.parametrize(
    ("muster", "freitext"),
    [
        (r"^[A-Z]{2}", "AB Erika Mustermann wohnt in der Musterstraße 1"),
        (r"[0-9]{3}", "123 Erika Mustermann"),
        (r"^[a-z ]+$", "erika mustermann wohnt in der musterstrasse"),
    ],
)
def test_feste_probetexte_genuegen_nicht(muster: str, freitext: str) -> None:
    """Gegenprobe: Keine der früheren Proben passt auf diese Muster, und doch lassen sie Freitext zu."""
    assert not any(re.search(muster, probe) for probe in ("Erika Mustermann", "ein Satz mit Leerzeichen."))
    assert re.search(muster, freitext)
    assert not excludes_whitespace(muster)
