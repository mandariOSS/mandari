# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Werkzeugaufrufe, die ein Modell als Text in seine Antwort schreibt (Issue #1436).

Manche Modelle liefern einen Aufruf nicht strukturiert (``tool_calls``), sondern in ihrer eigenen Schreibweise im
Antworttext, etwa wenn die Schlussrunde keine Werkzeuge mehr erlaubt oder der Anbieter die Schreibweise nicht erkennt:

* ``<tool_call>name<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>``
* ``<tool_call>{"name": "…", "arguments": {…}}</tool_call>``

Solcher Text ist nie Teil einer Antwort. :func:`lesen` trennt ihn ab und gibt die lesbaren Aufrufe bekannter Werkzeuge
zurück; der Chat-Dienst führt sie aus oder fragt nach einer Antwort ohne Aufruf (``chat_service``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

#: Ein Aufruf; fehlt das Ende (abgeschnittene Antwort), gehört der Rest des Textes dazu
_BLOCK_RE = re.compile(r"<tool_call>(.*?)(?:</tool_call>|\Z)", re.DOTALL | re.IGNORECASE)
#: Einzelne Marken, die ohne ihren Aufruf stehen geblieben sind
_MARKE_RE = re.compile(r"</?(?:tool_call|arg_key|arg_value)>", re.IGNORECASE)
_NAME_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)")
#: Ein Argument; statt ``<arg_key>`` schreiben Modelle gelegentlich eine öffnende Klammer
_PAAR_RE = re.compile(
    r"(?:<arg_key>|\()\s*([A-Za-z_][A-Za-z0-9_]*)\s*</arg_key>\s*<arg_value>(.*?)</arg_value>",
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class Gelesen:
    """Antworttext ohne Werkzeugaufrufe, die lesbaren Aufrufe und ob der Text überhaupt welche enthielt."""

    text: str
    aufrufe: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    #: Der Text enthielt Aufrufe oder ihre Marken, auch unlesbare oder solche unbekannter Werkzeuge
    markiert: bool = False


def _wert(roh: str) -> Any:
    wert = roh.strip()
    if wert in ("true", "false"):
        return wert == "true"
    return wert


def _aufruf(rumpf: str) -> tuple[str, dict[str, Any]] | None:
    rumpf = rumpf.strip()
    if rumpf.startswith("{"):
        try:
            daten = json.loads(rumpf)
        except ValueError:
            return None
        if not isinstance(daten, dict):
            return None
        name = daten.get("name")
        argumente = daten.get("arguments", daten.get("parameters"))
        if isinstance(argumente, str):
            try:
                argumente = json.loads(argumente or "{}")
            except ValueError:
                return None
        if argumente is None:
            argumente = {}
        if not isinstance(name, str) or not isinstance(argumente, dict):
            return None
        return name, argumente
    kopf = _NAME_RE.match(rumpf)
    if kopf is None:
        return None
    return kopf.group(1), {name: _wert(wert) for name, wert in _PAAR_RE.findall(rumpf)}


def lesen(inhalt: str, namen: Collection[str]) -> Gelesen:
    """
    Werkzeugaufrufe aus dem Antworttext nehmen.

    Args:
        inhalt: Antworttext des Modells
        namen: Namen der Werkzeuge, die es gibt; Aufrufe anderer Namen werden nur entfernt

    Returns:
        Text ohne die Aufrufe, die lesbaren Aufrufe ``[(name, argumente)]`` in ihrer Reihenfolge und ``markiert``
    """
    if not inhalt or "<" not in inhalt or not _MARKE_RE.search(inhalt):
        return Gelesen(inhalt or "")
    aufrufe: list[tuple[str, dict[str, Any]]] = []
    for rumpf in _BLOCK_RE.findall(inhalt):
        aufruf = _aufruf(rumpf)
        if aufruf is not None and aufruf[0] in namen:
            aufrufe.append(aufruf)
    text = _MARKE_RE.sub("", _BLOCK_RE.sub("", inhalt)).strip()
    return Gelesen(text, aufrufe, markiert=True)
