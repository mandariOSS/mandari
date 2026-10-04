# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nur additive Änderungen an einer bestehenden Schemaversion (``docs/adr/20260929-ereignisvertraege.md``).

Ein Schema ``<typ>/v<n>.json`` ist ein Versprechen an Empfänger, die es heute lesen, und an das Journal,
dessen ältere Ereignisse es weiter erfüllen müssen. ``breaking_changes(alt, neu)`` vergleicht zwei Stände
derselben Version und nennt jede Änderung, die nicht rein ergänzend ist. Erlaubt sind:

- ein neues Feld in ``properties``, solange es nicht in ``required`` steht,
- ein neuer Wert in einer Codeliste (``enum``): Empfänger behandeln unbekannte Codes wie einen fehlenden
  Wert; ein Code fällt nie weg,
- eine neue Sichtbarkeitsklasse in ``x-visibility`` (die Vertragsregeln prüfen sie wie jede andere),
- ein neuer Eintrag in ``$defs`` oder ``patternProperties``,
- Erläuterungen: ``title``, ``description``, ``examples``, ``$comment``, ``deprecated`` und die
  Anmerkungen ``x-…`` außer ``x-kind`` und ``x-visibility``.

Alles andere ist brechend und braucht eine neue Version: ein Feld entfernen oder umbenennen, ein neues
Pflichtfeld, ein Pflichtfeld, das optional wird (Empfänger verlassen sich darauf), Typ, Format, Muster,
Grenzen, ``const``, ``additionalProperties`` oder Verweise ändern, eine Codeliste einführen oder streichen,
eine Sichtbarkeitsklasse entfernen, die Art (Ereignis/Befehl) wechseln. Auch eine Lockerung zählt: Ein
Empfänger, der heute eine Kennung erwartet, darf morgen keinen beliebigen Text bekommen.

``contract_changes(alt, neu)`` wendet den Vergleich auf zwei Ablagen an (Schlüssel ``<typ>/v<n>``): Eine
Version, die verschwindet, ist brechend, denn das Journal kann noch Ereignisse dieser Version enthalten.
Neue Typen und neue Versionen sind frei.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Final

#: Schlüsselwörter, die nur erläutern; ihre Änderung bricht nichts.
ANNOTATIONS: Final[frozenset[str]] = frozenset({"title", "description", "examples", "$comment", "deprecated"})
#: Anmerkungen ``x-…``, die zum Vertrag gehören und nicht frei geändert werden dürfen.
BINDING_EXTENSIONS: Final[frozenset[str]] = frozenset({"x-kind", "x-visibility"})
#: Schlüsselwörter, deren Wert ein Teilschema ist.
SUBSCHEMA_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"items", "additionalProperties", "propertyNames", "contains", "not", "if", "then", "else"}
)
#: Schlüsselwörter, deren Wert eine Liste von Teilschemas ist (gleiche Länge, paarweise verglichen).
SCHEMA_LIST_KEYWORDS: Final[frozenset[str]] = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
#: Schlüsselwörter, deren Wert ein Verzeichnis von Teilschemas ist (neue Einträge frei).
SCHEMA_MAP_KEYWORDS: Final[frozenset[str]] = frozenset({"$defs", "patternProperties"})

_FEHLT: Final = object()
#: Schlüsselwörter, deren Fehlen dasselbe bedeutet wie ein leerer Wert (keine Pflichtfelder, keine Felder).
_EMPTY: Final[dict[str, Any]] = {"required": [], "properties": {}, "$defs": {}, "patternProperties": {}}


def _is_annotation(key: str) -> bool:
    return key in ANNOTATIONS or (key.startswith("x-") and key not in BINDING_EXTENSIONS)


def _as_set(value: Any) -> frozenset[Any]:
    if isinstance(value, list):
        return frozenset(_hashable(v) for v in value)
    return frozenset({_hashable(value)})


def _hashable(value: Any) -> Any:
    if isinstance(value, dict | list):
        return repr(value)
    return value


def _shown(value: Any) -> str:
    return ", ".join(sorted(str(v) for v in value))


def breaking_changes(old: Any, new: Any, path: str = "$") -> list[str]:
    """Brechende Unterschiede zwischen zwei Ständen desselben Schemas; leer, wenn ``new`` nur ergänzt."""
    return list(_compare(old, new, path))


def _compare(old: Any, new: Any, path: str) -> Iterator[str]:
    if not isinstance(old, dict) or not isinstance(new, dict):
        if old != new:
            yield f"{path}: Teilschema geändert"
        return
    for key in sorted(set(old) | set(new)):
        if _is_annotation(key):
            continue
        empty = _EMPTY.get(key, _FEHLT)
        before, after = old.get(key, empty), new.get(key, empty)
        where = f"{path}.{key}"
        if before is _FEHLT:
            yield f"{where}: neu eingeführt (schränkt ein)"
        elif after is _FEHLT:
            yield f"{where}: entfernt (lockert oder ändert den Vertrag)"
        elif key == "properties":
            yield from _compare_properties(before, after, path)
        elif key == "required":
            yield from _compare_required(before, after, path)
        elif key == "enum":
            removed = _as_set(before) - _as_set(after)
            if removed:
                yield f"{where}: Werte entfernt ({_shown(removed)})"
        elif key == "x-visibility":
            removed = _as_set(before) - _as_set(after)
            if removed:
                yield f"{where}: Sichtbarkeit entfernt ({_shown(removed)})"
        elif key == "type":
            if _as_set(before) != _as_set(after):
                yield f"{where}: Typ geändert"
        elif key in SUBSCHEMA_KEYWORDS:
            yield from _compare(before, after, where)
        elif key in SCHEMA_LIST_KEYWORDS:
            yield from _compare_list(before, after, where)
        elif key in SCHEMA_MAP_KEYWORDS:
            yield from _compare_map(before, after, where)
        elif before != after:
            yield f"{where}: geändert"


def _compare_properties(old: Any, new: Any, path: str) -> Iterator[str]:
    if not isinstance(old, dict) or not isinstance(new, dict):
        if old != new:
            yield f"{path}.properties: geändert"
        return
    for name in sorted(set(old) - set(new)):
        yield f"{path}.{name}: Feld entfernt"
    for name in sorted(set(old) & set(new)):
        yield from _compare(old[name], new[name], f"{path}.{name}")


def _compare_required(old: Any, new: Any, path: str) -> Iterator[str]:
    added = _as_set(new) - _as_set(old)
    dropped = _as_set(old) - _as_set(new)
    if added:
        yield f"{path}: neue Pflichtfelder ({_shown(added)})"
    if dropped:
        yield f"{path}: nicht mehr Pflicht ({_shown(dropped)})"


def _compare_list(old: Any, new: Any, path: str) -> Iterator[str]:
    if not isinstance(old, list) or not isinstance(new, list) or len(old) != len(new):
        yield f"{path}: Zahl der Teilschemas geändert"
        return
    for index, (before, after) in enumerate(zip(old, new, strict=True)):
        yield from _compare(before, after, f"{path}[{index}]")


def _compare_map(old: Any, new: Any, path: str) -> Iterator[str]:
    if not isinstance(old, dict) or not isinstance(new, dict):
        if old != new:
            yield f"{path}: geändert"
        return
    for name in sorted(set(old) - set(new)):
        yield f"{path}.{name}: entfernt"
    for name in sorted(set(old) & set(new)):
        yield from _compare(old[name], new[name], f"{path}.{name}")


def contract_changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """
    Brechende Änderungen zwischen zwei Ablagen ``{"<typ>/v<n>": schema}``.

    Eine entfernte Version ist brechend; Unterschiede bestehender Versionen prüft ``breaking_changes``.
    Neue Typen und Versionen sind frei. Jede Meldung beginnt mit ``<typ>/v<n>``.
    """
    problems: list[str] = []
    for key in sorted(old):
        if key not in new:
            problems.append(f"{key}: Version entfernt (das Journal kann noch Ereignisse dieser Version enthalten)")
            continue
        problems += [f"{key}: {problem}" for problem in breaking_changes(old[key], new[key])]
    return problems
