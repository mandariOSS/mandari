# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Regeln für ein einzelnes Schema unter ``schemas/<typ>/v<n>.json``
(``docs/adr/20260929-ereignisvertraege.md``).

Jedes Schema

- ist gültiges JSON Schema 2020-12 (``$schema``) mit ``$id`` ``urn:mandari:<art>:<typ>:v<n>``,
  ``title`` und ``description`` (für den erzeugten Katalog);
- beschreibt die Nutzlast als Objekt (``type: object``);
- nennt ``x-kind`` (``event`` oder ``command``), den Eigentümer ``x-owner`` und die zulässigen
  Sichtbarkeitsklassen ``x-visibility`` (eine Klasse oder eine Liste);
- bringt in ``examples`` mindestens ein Beispiel mit, das gegen das Schema gültig ist.

Interne Bereiche (``session``, ``work``, ``portal``) gehören ihrem Fachmodul und sind nie
``oeffentlich``; ``core`` gehört der Plattform. Schemas der Klassen ``nichtoeffentlich`` und
``personenbezogen`` enthalten keine Freitextfelder: Jede Zeichenkette ist Kennung, Code oder
Feldname (``enum``/``const``, Format wie ``uuid`` oder ein Muster ohne Leerzeichen), und Objekte und
Listen lassen keine undeklarierten Werte zu.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any, Final

from .envelope import RESTRICTED_VISIBILITIES, VISIBILITIES
from .naming import COMMAND, EVENT, INTERNAL_DOMAINS, KINDS, PLATFORM_PACKAGES, check_name, domain_of
from .validation import SCHEMA_DIALECT, instance_problems, schema_problems, validator_for

#: Pakete, die Eigentümer von Ereignissen oder Befehlen sein können (auch deren Unterpakete).
KNOWN_OWNERS: Final[tuple[str, ...]] = (
    "apps.session",
    "apps.work",
    "apps.portal",
    "apps.minutes",
    *PLATFORM_PACKAGES,
    "hub",
    "insight_core",
    "ingestor",
)

#: Formate, die als Kennung oder Zeitangabe gelten und damit kein Freitext sind.
IDENTIFIER_FORMATS: Final[frozenset[str]] = frozenset({"uuid", "date", "date-time", "time", "duration"})
#: Ein Muster, auf das einer dieser Texte passt, lässt Freitext zu.
_FREE_TEXT_PROBES: Final[tuple[str, ...]] = ("Erika Mustermann", "ein Satz mit Leerzeichen.")

_OWNER_RE = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*$")


def expected_id(kind: str, name: str, version: int) -> str:
    return f"urn:mandari:{kind}:{name}:v{version}"


def parse_visibility(value: object) -> tuple[frozenset[str], list[str]]:
    """``x-visibility`` als Menge; eine Klasse oder eine nicht leere Liste ohne Doppelungen."""
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values or not all(isinstance(item, str) for item in values):
        return frozenset(), [f"x-visibility fehlt oder ist ungültig (eine oder mehrere von: {', '.join(VISIBILITIES)})"]
    problems = [f"x-visibility: unbekannte Klasse „{item}“" for item in values if item not in VISIBILITIES]
    if len(set(values)) != len(values):
        problems.append("x-visibility nennt eine Klasse doppelt")
    return frozenset(item for item in values if item in VISIBILITIES), problems


def owner_problems(name: str, owner: object) -> list[str]:
    if not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner):
        return ["x-owner fehlt oder ist kein Paketpfad (z. B. apps.session)"]
    if not _within(owner, KNOWN_OWNERS):
        return [f"x-owner „{owner}“ ist kein bekanntes Modul (erlaubt: {', '.join(KNOWN_OWNERS)} und Unterpakete)"]
    domain = domain_of(name)
    internal_owner = INTERNAL_DOMAINS.get(domain)
    if internal_owner and not _within(owner, (internal_owner,)):
        return [f"Bereich „{domain}“ gehört {internal_owner}, nicht „{owner}“"]
    if domain == "core" and not _within(owner, PLATFORM_PACKAGES):
        return [f"Bereich „core“ gehört der Plattform ({', '.join(PLATFORM_PACKAGES)}), nicht „{owner}“"]
    return []


def _within(owner: str, packages: tuple[str, ...]) -> bool:
    return any(owner == package or owner.startswith(package + ".") for package in packages)


def free_text_paths(schema: Mapping[str, Any]) -> list[str]:
    """Stellen im Schema, an denen beliebiger Text stehen kann (JSON-Pointer mit Grund)."""
    return list(_open_nodes(schema, "#"))


def _open_nodes(node: object, pointer: str) -> Iterator[str]:
    if node is True:
        yield f"{pointer} (true lässt jeden Wert zu)"
        return
    if not isinstance(node, Mapping):
        return
    reason = _node_reason(node)
    if reason:
        yield f"{pointer} ({reason})"
    for child_pointer, child in _children(node, pointer):
        yield from _open_nodes(child, child_pointer)


def _node_reason(node: Mapping[str, Any]) -> str:
    """Grund, warum dieser Knoten Freitext zulässt; leer, wenn er es nicht tut."""
    if "const" in node or "enum" in node:
        return ""
    types = node.get("type")
    type_set = {types} if isinstance(types, str) else set(types) if isinstance(types, list) else set()
    if not type_set:
        # Ohne Typ muss der Knoten über einen Verweis oder eine Kombination beschränkt sein.
        if any(key in node for key in ("$ref", "allOf", "anyOf", "oneOf")):
            return ""
        return "ohne Typ ist jeder Wert erlaubt"
    if "string" in type_set and not _string_restricted(node):
        return "Zeichenkette ohne enum, Kennungsformat oder Muster ohne Leerzeichen"
    if "object" in type_set and not _object_closed(node):
        return "Objekt ohne additionalProperties: false"
    if "array" in type_set and "items" not in node:
        return "Liste ohne Schema für items"
    return ""


def _string_restricted(node: Mapping[str, Any]) -> bool:
    if node.get("format") in IDENTIFIER_FORMATS:
        return True
    pattern = node.get("pattern")
    if not isinstance(pattern, str):
        return False
    try:
        compiled = re.compile(pattern)
    except re.error:
        return False
    return not any(compiled.search(probe) for probe in _FREE_TEXT_PROBES)


def _object_closed(node: Mapping[str, Any]) -> bool:
    """Undeklarierte Felder sind verboten oder haben ein eigenes (geprüftes) Schema."""
    return any(
        node.get(key) is False or isinstance(node.get(key), Mapping)
        for key in ("additionalProperties", "unevaluatedProperties")
    )


_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "dependentSchemas")
_SCHEMA_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_SINGLE = ("items", "additionalProperties", "unevaluatedProperties", "contains", "if", "then", "else")


def _children(node: Mapping[str, Any], pointer: str) -> Iterator[tuple[str, object]]:
    for key in _SCHEMA_MAPS:
        mapping = node.get(key)
        if isinstance(mapping, Mapping):
            for name, child in mapping.items():
                yield f"{pointer}/{key}/{_escape(str(name))}", child
    for key in _SCHEMA_LISTS:
        items = node.get(key)
        if isinstance(items, list):
            for index, child in enumerate(items):
                yield f"{pointer}/{key}/{index}", child
    for key in _SCHEMA_SINGLE:
        child = node.get(key)
        if isinstance(child, Mapping) or child is True:
            yield f"{pointer}/{key}", child


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def document_problems(name: str, version: int, document: object) -> list[str]:
    """Alle Verstöße eines Schemas gegen die Vertragsregeln; leere Liste, wenn es gültig ist."""
    if not isinstance(document, Mapping):
        return ["Schema muss ein JSON-Objekt sein"]
    problems: list[str] = []
    if document.get("$schema") != SCHEMA_DIALECT:
        problems.append(f"$schema muss {SCHEMA_DIALECT} sein")
    metaschema = schema_problems(document)
    problems += metaschema

    kind = document.get("x-kind")
    if kind not in KINDS:
        problems.append(f"x-kind muss „{EVENT}“ oder „{COMMAND}“ sein")
    else:
        problems += check_name(name, str(kind))
        if document.get("$id") != expected_id(str(kind), name, version):
            problems.append(f"$id muss {expected_id(str(kind), name, version)} sein")
    for key in ("title", "description"):
        if not isinstance(document.get(key), str) or not str(document.get(key)).strip():
            problems.append(f"{key} fehlt")
    if document.get("type") != "object":
        problems.append("die Nutzlast muss ein Objekt sein (type: object)")

    problems += owner_problems(name, document.get("x-owner"))
    visibility, visibility_problems = parse_visibility(document.get("x-visibility"))
    problems += visibility_problems
    domain = domain_of(name)
    if domain in INTERNAL_DOMAINS and "oeffentlich" in visibility:
        problems.append(f"Bereich „{domain}“ ist intern und darf nicht oeffentlich sein")
    restricted = sorted(visibility & RESTRICTED_VISIBILITIES)
    if restricted:
        problems += [f"Freitext bei Sichtbarkeit {', '.join(restricted)}: {path}" for path in free_text_paths(document)]

    examples = document.get("examples")
    if not isinstance(examples, list) or not examples:
        problems.append("examples fehlt (mindestens ein Beispiel)")
    elif not metaschema:
        validator = validator_for(document)
        for index, example in enumerate(examples):
            problems += [f"examples[{index}] {problem}" for problem in instance_problems(validator, example)]
    return problems
