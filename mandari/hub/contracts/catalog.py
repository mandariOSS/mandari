# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereigniskatalog aus dem Vertragsregister (``docs/adr/20260929-ereignisvertraege.md``, Abschnitt Katalog).

Die lesbare Übersicht aller Ereignisse und Befehle wird erzeugt, nicht von Hand gepflegt:
``render_catalog()`` schreibt aus Register und Hülle eine Markdown-Datei mit Übersicht, Feldern je Version
und Beispielen. Sie liegt unter ``docs/EREIGNISKATALOG.md``; die CI erzeugt sie neu und vergleicht
(``scripts/check_event_contracts.py``). Die Ausgabe hängt nur von den Schemas ab (sortiert, ohne Datum),
damit derselbe Stand immer dieselbe Datei ergibt.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any, Final

from .envelope import ENVELOPE_VERSION
from .naming import COMMAND, EVENT
from .registry import Contract, Registry

#: Pfad der Schemas relativ zu ``docs/`` (Verweise im Katalog).
SCHEMA_LINK_ROOT: Final = "../mandari/hub/contracts"

_TYPES: Final[dict[str, str]] = {
    "string": "Zeichenkette",
    "integer": "Ganzzahl",
    "number": "Zahl",
    "boolean": "Wahrheitswert",
    "object": "Objekt",
    "array": "Liste",
    "null": "null",
}
_KIND_LABEL: Final[dict[str, str]] = {EVENT: "Ereignis", COMMAND: "Befehl"}


def _cell(text: str) -> str:
    """Text für eine Tabellenzelle: einzeilig, senkrechte Striche maskiert."""
    return " ".join(text.split()).replace("|", "\\|")


def _prose(text: str) -> str:
    """Fließtext aus einem Schema: spitze Klammern maskiert (``<bereich>`` ist kein HTML-Element)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _resolve(schema: Any, root: Mapping[str, Any]) -> Any:
    """Lokalen Verweis ``#/$defs/<name>`` auflösen (die Hülle nutzt ihn); sonst unverändert."""
    if isinstance(schema, dict) and isinstance(schema.get("$ref"), str) and schema["$ref"].startswith("#/$defs/"):
        target = root.get("$defs", {}).get(schema["$ref"].removeprefix("#/$defs/"))
        if isinstance(target, dict):
            merged = dict(target)
            merged.update({k: v for k, v in schema.items() if k != "$ref"})
            return merged
    return schema


def describe_type(schema: Any, root: Mapping[str, Any]) -> str:
    """Kurzbeschreibung eines Feldtyps, z. B. ``Zeichenkette (uuid)`` oder ``Code: `a`, `b```."""
    schema = _resolve(schema, root)
    if not isinstance(schema, dict):
        return "beliebig" if schema is True else "–"
    if "const" in schema:
        return f"Konstante `{json.dumps(schema['const'], ensure_ascii=False)}`"
    if "enum" in schema:
        return "Code: " + ", ".join(f"`{value}`" for value in schema["enum"])
    for keyword in ("anyOf", "oneOf"):
        if isinstance(schema.get(keyword), list):
            return " oder ".join(describe_type(part, root) for part in schema[keyword])
    kind = schema.get("type")
    names = kind if isinstance(kind, list) else [kind] if kind else []
    text = " oder ".join(_TYPES.get(str(name), str(name)) for name in names) or "beliebig"
    details: list[str] = []
    if "format" in schema:
        details.append(str(schema["format"]))
    if "pattern" in schema:
        details.append(f"Muster `{schema['pattern']}`")
    if "maxLength" in schema:
        details.append(f"höchstens {schema['maxLength']} Zeichen")
    if "minimum" in schema or "maximum" in schema:
        details.append(f"{schema.get('minimum', '…')} bis {schema.get('maximum', '…')}")
    if "array" in names:
        text = f"Liste aus {describe_type(schema.get('items', True), root)}"
        if "maxItems" in schema:
            details.append(f"{schema.get('minItems', 0)} bis {schema['maxItems']} Einträge")
    return f"{text} ({', '.join(details)})" if details else text


def field_rows(schema: Mapping[str, Any], root: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, ...]]:
    """Zeilen (Feld, Pflicht, Typ, Beschreibung) eines Objektschemas, verschachtelte Felder mit Punkt."""
    required = set(schema.get("required", []))
    for name, sub in schema.get("properties", {}).items():
        sub = _resolve(sub, root)
        label = f"{prefix}{name}"
        flags = "ja" if name in required else "nein"
        if isinstance(sub, dict) and sub.get("x-content"):
            flags += ", Inhalt"
        description = str(sub.get("description", "")) if isinstance(sub, dict) else ""
        yield (f"`{label}`", flags, describe_type(sub, root), _prose(description))
        if isinstance(sub, dict) and isinstance(sub.get("properties"), dict):
            yield from field_rows(sub, root, f"{label}.")
        items = sub.get("items") if isinstance(sub, dict) else None
        if isinstance(items, dict) and isinstance(items.get("properties"), dict):
            yield from field_rows(items, root, f"{label}[].")


def _table(rows: list[tuple[str, ...]], header: tuple[str, ...]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return lines


def _visibility(contract: Contract) -> str:
    return ", ".join(sorted(contract.visibility))


def _contract_section(contract: Contract) -> list[str]:
    schema = contract.schema
    link = f"{SCHEMA_LINK_ROOT}/schemas/{contract.name}/v{contract.version}.json"
    lines = [
        f"### {contract.name} v{contract.version}",
        "",
        f"**{_prose(contract.title)}.** {_prose(contract.description)}",
        "",
        f"- Art: {_KIND_LABEL[contract.kind]}",
        f"- Eigentümer: `{contract.owner}`",
        f"- Sichtbarkeit: {_visibility(contract)}",
        f"- Schema: [`{contract.name}/v{contract.version}.json`]({link})",
        "",
    ]
    rows = list(field_rows(schema, schema))
    lines += _table(rows, ("Feld", "Pflicht", "Typ", "Beschreibung")) if rows else ["Keine Felder."]
    for index, example in enumerate(contract.examples, start=1):
        lines += ["", f"Beispiel {index}:", "", "```json", json.dumps(example, ensure_ascii=False, indent=2), "```"]
    lines.append("")
    return lines


def render_catalog(registry: Registry, envelope: Mapping[str, Any]) -> str:
    """Der Ereigniskatalog als Markdown (Zeilenende ``\\n``, endet mit einer Leerzeile)."""
    contracts = registry.contracts()
    lines = [
        "<!-- Erzeugt aus dem Vertragsregister. Nicht von Hand ändern, sondern neu erzeugen:",
        "     python scripts/check_event_contracts.py --write-catalog -->",
        "",
        "# Ereigniskatalog",
        "",
        "Alle Ereignisse und Befehle der Datendrehscheibe mit Version, Eigentümer, Sichtbarkeit, Feldern und",
        f"Beispielen. Quelle sind die Schemas unter [`mandari/hub/contracts/schemas/`]({SCHEMA_LINK_ROOT}/schemas/);",
        "Regeln für Namen, Sichtbarkeit und Änderungen stehen in",
        "[Verträge für Ereignisse und Befehle](adr/20260929-ereignisvertraege.md). Eine bestehende Version",
        "ändert sich nur ergänzend (neue optionale Felder, neue Codes); alles andere ergibt eine neue Version.",
        "Die CI prüft das und erzeugt diese Datei neu (`scripts/check_event_contracts.py`).",
        "",
        "## Übersicht",
        "",
    ]
    overview: list[tuple[str, ...]] = []
    for name in registry.names():
        latest = registry.latest(name)
        versions = ", ".join(str(v) for v in registry.versions(name))
        overview.append(
            (
                f"`{name}`",
                _KIND_LABEL[latest.kind],
                versions,
                f"`{latest.owner}`",
                _visibility(latest),
                _prose(latest.title),
            )
        )
    lines += _table(overview, ("Typ", "Art", "Versionen", "Eigentümer", "Sichtbarkeit", "Titel"))
    lines += [
        "",
        f"## Ereignishülle v{ENVELOPE_VERSION}",
        "",
        f"**{_prose(str(envelope.get('title', '')))}.** {_prose(str(envelope.get('description', '')))}",
        "",
        f"Schema: [`envelope/v{ENVELOPE_VERSION}.json`]({SCHEMA_LINK_ROOT}/envelope/v{ENVELOPE_VERSION}.json)",
        "",
    ]
    lines += _table(list(field_rows(envelope, envelope)), ("Feld", "Pflicht", "Typ", "Beschreibung"))
    for kind, heading in ((EVENT, "Ereignisse"), (COMMAND, "Befehle")):
        selected = [c for c in contracts if c.kind == kind]
        if not selected:
            continue
        lines += ["", f"## {heading}", ""]
        for contract in selected:
            lines += _contract_section(contract)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"
