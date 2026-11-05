# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die kommParl-Spezifikation im Code: übernommene Schemas, Kennungen und ihre Umwandlung für die Beschreibung.

**Herkunft:** Die Schemas liegen unverändert unter ``<FASSUNG>/`` (``schema/``, ``profiles/``), daneben
``HERKUNFT.json`` mit Repository, Commit, Datum, Lizenz (CC BY-SA 4.0) und Prüfsummen. Erneuert werden sie nur mit
``scripts/kommparl_schemas.py --commit <sha>``; ein Test prüft, dass sie unverändert sind.

**Objekttypen** (``schema/*.json``) sind im Format der OParl-Spezifikation geschrieben: JSON Schema mit eigenen
Schlüsselwörtern (``schema`` für eingebettete Objekte, ``references``, ``backreference``, ``cardinality``) und
eigenen Formaten (``url``, ``date``, ``date-time``). ``type_schema`` wandelt einen Objekttyp in JSON Schema 2020-12
für OpenAPI 3.1 um, nach denselben Regeln wie die Prüfung der Spezifikation (``scripts/validate.py`` in kommParl):

- ``url`` wird ``format: uri``, ``date`` und ``date-time`` bleiben (Kapitel „JSON-Ausgabe“),
- ein eingebettetes Objekt (``"type": "object", "schema": "File.json"``) wird ein Verweis auf dessen Schema,
- ein ``pattern``, das genau eine Zeichenkette zulässt (``type``), wird ``const``,
- Zeichenketten ohne Format sind nicht leer (``minLength: 1``; Kapitel „JSON-Ausgabe“: keine leeren Strings),
- Eigenschaften mit Herstellerpräfix sind erlaubt, alle anderen nur, wenn das Schema sie nennt – auch die mit
  dem Präfix ``kommparl:`` (``VENDOR_PROPERTY``).

Die Schlüsselwörter ``references``, ``backreference`` und ``cardinality`` beschreiben Beziehungen und entfallen;
eine externe Liste (``"references": "externalList"``) ist in der Ausgabe eine URL.

**Profile** (``profiles/<Profil>/*.json``) sind reines JSON Schema 2020-12. ``profile_schema`` liefert eines
unverändert (für die Prüfung der Ausgaben), ``profile_component`` als Komponente der Beschreibung: ohne ``$schema``
und ``$id`` (die Kennung steht in ``x-kommparl-schema``), Verweise in ``$defs`` aufgelöst.
"""

from __future__ import annotations

import copy
import json
import re
from functools import cache
from pathlib import Path
from typing import Any, Final

#: Fassung von kommParl, deren Schemas übernommen sind (Entwurf 0.1, kommParl Kapitel 5)
FASSUNG: Final = "0.1"

#: Kennung der Fassung (Namensraum aller Kennungen dieser Fassung)
VERSION: Final = f"https://schema.kommparl.de/{FASSUNG}/"
#: Kennungen der Profile
PROFILE_CHANGES: Final = f"{VERSION}profile/changes"
PROFILE_SNAPSHOT: Final = f"{VERSION}profile/snapshot"
#: Typ-URLs der Kopf- und Schlusszeile eines Snapshots
SNAPSHOT_HEADER: Final = f"{VERSION}SnapshotHeader"
SNAPSHOT_END: Final = f"{VERSION}SnapshotEnd"
#: Kennung der OParl-Version, die ein kommParl-Server weiter nennt (Kapitel 5, Kompatibilitätsregel 2)
OPARL_VERSION: Final = "https://schema.oparl.org/1.1/"
#: Typ des Fehlerobjekts nach OParl 1.1, Kapitel „Ausnahmebehandlung“
OPARL_ERROR: Final = "https://schema.oparl.org/1.1/Error"

#: Profile, deren Anforderungen beide OParl-Ausgaben vollständig erfüllen, wenn die Installation Änderungsfeed und
#: Snapshot anbietet (``OPARL_CHANGES_ENABLED``). Nur sie nennt ``kommparl:conformsTo`` (Kapitel 5: nur vollständig
#: erfüllte Kennungen). Heute keines: Ändert sich ein eingebettetes Objekt (ein Tagesordnungspunkt in einer Sitzung,
#: eine Datei an einer Vorlage), verlangt das Profil Änderungsfeed auch einen Eintrag für das einbettende Objekt
#: (Abschnitt „Vollständigkeit und Reihenfolge“). Der Feed nennt nur das Objekt, zu dem es ein Ereignis gibt. Das
#: Profil Snapshot setzt das Profil Änderungsfeed voraus.
PROFILES_FULFILLED: Final[tuple[str, ...]] = ()

#: Eigenschaften mit Herstellerpräfix, z. B. ``mandari:slug``. ``kommparl:`` ist kein Herstellerpräfix: Solche
#: Eigenschaften legt die Spezifikation fest, und sie sind nur erlaubt, wenn das Schema sie nennt.
VENDOR_PROPERTY: Final = r"^(?!kommparl:)[A-Za-z][A-Za-z0-9_.-]*:.+$"

#: Formate der Spezifikation (Kapitel „JSON-Ausgabe“) -> Formate von JSON Schema
FORMATS: Final[dict[str, str]] = {"url": "uri", "date": "date", "date-time": "date-time"}

ROOT: Final = Path(__file__).resolve().parent / FASSUNG
PROVENANCE: Final = "HERKUNFT.json"

_REF: Final = "#/components/schemas/"
_SPECIAL: Final = frozenset(".^$*+?()[]{}|\\")


# =============================================================================
# Dateien
# =============================================================================


def _load(relative: str) -> dict[str, Any]:
    data = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"kommParl {FASSUNG}: {relative} ist kein JSON-Objekt")
    return data


@cache
def provenance() -> dict[str, Any]:
    """Herkunft der übernommenen Schemas: Repository, Commit, Datum des Commits, Lizenz, Prüfsummen."""
    return _load(PROVENANCE)


@cache
def _object_schemas() -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "schema").glob("*.json")):
        schema = _load(f"schema/{path.name}")
        schemas[str(schema["title"])] = schema
    return schemas


def object_types() -> list[str]:
    """Namen der Objekttypen (``System``, ``Body`` …) in der Reihenfolge der Dateien."""
    return list(_object_schemas())


def object_schema(typ: str) -> dict[str, Any]:
    """Schema eines Objekttyps im Format der OParl-Spezifikation, unverändert."""
    return copy.deepcopy(_object_schemas()[typ])


@cache
def _profile_schemas() -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (path.parent.name, path.stem): _load(path.relative_to(ROOT).as_posix())
        for path in sorted((ROOT / "profiles").glob("*/*.json"))
    }


def profile_schemas() -> list[tuple[str, str]]:
    """Alle Schemas der Profile als ``(Profil, Name)``, z. B. ``("changes", "ChangeList")``."""
    return list(_profile_schemas())


def profile_schema(profile: str, name: str) -> dict[str, Any]:
    """Schema eines Profils, unverändert (JSON Schema 2020-12 mit ``$id``)."""
    return copy.deepcopy(_profile_schemas()[(profile, name)])


def conforms_to(*, feed_offered: bool) -> list[str]:
    """
    Kennungen für ``kommparl:conformsTo`` (Einstieg ``/api/`` und ``System`` beider Ausgaben): die Fassung und die
    Profile aus ``PROFILES_FULFILLED`` – diese nur, wenn die Installation Änderungsfeed und Snapshot anbietet.

    Die Fassung setzt unter anderem voraus, dass jeder Zeitpunkt die Form ``yyyy-mm-ddThh:mm:ss±hh:mm`` hat (Kapitel
    „JSON-Ausgabe“, muss); dafür sorgt ``hub.ris.canonical.iso``. Die Konformitätsprüfung der Tests prüft die Formate
    mit den Mustern der Spezifikation, ohne Ausnahme.
    """
    return [VERSION, *(PROFILES_FULFILLED if feed_offered else ())]


def attribution() -> str:
    """Namensnennung für Beschreibung und Dokumentation (CC BY-SA 4.0)."""
    origin = provenance()
    return (
        f"Schemas der OParl- und kommParl-Typen aus kommParl {FASSUNG} ({origin['repository']}, Commit "
        f"{str(origin['commit'])[:12]} vom {str(origin['commitDate'])[:10]}), einer Bearbeitung der "
        "OParl-Spezifikation 1.1 (https://github.com/OParl/spec), Lizenz CC BY-SA 4.0 "
        "(https://creativecommons.org/licenses/by-sa/4.0/). Für die Beschreibung in JSON Schema 2020-12 umgewandelt."
    )


# =============================================================================
# Objekttypen -> JSON Schema 2020-12
# =============================================================================


def component_name(typ: str) -> str:
    """Name des Schemas eines Objekttyps in der Beschreibung, z. B. ``OParlBody``."""
    return f"OParl{typ}"


def _literal(pattern: str) -> str | None:
    """Die eine Zeichenkette, die ``pattern`` zulässt (``^…$`` ohne Sonderzeichen), sonst ``None``."""
    if not (pattern.startswith("^") and pattern.endswith("$")):
        return None
    inner, text, index = pattern[1:-1], [], 0
    while index < len(inner):
        char = inner[index]
        if char == "\\":
            if index + 1 >= len(inner) or inner[index + 1].isalnum():
                return None  # Zeichenklasse wie \d oder ein abgebrochenes Escape
            text.append(inner[index + 1])
            index += 2
            continue
        if char in _SPECIAL:
            return None
        text.append(char)
        index += 1
    return "".join(text)


def _pattern(pattern: str) -> str:
    """Muster ohne überflüssige Escapes vor ``:`` und ``/``, damit es auch als ECMA-262 mit Flag ``u`` gilt."""
    return re.sub(r"\\([:/])", r"\1", pattern)


def _property(prop: dict[str, Any], where: str) -> dict[str, Any]:
    kind = prop.get("type")
    if kind == "object":
        target = prop.get("schema")
        if not target:
            return {"type": "object"}
        name = str(target).removesuffix(".json")
        if name not in _object_schemas():
            raise ValueError(f"kommParl {FASSUNG}, {where}: Verweis auf unbekanntes Schema {target!r}")
        return {"$ref": f"{_REF}{component_name(name)}"}
    if kind == "array":
        if "items" not in prop:
            raise ValueError(f"kommParl {FASSUNG}, {where}: Liste ohne items")
        return {"type": "array", "items": _property(prop["items"], where)}
    if kind == "string":
        result: dict[str, Any] = {"type": "string"}
        if "format" in prop:
            if prop["format"] not in FORMATS:
                raise ValueError(f"kommParl {FASSUNG}, {where}: unbekanntes Format {prop['format']!r}")
            result["format"] = FORMATS[prop["format"]]
        if "pattern" in prop:
            literal = _literal(str(prop["pattern"]))
            if literal is not None and "format" not in prop:
                return {"const": literal}
            result["pattern"] = _pattern(str(prop["pattern"]))
        if "format" not in prop:
            result["minLength"] = 1
        return result
    if kind in ("boolean", "integer"):
        return {"type": kind}
    raise ValueError(f"kommParl {FASSUNG}, {where}: unbekannter Typ {kind!r}")


def type_schema(typ: str, *, vendor_property: str = VENDOR_PROPERTY) -> dict[str, Any]:
    """
    Schema eines Objekttyps als JSON Schema 2020-12 für OpenAPI 3.1 (Regeln oben). ``vendor_property`` ist das Muster
    der Eigenschaften mit Herstellerpräfix, die das Schema nicht nennt.
    """
    raw = _object_schemas()[typ]
    properties = {name: _property(prop, f"{typ}.{name}") for name, prop in raw["properties"].items()}
    return {
        "title": typ,
        "type": "object",
        "required": list(raw["required"]),
        "properties": properties,
        "patternProperties": {vendor_property: {}},
        "additionalProperties": False,
    }


# =============================================================================
# Profile -> Komponenten der Beschreibung
# =============================================================================


def profile_component_name(name: str) -> str:
    """Name eines Profilschemas in der Beschreibung, z. B. ``KommparlChangeList``."""
    return f"Kommparl{name}"


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_inline(value, defs) for value in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = _inline(defs[ref.removeprefix("#/$defs/")], defs)
        siblings = {key: _inline(value, defs) for key, value in node.items() if key != "$ref"}
        return {**target, **siblings}
    return {key: _inline(value, defs) for key, value in node.items()}


def profile_component(profile: str, name: str) -> dict[str, Any]:
    """
    Schema eines Profils als Komponente der Beschreibung: ohne ``$schema`` und ``$id`` (eine eigene Basisadresse
    würde Verweise der Beschreibung umlenken), mit aufgelösten ``$defs``; die Kennung steht in
    ``x-kommparl-schema``. Bedeutung und Strenge bleiben gleich.
    """
    raw = profile_schema(profile, name)
    defs = raw.pop("$defs", {})
    schema_id = raw.pop("$id", None)
    raw.pop("$schema", None)
    component: dict[str, Any] = _inline(raw, defs)
    if schema_id:
        component["x-kommparl-schema"] = schema_id
    return component
