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
Feldname (``enum``/``const``, Format wie ``uuid`` oder ein verankertes Muster ohne Leerraum mit
Längengrenze, siehe ``patterns.py``), und Objekte und Listen lassen keine undeklarierten Werte zu.
Ein Muster zählt nur als Kennung, wenn es selbst die Länge begrenzt (Quantoren mit Obergrenze) oder
das Feld ``maxLength`` trägt, jeweils höchstens ``IDENTIFIER_MAX_LENGTH`` Zeichen. Auch die
Feldnamen eines Objekts sind kein Freitext: Frei wählbare Schlüssel (``additionalProperties`` als
Schema, ``patternProperties`` mit einem Muster, das kein solches Kennungsmuster ist) brauchen
``propertyNames`` mit ``enum``/``const``, Kennungsformat oder Kennungsmuster.

**Inhaltsfelder in Befehlen:** Ein Befehl bittet den Eigentümer, Daten zu speichern; manche davon
sind Inhalte (Antragstext, Grund einer Absage). Solche Felder tragen ``"x-content": true`` und sind
vom Freitextverbot ausgenommen. Ausgenommen ist nur die Zeichenkette selbst: Im Teilbaum eines
Inhaltsfelds gelten dieselben Regeln zur Offenheit wie außerhalb (kein Knoten ohne Typ, kein
``true``, Objekte mit ``additionalProperties: false``, Listen mit ``items``), jede freie
Zeichenkette braucht ``maxLength``, jede Liste ``maxItems``, und Verweise (``$ref``) sind dort nicht
zulässig, weil sich die Grenzen des Ziels an dieser Stelle nicht prüfen lassen. Ein Inhaltsfeld
nimmt also nie beliebiges JSON an. Ereignisse haben nie Inhaltsfelder: Sie landen im Journal, im
Änderungsfeed und bei Abonnenten; Inhalte holt der Empfänger berechtigt beim Eigentümer. Ein Befehl
geht dagegen nur an den Eigentümer, und der Befehlsweg gibt seinen Inhalt weder in Logs noch in
Fehlermeldungen oder Ereignisse weiter.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any, Final

from .envelope import RESTRICTED_VISIBILITIES, VISIBILITIES
from .formats import IDENTIFIER_FORMATS
from .naming import COMMAND, EVENT, INTERNAL_DOMAINS, KINDS, PLATFORM_PACKAGES, check_name, domain_of
from .patterns import excludes_whitespace, max_length
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

#: Größte Länge, bis zu der eine Zeichenkette mit Muster noch als Kennung gilt.
IDENTIFIER_MAX_LENGTH: Final = 255

_FREE_STRING: Final = "Zeichenkette ohne enum, Kennungsformat oder verankertes Muster ohne Leerraum mit Längengrenze"
_REFERENCES: Final = ("$ref", "$dynamicRef")

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
    """Verstöße gegen die Regeln für Inhaltsfelder: nur in Befehlen, nur an Feldern, geschlossen und begrenzt."""
    problems: list[str] = []
    for pointer, node in _content_nodes(schema, "#"):
        if kind != COMMAND:
            problems.append(f"{pointer}: {CONTENT_KEYWORD} gibt es nur in Befehlen, Ereignisse tragen keine Inhalte")
        elif pointer == "#":
            problems.append(f"#: {CONTENT_KEYWORD} gilt für einzelne Felder, nicht für den ganzen Befehl")
        elif node[CONTENT_KEYWORD] is not True:
            problems.append(f"{pointer}: {CONTENT_KEYWORD} muss true sein")
        else:
            problems += _content_field_problems(node, pointer)
    # Ein Inhaltsfeld in einem Inhaltsfeld würde seine Verstöße sonst doppelt melden.
    return list(dict.fromkeys(problems))


def _content_field_problems(node: object, pointer: str) -> Iterator[str]:
    """
    Verstöße im Teilbaum eines Inhaltsfelds.

    Vom Freitextverbot ausgenommen ist nur die Zeichenkette; sie braucht ``maxLength``. Alles andere
    gilt wie außerhalb: Ein Knoten ohne Typ, ``true``, ein offenes Objekt oder eine Liste ohne
    ``items`` ließe beliebiges JSON ohne Längengrenze zu.
    """
    if node is True:
        yield f"{pointer}: Inhaltsfeld offen (true lässt jeden Wert zu)"
        return
    if not isinstance(node, Mapping):
        return
    if any(key in node for key in _REFERENCES):
        yield f"{pointer}: Inhaltsfeld mit Verweis ($ref), die Grenzen des Ziels sind hier nicht prüfbar"
    structure = _structure_reason(node)
    if structure:
        yield f"{pointer}: Inhaltsfeld offen ({structure})"
    if _string_reason(node) and _limit(node.get("maxLength")) is None:
        yield f"{pointer}: Inhaltsfeld ohne maxLength"
    if "array" in _types(node) and _limit(node.get("maxItems")) is None:
        yield f"{pointer}: Inhaltsfeld ohne maxItems"
    for child_pointer, child in _children(node, pointer):
        yield from _content_field_problems(child, child_pointer)


def content_leaves(schema: Mapping[str, Any]) -> dict[str, int | None]:
    """
    Blätter aller Inhaltsfelder (JSON-Pointer) mit ihrer Längengrenze ``maxLength``.

    Blatt ist jeder Knoten ohne Teilschemas, also jedes Feld, das am Ende einen Wert trägt. Damit
    lässt sich der genaue Zuschnitt der Inhaltsfelder als Test festhalten: Ein neues Unterfeld oder
    eine höhere Grenze fällt auf.
    """
    leaves: dict[str, int | None] = {}
    for pointer, node in _content_nodes(schema, "#"):
        for leaf_pointer, leaf in _leaves(node, pointer):
            leaves[leaf_pointer] = _limit(leaf.get("maxLength"))
    return leaves


def _leaves(node: object, pointer: str) -> Iterator[tuple[str, Mapping[str, Any]]]:
    if not isinstance(node, Mapping):
        return
    children = list(_children(node, pointer))
    if not children:
        yield pointer, node
    for child_pointer, child in children:
        yield from _leaves(child, child_pointer)


def _limit(value: object) -> int | None:
    """Wert von ``maxLength``/``maxItems`` als Zahl; ``None``, wenn die Grenze fehlt."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _types(node: Mapping[str, Any]) -> set[str]:
    types = node.get("type")
    return {types} if isinstance(types, str) else set(types) if isinstance(types, list) else set()


def _node_reason(node: Mapping[str, Any]) -> str:
    """Grund, warum dieser Knoten Freitext zulässt; leer, wenn er es nicht tut."""
    return _string_reason(node) or _structure_reason(node)


def _string_reason(node: Mapping[str, Any]) -> str:
    """Grund, wenn der Knoten eine freie Zeichenkette zulässt (weder Kennung noch Code)."""
    if "const" in node or "enum" in node:
        return ""
    if "string" in _types(node) and not _string_restricted(node):
        return _FREE_STRING
    return ""


def _structure_reason(node: Mapping[str, Any]) -> str:
    """Grund, wenn der Knoten undeklarierte Werte zulässt: ohne Typ, offenes Objekt, Liste ohne ``items``."""
    if "const" in node or "enum" in node:
        return ""
    type_set = _types(node)
    if not type_set:
        # Ohne Typ muss der Knoten über einen Verweis oder eine Kombination beschränkt sein.
        if any(key in node for key in ("$ref", "allOf", "anyOf", "oneOf")):
            return ""
        return "ohne Typ ist jeder Wert erlaubt"
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
    return _pattern_restricted(node.get("pattern"), node.get("maxLength"))


def _pattern_restricted(pattern: object, declared_limit: object = None) -> bool:
    """
    Das Muster beschreibt eine Kennung (``patterns.py``): vorn und hinten verankert, ohne Leerraum,
    und höchstens ``IDENTIFIER_MAX_LENGTH`` Zeichen lang, begrenzt durch das Muster selbst oder durch
    ``maxLength`` am selben Knoten.
    """
    if not excludes_whitespace(pattern):
        return False
    limits = [limit for limit in (max_length(pattern), _limit(declared_limit)) if limit is not None]
    return bool(limits) and min(limits) <= IDENTIFIER_MAX_LENGTH


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
        return "Objekt mit frei wählbaren Feldnamen (Muster in patternProperties ist kein Kennungsmuster)"
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
