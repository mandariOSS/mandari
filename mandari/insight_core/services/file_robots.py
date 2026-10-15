# SPDX-License-Identifier: AGPL-3.0-or-later
"""
robots.txt der Ratsinformationssysteme für die Dateiabrufe des Löschabgleichs (Issue #787).

Die robots.txt einer Quelle ist für unsere automatischen Abrufe verbindlich. Manche Quellen erlauben
die OParl-Schnittstelle, sperren aber die Dokumente, typischerweise mit ``Disallow: /*.pdf$``. Das ist
ein maschinenlesbarer Nutzungsvorbehalt (§ 44b Abs. 3 UrhG); unsere Abrufe der Dateien unterbleiben
dann.

Seit Issue #1027 prüft der Löschabgleich mit derselben Auswertung wie alle anderen Abrufe
(:mod:`insight_core.services.robots`, RFC 9309 in :mod:`mandari_oparl.robots`): maßgeblich ist die Gruppe für
``mandari-ingestor`` (bzw. das Token einer abweichenden Kennung der Quelle), sonst ``*``; Gruppen für unsere
früheren Kennungen ``mandari-file-cache`` und ``mandari`` gelten zusätzlich, wenn es sie gibt. Die robots.txt
liegt im gemeinsamen Django-Cache, ihr Abruf zählt zum Takt je Host. Keine robots.txt (4xx) bedeutet erlaubt; ist
sie nicht erreichbar (5xx, 408, 429, Netzfehler), unterbleibt der Abruf, bis sie wieder zu lesen ist.

Eine Ausnahme je Quelle steht in ``sync_config["robots_override"]`` mit Bereich und Pflicht-Vermerk,
z. B. ``{"scope": "files", "note": "Zustimmung liegt vor, Anfrage läuft"}`` – dasselbe Format wie für
die übrigen Abrufe der Quelle (:func:`mandari_oparl.robots.robots_override`). Für Dateien gelten die Bereiche
``files`` und ``all``; ohne Vermerk von mindestens zehn Zeichen gibt es keine Ausnahme.
"""

from __future__ import annotations

from mandari_oparl.robots import KIND_FILES, robots_override

from . import robots

ALLOWED = "erlaubt"
DISALLOWED = "gesperrt"
UNREACHABLE = "nicht_lesbar"


def exception_note(source: object) -> str:
    """Vermerk einer Ausnahme der Quelle für Dateiabrufe (leer = keine gültige Ausnahme)."""
    override = robots_override(getattr(source, "sync_config", None))
    return override.note if override is not None and override.covers(KIND_FILES) else ""


def robots_status(source: object, url: str) -> str:
    """Was die robots.txt allein (ohne Ausnahme der Quelle) zu ``url`` sagt: ``erlaubt``, ``gesperrt``, ``nicht_lesbar``."""
    decision = robots.decide(url, agent=robots.user_agent_for(source), sync_config=getattr(source, "sync_config", None))
    if decision.unreachable:
        return UNREACHABLE
    return ALLOWED if decision.allowed else DISALLOWED


def file_fetch_status(source: object, url: str) -> str:
    """``erlaubt``, ``gesperrt`` oder ``nicht_lesbar`` für einen automatischen Abruf von ``url`` (mit Ausnahme)."""
    if exception_note(source):
        return ALLOWED
    return robots_status(source, url)
