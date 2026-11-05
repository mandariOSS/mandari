# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die eine OpenAPI-3.1-Beschreibung aller Bereiche (``docs/adr/20261105-eine-schnittstelle-api-first.md``).

``build_description()`` fügt den Einstieg ``/api/`` und alle angemeldeten Bereiche (``registry``) zu einer
Beschreibung zusammen:

- Pfade aller Bereiche; jeder Endpunkt trägt den Namen seines Bereichs als Schlagwort (``tags``).
- ``components`` aller Bereiche; gleiche Namen sind nur erlaubt, wenn der Inhalt gleich ist.
- ``operationId`` ist über alle Bereiche eindeutig.

Ein Konflikt ist ein Programmierfehler und bricht ab (``ImproperlyConfigured``). Pfade und Schemas sind
sortiert, die Beschreibung hängt nicht vom Host einer Anfrage ab: Sie ist als Datei eingecheckt
(``docs/schnittstelle/openapi.json``, ``manage.py schnittstelle_beschreibung``), und die CI erkennt
brechende Änderungen gegenüber ``dev``.

Ein Endpunkt, der einen Befehl nach A6 annimmt, nennt ihn in ``x-mandari-command`` (Name des Befehls im
Register ``hub.contracts``). Jeder registrierte Befehl braucht einen solchen Endpunkt, Ausnahmen stehen in
``befehle_ohne_endpunkt.json`` (Liste, die nur kürzer werden darf).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from typing import Any, Final

from django.core.exceptions import ImproperlyConfigured

from apps.common.api.problems import PROBLEM_SCHEMA
from hub.api import kommparl

from .registry import Area, Description, areas

OPENAPI_VERSION: Final = "3.1.0"

#: Version der Beschreibung; wechselt nur mit einer brechenden Änderung des Einstiegs
DESCRIPTION_VERSION: Final = "1"

#: Erweiterung eines Endpunkts, der einen Befehl annimmt
COMMAND_EXTENSION: Final = "x-mandari-command"

METHODS: Final = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

TITLE: Final = "mandari-Schnittstelle"

DESCRIPTION: Final = """
Eine Schnittstelle für alle Bereiche von mandari. Einstieg: `GET /api/`.

- Ratsdaten kommen als OParl 1.1; offene Erweiterungen tragen das Präfix `kommparl:`, Erweiterungen nur für
  mandari das Präfix `mandari:`.
- Die Adressen der OParl-Objekte sind ihre Kennungen und ändern sich nicht.
- Fehler kommen als `application/problem+json` (RFC 9457); die OParl-Ausgaben melden einfache Fehler als
  `{"error", "status"}`.
- Bedingte Abrufe über `ETag` und `If-None-Match`; abgekündigte Endpunkte tragen `Deprecation` (RFC 9745) und
  `Sunset` (RFC 8594).
- Schreibende Aufrufe sind Befehle mit `Idempotency-Key`.
""".strip()


def build_description(*, extra: Iterable[tuple[Area, Description]] = ()) -> dict[str, Any]:
    """
    Gesamtbeschreibung aus allen angemeldeten Bereichen; ``extra`` sind Bereiche mit fertiger Beschreibung, die
    vor den angemeldeten stehen (der Einstieg).
    """
    document: dict[str, Any] = {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": TITLE,
            "version": DESCRIPTION_VERSION,
            "description": f"{DESCRIPTION}\n\n{kommparl.attribution()}",
        },
        "tags": [],
        "paths": {},
        "components": {"schemas": {"Problem": copy.deepcopy(PROBLEM_SCHEMA)}},
    }
    parts = [*extra, *((area, area.openapi()) for area in areas())]
    operation_ids: dict[str, str] = {}
    for area, part in parts:
        document["tags"].append({"name": area.title, "description": area.description})
        _merge_paths(document["paths"], part.get("paths", {}), area, operation_ids)
        _merge_components(document["components"], part.get("components", {}), area)
    document["paths"] = dict(sorted(document["paths"].items()))
    document["components"] = {
        section: dict(sorted(entries.items())) for section, entries in sorted(document["components"].items())
    }
    return document


def _merge_paths(paths: dict[str, Any], part: Mapping[str, Any], area: Area, operation_ids: dict[str, str]) -> None:
    for path, item in part.items():
        if path in paths:
            raise ImproperlyConfigured(f"Pfad {path} gibt es in zwei Bereichen (zuletzt {area.key})")
        item = copy.deepcopy(dict(item))
        for method in METHODS:
            operation = item.get(method)
            if operation is None:
                continue
            operation["tags"] = [area.title]
            operation_id = operation.get("operationId")
            if not operation_id:
                raise ImproperlyConfigured(f"{method.upper()} {path} ({area.key}) hat keine operationId")
            if operation_id in operation_ids:
                raise ImproperlyConfigured(
                    f"operationId {operation_id} doppelt: {operation_ids[operation_id]} und {method.upper()} {path}"
                )
            operation_ids[operation_id] = f"{method.upper()} {path}"
        paths[path] = item


def _merge_components(components: dict[str, Any], part: Mapping[str, Any], area: Area) -> None:
    for section, entries in part.items():
        target = components.setdefault(section, {})
        for name, value in entries.items():
            if name in target and target[name] != value:
                raise ImproperlyConfigured(
                    f"components.{section}.{name} unterscheidet sich zwischen Bereichen ({area.key})"
                )
            target[name] = copy.deepcopy(value)


def operations(document: Mapping[str, Any]) -> Iterable[tuple[str, str, Mapping[str, Any]]]:
    """Alle Endpunkte einer Beschreibung: ``(Methode, Pfad, Operation)``."""
    for path, item in document.get("paths", {}).items():
        for method in METHODS:
            if method in item:
                yield method, path, item[method]


def commands_with_endpoint(document: Mapping[str, Any]) -> dict[str, list[str]]:
    """Befehl (``x-mandari-command``) -> Endpunkte, die ihn annehmen."""
    found: dict[str, list[str]] = {}
    for method, path, operation in operations(document):
        name = operation.get(COMMAND_EXTENSION)
        if name:
            found.setdefault(str(name), []).append(f"{method.upper()} {path}")
    return found


def render(document: Mapping[str, Any]) -> str:
    """Beschreibung als Text, wie sie eingecheckt wird: JSON mit Einrückung, UTF-8, Zeilenende am Schluss."""
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
