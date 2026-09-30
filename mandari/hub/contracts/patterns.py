# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lässt ein Muster (``pattern``) nur Zeichenketten ohne Leerraum zu?

JSON Schema wendet ``pattern`` als Suche an: Das Muster muss nur irgendwo in der Zeichenkette passen
(``jsonschema`` prüft mit ``re.search``). ``[a-z]+`` lässt daher auch „ab Erika Mustermann“ zu,
ebenso ``^[A-Z]{2}`` (nur vorn verankert) oder ``[0-9]{3}$``. Ein Muster schließt Freitext nur aus,
wenn es

- vorn und hinten verankert ist (``^…$`` bzw. ``\\A…\\Z``, in jeder Alternative), ohne
  Mehrzeilenmodus (``(?m)``), in dem ``^`` und ``$`` an jeder Zeile passen, und
- an keiner Stelle ein Leerraumzeichen verbrauchen kann: kein Leerzeichen, kein ``.``, keine Klasse
  wie ``\\s``, ``\\D``, ``\\W`` oder ``[^…]``, die Leerraum einschließt.

Geprüft wird der Syntaxbaum von Pythons ``re``, mit dem auch ``jsonschema`` prüft; Probetexte
genügen dafür nicht (``^[a-z ]+$`` passt weder auf „Erika Mustermann“ noch auf „Ein Satz.“, lässt
aber jeden kleingeschriebenen Satz zu). Was sich nicht sicher beurteilen lässt (unbekannte Bausteine,
Rückverweise, bedingte Gruppen), gilt als Freitext.

Einzige Lücke: ``$`` passt in Python auch vor einem abschließenden Zeilenumbruch. Ein einzelnes
``\\n`` am Ende ist kein Freitext; ``\\Z`` wäre strenger, gehört aber nicht zu ECMA-262, der
Grammatik von JSON Schema.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable
from typing import Any, Final

# Der Parser von ``re`` ist ein internes Modul ohne Typangaben; seit Python 3.11 liegt er unter
# ``re._parser`` (vorher ``sre_parse``, seitdem abgekündigt). Zugriff nur hier und nur lesend.
_parser: Any = importlib.import_module("re._parser")

#: Alle Zeichen, auf die ``\\s`` passt und für die ``str.isspace()`` gilt (Test prüft den Gleichlauf).
WHITESPACE: Final = "".join(
    map(
        chr,
        (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x85, 0xA0, 0x1680)
        + tuple(range(0x2000, 0x200B))
        + (0x2028, 0x2029, 0x202F, 0x205F, 0x3000),
    )
)

_START_ANCHORS: Final = frozenset({"AT_BEGINNING", "AT_BEGINNING_STRING"})
_END_ANCHORS: Final = frozenset({"AT_END", "AT_END_STRING"})
_REPEATS: Final = frozenset({"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"})
#: Klassen wie ``\\d`` im Syntaxbaum → gleichwertiges Muster für die Probe je Leerraumzeichen.
_CATEGORIES: Final = {
    "CATEGORY_DIGIT": re.compile(r"\d"),
    "CATEGORY_NOT_DIGIT": re.compile(r"\D"),
    "CATEGORY_SPACE": re.compile(r"\s"),
    "CATEGORY_NOT_SPACE": re.compile(r"\S"),
    "CATEGORY_WORD": re.compile(r"\w"),
    "CATEGORY_NOT_WORD": re.compile(r"\W"),
}

_Items = list[tuple[Any, Any]]


def excludes_whitespace(pattern: object) -> bool:
    """``True``, wenn jede Zeichenkette, auf die ``pattern`` (als Suche) passt, frei von Leerraum ist."""
    if not isinstance(pattern, str):
        return False
    try:
        re.compile(pattern)
        parsed = _parser.parse(pattern)
        if parsed.state.flags & re.MULTILINE:
            return False
        items = _items(parsed)
        return _anchored(items, start=True) and _anchored(items, start=False) and not _may_consume_whitespace(items)
    except (re.error, RecursionError, AttributeError, TypeError, ValueError):
        # Unlesbar oder unbekannter Aufbau: vorsichtshalber Freitext.
        return False


def _items(subpattern: Iterable[Any]) -> _Items:
    return [(op, argument) for op, argument in subpattern]


def _name(constant: object) -> str:
    return str(getattr(constant, "name", ""))


def _anchored(items: _Items, *, start: bool) -> bool:
    """Das Muster beginnt (bzw. endet) in jeder Alternative mit einem Anker an Anfang (bzw. Ende)."""
    if not items:
        return False
    op, argument = items[0] if start else items[-1]
    name = _name(op)
    if name == "AT":
        return _name(argument) in (_START_ANCHORS if start else _END_ANCHORS)
    if name == "SUBPATTERN":
        return _anchored(_items(argument[-1]), start=start)
    if name == "ATOMIC_GROUP":
        return _anchored(_items(argument), start=start)
    if name == "BRANCH":
        return all(_anchored(_items(branch), start=start) for branch in argument[1])
    return False


def _may_consume_whitespace(items: _Items) -> bool:
    return any(_item_may_consume_whitespace(op, argument) for op, argument in items)


def _item_may_consume_whitespace(op: object, argument: Any) -> bool:
    name = _name(op)
    if name == "LITERAL":
        return chr(argument) in WHITESPACE
    if name in ("NOT_LITERAL", "ANY"):
        return True
    if name == "IN":
        return _class_may_match_whitespace(_items(argument))
    if name == "BRANCH":
        return any(_may_consume_whitespace(_items(branch)) for branch in argument[1])
    if name == "SUBPATTERN":
        _group, add_flags, _del_flags, subpattern = argument
        # (?m:…) macht ^ und $ darin zu Zeilenankern.
        return bool(add_flags & re.MULTILINE) or _may_consume_whitespace(_items(subpattern))
    if name == "ATOMIC_GROUP":
        return _may_consume_whitespace(_items(argument))
    if name in _REPEATS:
        _low, high, subpattern = argument
        return high != 0 and _may_consume_whitespace(_items(subpattern))
    # Anker und Vorausschau verbrauchen keine Zeichen. Alles andere gilt als möglicher Leerraum:
    # Rückverweise (GROUPREF) können Text wiederholen, den eine Vorausschau ungeprüft erfasst hat,
    # bedingte Gruppen und unbekannte Bausteine lassen sich nicht sicher beurteilen.
    return name not in ("AT", "ASSERT", "ASSERT_NOT")


def _class_may_match_whitespace(items: _Items) -> bool:
    negated = any(_name(op) == "NEGATE" for op, _ in items)
    members = [(op, argument) for op, argument in items if _name(op) != "NEGATE"]
    for char in WHITESPACE:
        hits = [_member_matches(op, argument, char) for op, argument in members]
        if None in hits:
            return True
        if any(hits) != negated:
            return True
    return False


def _member_matches(op: object, argument: Any, char: str) -> bool | None:
    """Passt ein Klassenbestandteil auf ``char``? ``None``, wenn der Bestandteil unbekannt ist."""
    name = _name(op)
    if name == "LITERAL":
        return bool(argument == ord(char))
    if name == "RANGE":
        low, high = argument
        return bool(low <= ord(char) <= high)
    if name == "CATEGORY":
        category = _CATEGORIES.get(_name(argument))
        return None if category is None else category.fullmatch(char) is not None
    return None
