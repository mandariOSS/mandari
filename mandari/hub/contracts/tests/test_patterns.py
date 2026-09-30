# SPDX-License-Identifier: AGPL-3.0-or-later
"""Muster ohne Freitext: verankert, ohne Leerraum, ohne Python-Eigenheiten, mit Längengrenze (Issue #518)."""

from __future__ import annotations

import re
import sys

import pytest

from hub.contracts.patterns import WHITESPACE, excludes_whitespace, max_length


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
        r"^(?=[a-z])[a-z0-9]+$",
        r"^[a-z]+\b$",
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
        # Nur ^ und $ sind Anker nach ECMA-262; \A und \Z liest ein fremder Prüfer als Buchstaben.
        (r"\A[a-z]+\Z", "\\A und \\Z gibt es nur in Python"),
        (r"\A[a-z]+$", "\\A vorn"),
        (r"^[a-z]+\Z", "\\Z hinten"),
        (r"^\A[a-z]+\Z$", "\\A und \\Z zusätzlich zu ^ und $"),
        (r"^(?=\A)[a-z]+$", "\\A in einer Vorausschau"),
        (r"(?i)^[a-z]+$", "Schalter (?i) gibt es in ECMA-262 nicht"),
        (r"(?s)^[a-z]+$", "Schalter (?s)"),
        (r"(?a)^\w+$", "Schalter (?a)"),
        (r"^(?i:[a-z]+)$", "Schalter je Gruppe"),
        (r"^(?-i:[a-z]+)$", "abgeschalteter Schalter je Gruppe"),
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


@pytest.mark.parametrize(
    ("muster", "erwartet"),
    [
        (r"^[a-z][A-Za-z0-9_]{0,63}$", 64),
        (r"^[0-9a-f]{64}$", 64),
        (r"^SG-[0-9]{4}-[0-9]{4,6}$", 14),
        (r"^(?:a|bcd){2,3}$", 9),
        (r"^a$|^b{1,300}$", 300),
        (r"^$", 0),
        (r"^[a-z]+$", None),
        (r"^\S*$", None),
        (r"^[a-z]{2,}$", None),
        (r"^(?:[a-z]{1,3})+$", None),
        (r"\A[a-z]\Z", None),
        (r"(?i)^[a-z]{3}$", None),
        ("^[", None),
        (None, None),
    ],
)
def test_laengengrenze_eines_musters(muster: object, erwartet: int | None) -> None:
    assert max_length(muster) == erwartet


@pytest.mark.parametrize("wert", ["ErikaMustermann", "erika.mustermann@example.org"])
def test_grenze_der_regel_einzelne_woerter_bleiben_moeglich(wert: str) -> None:
    """
    „Ohne Leerraum“ schließt Sätze aus, nicht jedes Wort. Deshalb verlangt ``rules.py`` eine
    Längengrenze, und die ausgelieferten Muster stehen als Liste fest (``test_schemas.py``).
    """
    assert excludes_whitespace(r"^\S{1,64}$")
    assert re.search(r"^\S{1,64}$", wert)
    assert not re.search(r"^[a-z][A-Za-z0-9_]{0,63}$", wert)


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
