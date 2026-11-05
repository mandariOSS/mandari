# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Testhilfe: prüft OParl-Objekte gegen die Feldtypen von OParl 1.1.

Die Tabelle ``FELDER`` nennt je Objekttyp die Eigenschaften der Spezifikation mit ihrem Typ, ``PFLICHT``
die zwingenden Eigenschaften (beide in ``hub.api.oparl_types``, dieselbe Quelle wie die Schemas der
OpenAPI-Beschreibung). ``pruefe`` läuft ein Objekt samt eingebetteter Objekte ab und liefert die
Abweichungen als Liste von Sätzen; eine leere Liste heißt konform. Beide Ausgaben (Aggregator und
Session-Schnittstelle) prüfen ihre Antworten damit.

Geprüft wird:

- Pflichtfelder vorhanden, ``type`` passt zum Objekttyp
- Typen: URL, Text, Datum (``yyyy-mm-dd``), Zeitpunkt mit Zeitzone, Wahrheitswert, Ganzzahl, Listen,
  eingebettete Objekte
- keine leeren Texte, keine unbekannten Eigenschaften ohne Namensraum (eigene Felder heißen ``präfix:name``)
- ``organizationType`` ist einer der sieben Werte der Spezifikation
- gelöschte Objekte tragen nur ``id``, ``type``, ``created``, ``modified``, ``deleted``
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from hub.api.oparl_types import ALLGEMEIN, FELDER, ORGANIZATION_TYPES, PFLICHT, SCHEMA, TOMBSTONE

__all__ = ["FELDER", "ORGANIZATION_TYPES", "PFLICHT", "SCHEMA", "pruefe", "pruefe_liste"]

_DATUM = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ist_url(wert: Any) -> bool:
    return isinstance(wert, str) and wert.startswith(("http://", "https://")) and " " not in wert


def _ist_datum(wert: Any) -> bool:
    if not isinstance(wert, str) or not _DATUM.match(wert):
        return False
    try:
        date.fromisoformat(wert)
    except ValueError:
        return False
    return True


def _ist_zeitpunkt(wert: Any) -> bool:
    if not isinstance(wert, str) or "T" not in wert:
        return False
    try:
        return datetime.fromisoformat(wert).tzinfo is not None
    except ValueError:
        return False


def _pruefe_wert(pfad: str, art: str, wert: Any, probleme: list[str]) -> None:
    if art.startswith("objekt:"):
        if isinstance(wert, dict):
            probleme.extend(pruefe(wert, art.split(":", 1)[1], pfad=pfad))
        else:
            probleme.append(f"{pfad}: eingebettetes Objekt erwartet, erhalten {wert!r}")
        return
    if art.startswith("objekte:"):
        if not isinstance(wert, list) or not all(isinstance(eintrag, dict) for eintrag in wert):
            probleme.append(f"{pfad}: Liste eingebetteter Objekte erwartet, erhalten {wert!r}")
            return
        for nummer, eintrag in enumerate(wert):
            probleme.extend(pruefe(eintrag, art.split(":", 1)[1], pfad=f"{pfad}[{nummer}]"))
        return
    pruefungen = {
        "url": _ist_url(wert),
        "text": isinstance(wert, str) and wert != "",
        "datum": _ist_datum(wert),
        "zeitpunkt": _ist_zeitpunkt(wert),
        "bool": isinstance(wert, bool),
        "zahl": isinstance(wert, int) and not isinstance(wert, bool),
        "urls": isinstance(wert, list) and all(_ist_url(eintrag) for eintrag in wert),
        "texte": isinstance(wert, list) and all(isinstance(e, str) and e != "" for e in wert),
        "geojson": isinstance(wert, dict),
    }
    if not pruefungen[art]:
        probleme.append(f"{pfad}: {art} erwartet, erhalten {wert!r}")


def pruefe(objekt: dict[str, Any], typ: str, *, pfad: str = "") -> list[str]:
    """Abweichungen eines OParl-Objekts vom Typ ``typ`` (samt eingebetteter Objekte); leer = konform."""
    pfad = pfad or typ
    probleme: list[str] = []
    if objekt.get("type") != f"{SCHEMA}{typ}":
        probleme.append(f"{pfad}.type: {SCHEMA}{typ} erwartet, erhalten {objekt.get('type')!r}")
    if objekt.get("deleted") is True:
        ueberzaehlig = sorted(set(objekt) - TOMBSTONE)
        if ueberzaehlig:
            probleme.append(f"{pfad}: gelöschtes Objekt trägt weitere Eigenschaften {ueberzaehlig}")
        pflicht: tuple[str, ...] = ("id", "type")
    else:
        pflicht = PFLICHT.get(typ, ("id", "type"))
    for name in pflicht:
        if name not in objekt:
            probleme.append(f"{pfad}.{name}: Pflichtfeld fehlt")
    felder = {**ALLGEMEIN, **FELDER[typ]}
    for name, wert in objekt.items():
        if ":" in name:
            continue  # eigene Felder im Namensraum eines Herstellers
        art = felder.get(name)
        if art is None:
            probleme.append(f"{pfad}.{name}: unbekannte Eigenschaft ohne Namensraum")
            continue
        if wert is None:
            continue
        _pruefe_wert(f"{pfad}.{name}", art, wert, probleme)
    if typ == "Organization" and "organizationType" in objekt and objekt["organizationType"] not in ORGANIZATION_TYPES:
        probleme.append(f"{pfad}.organizationType: kein Wert der Spezifikation: {objekt['organizationType']!r}")
    return probleme


def pruefe_liste(liste: dict[str, Any], typ: str) -> list[str]:
    """Abweichungen einer externen Objektliste (Hülle ``data``/``pagination``/``links`` und alle Objekte)."""
    probleme: list[str] = []
    for name in ("data", "pagination", "links"):
        if name not in liste:
            probleme.append(f"Liste {typ}: {name} fehlt")
    for nummer, objekt in enumerate(liste.get("data", [])):
        probleme.extend(pruefe(objekt, typ, pfad=f"{typ}[{nummer}]"))
    for name, wert in liste.get("links", {}).items():
        if not _ist_url(wert):
            probleme.append(f"Liste {typ}: links.{name} ist keine URL: {wert!r}")
    return probleme
