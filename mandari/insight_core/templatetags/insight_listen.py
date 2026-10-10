# SPDX-License-Identifier: AGPL-3.0-or-later
"""Filter für Listen des Bürgerportals (Issue #841): Spalten nur zeigen, wenn sie etwas enthalten."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django import template

from ..ris_links import INSIGHT

register = template.Library()

#: Werte, die in einer Zelle nur als Platzhalter erscheinen würden
LEER = {"", "-", "–", "—"}


def _wert(objekt: Any, pfad: str) -> Any:
    for teil in pfad.split("."):
        if objekt is None:
            return None
        objekt = objekt.get(teil) if isinstance(objekt, dict) else getattr(objekt, teil, None)
    return objekt


def ist_leer(wert: Any) -> bool:
    """Leer sind ``None``, ``0``, leere Folgen und Zeichenketten aus Platzhaltern („—“, „-“)."""
    if wert is None or wert is False:
        return True
    if isinstance(wert, str):
        return wert.strip() in LEER
    if isinstance(wert, int | float):
        return wert == 0
    if isinstance(wert, list | tuple | set | dict):
        return not wert
    return False


@register.filter
def hat_wert(objekte: Iterable[Any] | None, pfad: str) -> bool:
    """
    Hat mindestens ein Eintrag unter ``pfad`` (Attribut oder Schlüssel, Punkte für Tiefe) einen Wert?

    Eine Spalte, die nur „—“ zeigen würde, lassen die Listen weg: ``{% if persons|hat_wert:"email" %}``.
    Ein QuerySet wird dabei ausgewertet und für die folgende Schleife zwischengespeichert.
    """
    if not objekte:
        return False
    return any(not ist_leer(_wert(objekt, pfad)) for objekt in objekte)


@register.simple_tag(takes_context=True)
def seiten_url(context: Any, nummer: int) -> str:
    """Link auf Seite ``nummer`` einer Liste mit allen übrigen Parametern der Anfrage (Suche, Filter, Reiter)."""
    request = context.get("request")
    params = request.GET.copy() if request is not None else None
    if params is None:
        return f"?page={nummer}"
    params["page"] = str(nummer)
    return "?" + str(params.urlencode())


@register.simple_tag
def ris_url(links: Any, art: str, kennung: Any) -> str:
    """
    Ziel eines Eintrags in den gemeinsamen RIS-Bausteinen (Issue #853): über ``links`` (``insight_core.ris_links``),
    ohne Angabe die Detailseite in Insight. ``{% ris_url links "vorgang" paper.pk as ziel %}``.
    """
    return str((links or INSIGHT).url(art, kennung))
