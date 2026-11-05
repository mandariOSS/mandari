# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Testhilfe: prüft Ausgaben gegen die Schemas der kommParl-Spezifikation (Issue #1216).

Eine Quelle für Beschreibung und Prüfung: Die Schemas der Objekttypen sind dieselben wie in der OpenAPI-Beschreibung
(``hub.api.openapi.oparl.component_schemas``), also aus kommParl übernommen und umgewandelt (``hub.api.kommparl``);
die Profile Änderungsfeed und Snapshot prüfen unverändert mit den übernommenen Profilschemas. Beide Ausgaben
(Aggregator und Session-Schnittstelle) prüfen ihre Antworten damit, ebenso der OParl-Validator der CI
(``scripts/oparl_validator.py``).

- ``pruefe(objekt, typ)``: ein OParl-Objekt samt eingebetteter Objekte; ein gelöschtes gegen ``OParlDeleted``.
- ``pruefe_liste(liste, typ)``: eine externe Liste (``data``, ``pagination``, ``links``) und alle ihre Objekte.
- ``pruefe_profil(daten, profil, name)``: gegen ein Profilschema, etwa ``("changes", "ChangeList")``.
- ``pruefe_snapshot(zeilen)``: ein Snapshot nach dem Profil Snapshot (Kopfzeile, Objektzeilen, Schlusszeile).

Alle liefern die Abweichungen als Liste von Sätzen; eine leere Liste heißt konform. Formate:

- ``uri``: URL mit ``http`` oder ``https``, ohne Leerraum (Format ``url`` der Spezifikation),
- ``date``: ``yyyy-mm-dd`` und ein gültiger Tag,
- ``date-time``: Datum und Uhrzeit mit Zeitzone. Sekundenbruchteile lässt die Prüfung der Objekte noch zu, obwohl
  OParl 1.1 (Kapitel „JSON-Ausgabe“) die Form ``yyyy-mm-ddThh:mm:ss±hh:mm`` verlangt; die Profilschemas sind mit
  ihrem Muster strenger.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from hub.api import kommparl
from hub.api.openapi.oparl import DELETED, LINKS, PAGINATION, component_schemas, schema_name
from hub.ris.canonical import ORGANIZATION_TYPES

__all__ = ["ORGANIZATION_TYPES", "pruefe", "pruefe_liste", "pruefe_profil", "pruefe_snapshot"]

_OPARL = kommparl.OPARL_VERSION
_URL = re.compile(r"https?://\S+")
_DATUM = re.compile(r"\d{4}-\d{2}-\d{2}")


def _ist_url(wert: object) -> bool:
    return not isinstance(wert, str) or bool(_URL.fullmatch(wert))


def _ist_datum(wert: object) -> bool:
    if not isinstance(wert, str):
        return True
    return bool(_DATUM.fullmatch(wert)) and date.fromisoformat(wert) is not None


def _ist_zeitpunkt(wert: object) -> bool:
    if not isinstance(wert, str):
        return True
    return "T" in wert and datetime.fromisoformat(wert).tzinfo is not None


#: Formate der Spezifikation (Kapitel „JSON-Ausgabe“) in der Schreibweise von JSON Schema
FORMATE = FormatChecker(formats=())
for _name, _pruefung, _fehler in (
    ("uri", _ist_url, ()),
    ("date", _ist_datum, ValueError),
    ("date-time", _ist_zeitpunkt, ValueError),
):
    FORMATE.checks(_name, raises=_fehler)(_pruefung)


@cache
def _schemas() -> dict[str, Any]:
    """
    Die Schemas der Beschreibung, mit dem Muster der Spezifikation für eigene Felder: Eigenschaften mit dem Präfix
    ``kommparl:`` sind nur erlaubt, wenn das Schema sie nennt (die Beschreibung lässt jeden Namensraum zu).
    """
    schemas = component_schemas()
    for typ in kommparl.object_types():
        schemas[schema_name(typ)]["patternProperties"] = {kommparl.VENDOR_PROPERTY: {}}
    return schemas


@cache
def _validator(name: str) -> Draft202012Validator:
    root = {"$ref": f"#/components/schemas/{name}", "components": {"schemas": _schemas()}}
    return Draft202012Validator(root, format_checker=FORMATE)


@cache
def _profil(profil: str, name: str) -> Draft202012Validator:
    schema = kommparl.profile_schema(profil, name)
    return Draft202012Validator(schema, format_checker=FORMATE)


def _saetze(fehler: Iterable[ValidationError], pfad: str) -> list[str]:
    saetze = []
    for error in sorted(fehler, key=lambda e: [str(teil) for teil in e.absolute_path]):
        stelle = "".join(f"[{teil}]" if isinstance(teil, int) else f".{teil}" for teil in error.absolute_path)
        saetze.append(f"{pfad}{stelle}: {error.message}")
    return saetze


def pruefe(objekt: dict[str, Any], typ: str, *, pfad: str = "") -> list[str]:
    """Abweichungen eines OParl-Objekts vom Typ ``typ`` (samt eingebetteter Objekte); leer = konform."""
    pfad = pfad or typ
    if objekt.get("deleted") is True:
        probleme = []
        if objekt.get("type") != f"{_OPARL}{typ}":
            probleme.append(f"{pfad}.type: {_OPARL}{typ} erwartet, erhalten {objekt.get('type')!r}")
        return probleme + _saetze(_validator(DELETED).iter_errors(objekt), pfad)
    return _saetze(_validator(schema_name(typ)).iter_errors(objekt), pfad)


def pruefe_liste(liste: dict[str, Any], typ: str) -> list[str]:
    """Abweichungen einer externen Objektliste (Hülle ``data``/``pagination``/``links`` und alle Objekte)."""
    pfad = f"Liste {typ}"
    probleme = [f"{pfad}: {name} fehlt" for name in ("data", "pagination", "links") if name not in liste]
    if not isinstance(liste.get("data", []), list):
        return [*probleme, f"{pfad}.data: keine Liste"]
    probleme += _saetze(_validator(PAGINATION).iter_errors(liste.get("pagination", {})), f"{pfad}.pagination")
    probleme += _saetze(_validator(LINKS).iter_errors(liste.get("links", {})), f"{pfad}.links")
    for nummer, objekt in enumerate(liste.get("data", [])):
        probleme += pruefe(objekt, typ, pfad=f"{typ}[{nummer}]")
    return probleme


def pruefe_profil(daten: Any, profil: str, name: str) -> list[str]:
    """Abweichungen vom Schema ``name`` des Profils ``profil`` (unverändert aus kommParl)."""
    return _saetze(_profil(profil, name).iter_errors(daten), f"{profil}/{name}")


def pruefe_snapshot(zeilen: Sequence[dict[str, Any]], body: str | None = None) -> list[str]:
    """
    Abweichungen eines Snapshots vom Profil Snapshot: Kopfzeile und Schlusszeile nach ihren Schemas, ``count`` gleich
    der Zahl der Objektzeilen, jede Objektzeile ein konformes, nicht gelöschtes OParl-Objekt, jede Kennung nur einmal
    und genau ein Body, der in der Kopfzeile genannte (``body``: dessen Kennung, wenn bekannt).
    """
    if len(zeilen) < 2:
        return ["Snapshot: Kopfzeile oder Schlusszeile fehlt"]
    kopf, *objekte, ende = zeilen
    probleme = pruefe_profil(kopf, "snapshot", "SnapshotHeader") + pruefe_profil(ende, "snapshot", "SnapshotEnd")
    if ende.get("count") != len(objekte):
        probleme.append(f"Snapshot: count ist {ende.get('count')!r}, es folgen {len(objekte)} Objektzeilen")
    gesehen: set[str] = set()
    bodies: list[Any] = []
    for nummer, objekt in enumerate(objekte, start=2):
        typ = str(objekt.get("type", "")).removeprefix(_OPARL)
        if typ not in kommparl.object_types():
            probleme.append(f"Snapshot, Zeile {nummer}: kein Objekttyp von OParl 1.1 ({objekt.get('type')!r})")
            continue
        if objekt.get("deleted"):
            probleme.append(f"Snapshot, Zeile {nummer}: gelöschtes Objekt {objekt.get('id')}")
        probleme += pruefe(objekt, typ, pfad=f"Snapshot, Zeile {nummer} ({typ})")
        if objekt.get("id") in gesehen:
            probleme.append(f"Snapshot, Zeile {nummer}: {objekt.get('id')} kommt mehrfach vor")
        gesehen.add(str(objekt.get("id")))
        if typ == "Body":
            bodies.append(objekt.get("id"))
    if bodies != [kopf.get("body")] or (body is not None and kopf.get("body") != body):
        probleme.append(f"Snapshot: genau ein Body erwartet, der der Kopfzeile ({kopf.get('body')}); gefunden {bodies}")
    return probleme
