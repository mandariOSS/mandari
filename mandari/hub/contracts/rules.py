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
Feldname (``enum``/``const``, Format wie ``uuid`` oder ein verankertes Muster ohne Leerraum, siehe
``patterns.py``), und Objekte und Listen lassen keine undeklarierten Werte zu. Auch die Feldnamen
eines Objekts sind kein Freitext: Frei wählbare Schlüssel (``additionalProperties`` als Schema,
``patternProperties`` mit einem Muster, das Leerraum zulässt) brauchen ``propertyNames`` mit
``enum``/``const``, Kennungsformat oder einem verankerten Muster ohne Leerraum.

**Inhaltsfelder in Befehlen:** Ein Befehl bittet den Eigentümer, Daten zu speichern; manche davon
sind Inhalte (Antragstext, Grund einer Absage). Solche Felder tragen ``"x-content": true`` und sind
vom Freitextverbot ausgenommen, brauchen aber für jede Zeichenkette ``maxLength``. Ereignisse haben
nie Inhaltsfelder: Sie landen im Journal, im Änderungsfeed und bei Abonnenten; Inhalte holt der
Empfänger berechtigt beim Eigentümer. Ein Befehl geht dagegen nur an den Eigentümer, und der
Befehlsweg gibt seinen Inhalt weder in Logs noch in Fehlermeldungen oder Ereignisse weiter.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any, Final

from .envelope import RESTRICTED_VISIBILITIES, VISIBILITIES
from .formats import IDENTIFIER_FORMATS
from .naming import COMMAND, EVENT, INTERNAL_DOMAINS, KINDS, PLATFORM_PACKAGES, check_name, domain_of
from .patterns import excludes_whitespace
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

#: Kennzeichen eines Inhaltsfelds (nur in Befehlen, siehe oben).
CONTENT_KEYWORD: Final = "x-content"

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


def free_text_paths(schema: Mapping[str, Any], *, skip_content: bool = False) -> list[str]:
    """
    Stellen im Schema, an denen beliebiger Text stehen kann (JSON-Pointer mit Grund).

    Mit ``skip_content`` bleiben Inhaltsfelder (``"x-content": true``) samt allem darunter außen vor.
    """
    return list(_open_nodes(schema, "#", skip_content=skip_content))


def _open_nodes(node: object, pointer: str, *, skip_content: bool) -> Iterator[str]:
    if node is True:
        yield f"{pointer} (true lässt jeden Wert zu)"
        return
    if not isinstance(node, Mapping):
        return
    if skip_content and node.get(CONTENT_KEYWORD) is True:
        return
    reason = _node_reason(node)
    if reason:
        yield f"{pointer} ({reason})"
    for child_pointer, child in _children(node, pointer):
        yield from _open_nodes(child, child_pointer, skip_content=skip_content)


def content_paths(schema: Mapping[str, Any]) -> list[str]:
    """Stellen im Schema mit ``x-content`` (JSON-Pointer), in Dokumentreihenfolge."""
    return [pointer for pointer, _ in _content_nodes(schema, "#")]


def _content_nodes(node: object, pointer: str) -> Iterator[tuple[str, Mapping[str, Any]]]:
    if not isinstance(node, Mapping):
        return
    if CONTENT_KEYWORD in node:
        yield pointer, node
    for child_pointer, child in _children(node, pointer):
        yield from _content_nodes(child, child_pointer)


def content_problems(schema: Mapping[str, Any], kind: object) -> list[str]:
    """Verstöße gegen die Regeln für Inhaltsfelder: nur in Befehlen, nur an Feldern, mit Längengrenze."""
    problems: list[str] = []
    for pointer, node in _content_nodes(schema, "#"):
        if kind != COMMAND:
            problems.append(f"{pointer}: {CONTENT_KEYWORD} gibt es nur in Befehlen, Ereignisse tragen keine Inhalte")
        elif pointer == "#":
            problems.append(f"#: {CONTENT_KEYWORD} gilt für einzelne Felder, nicht für den ganzen Befehl")
        elif node[CONTENT_KEYWORD] is not True:
            problems.append(f"{pointer}: {CONTENT_KEYWORD} muss true sein")
        else:
            problems += [f"{path}: Inhaltsfeld ohne maxLength" for path in _unbounded_strings(node, pointer)]
    return problems


def _unbounded_strings(node: object, pointer: str) -> Iterator[str]:
    """Zeichenketten ohne ``maxLength`` (Kennungen und Codes ausgenommen) in einem Teilschema."""
    if not isinstance(node, Mapping):
        return
    if "string" in _types(node) and _node_reason(node) and not isinstance(node.get("maxLength"), int):
        yield pointer
    for child_pointer, child in _children(node, pointer):
        yield from _unbounded_strings(child, child_pointer)


def _types(node: Mapping[str, Any]) -> set[str]:
    types = node.get("type")
    return {types} if isinstance(types, str) else set(types) if isinstance(types, list) else set()


def _node_reason(node: Mapping[str, Any]) -> str:
    """Grund, warum dieser Knoten Freitext zulässt; leer, wenn er es nicht tut."""
    if "const" in node or "enum" in node:
        return ""
    type_set = _types(node)
    if not type_set:
        # Ohne Typ muss der Knoten über einen Verweis oder eine Kombination beschränkt sein.
        if any(key in node for key in ("$ref", "allOf", "anyOf", "oneOf")):
            return ""
        return "ohne Typ ist jeder Wert erlaubt"
    if "string" in type_set and not _string_restricted(node):
        return "Zeichenkette ohne enum, Kennungsformat oder verankertes Muster ohne Leerraum"
    if "object" in type_set:
        reason = _object_reason(node)
        if reason:
            return reason
    if "array" in type_set and "items" not in node:
        return "Liste ohne Schema für items"
    return ""


def _string_restricted(node: Mapping[str, Any]) -> bool:
    if node.get("format") in IDENTIFIER_FORMATS:
        return True
    return _pattern_restricted(node.get("pattern"))


def _pattern_restricted(pattern: object) -> bool:
    """Das Muster ist vorn und hinten verankert und lässt keinen Leerraum zu (``patterns.py``)."""
    return excludes_whitespace(pattern)


def _object_reason(node: Mapping[str, Any]) -> str:
    """Grund, warum ein Objekt undeklarierte Felder oder frei wählbare Feldnamen zulässt."""
    additional = node.get("additionalProperties")
    unevaluated = node.get("unevaluatedProperties")
    # Ein Teilschema für undeklarierte Felder prüft deren Werte, nicht deren Namen.
    if isinstance(additional, Mapping) or (additional is None and isinstance(unevaluated, Mapping)):
        if _names_restricted(node):
            return ""
        keyword = "additionalProperties" if isinstance(additional, Mapping) else "unevaluatedProperties"
        return f"Objekt mit frei wählbaren Feldnamen ({keyword} als Schema ohne propertyNames als Kennung)"
    if additional is not False and unevaluated is not False:
        return "Objekt ohne additionalProperties: false"
    patterns = node.get("patternProperties")
    if (
        isinstance(patterns, Mapping)
        and any(not _pattern_restricted(pattern) for pattern in patterns)
        and not _names_restricted(node)
    ):
        return "Objekt mit frei wählbaren Feldnamen (Muster in patternProperties nicht verankert oder mit Leerraum)"
    return ""


def _names_restricted(node: Mapping[str, Any]) -> bool:
    """``propertyNames`` beschränkt die Feldnamen auf Kennungen oder Codes."""
    names = node.get("propertyNames")
    if not isinstance(names, Mapping):
        return False
    return "const" in names or "enum" in names or _string_restricted(names)


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
    problems += content_problems(document, kind)
    restricted = sorted(visibility & RESTRICTED_VISIBILITIES)
    if restricted:
        open_paths = free_text_paths(document, skip_content=kind == COMMAND)
        problems += [f"Freitext bei Sichtbarkeit {', '.join(restricted)}: {path}" for path in open_paths]

    examples = document.get("examples")
    if not isinstance(examples, list) or not examples:
        problems.append("examples fehlt (mindestens ein Beispiel)")
    elif not metaschema:
        validator = validator_for(document)
        for index, example in enumerate(examples):
            problems += [f"examples[{index}] {problem}" for problem in instance_problems(validator, example)]
    return problems
