# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Filter und Reiter der Listen in der Adresse (Issue #1260).

Vorgänge, Sitzungen, Gremien und Personen filtern per htmx nur die Liste. Damit sich ein Ergebnis teilen lässt und
„Zurück“ die Auswahl behält, steht jede Auswahl als GET-Parameter in der Adresse (``hx-push-url``). Die Adresse trägt
nur, was gewählt ist: leere Felder („Alle Typen“, leere Suche) fallen weg, die Seitenzahl gilt nur für die Seitenlinks.
"""

from __future__ import annotations

from django.http import HttpRequest, QueryDict


def ohne_leere(params: QueryDict) -> QueryDict:
    """Kopie der Parameter ohne leere Werte (auch Felder nur aus Leerzeichen)."""
    sauber = QueryDict(mutable=True)
    for name in params:
        werte = [wert for wert in params.getlist(name) if str(wert).strip()]
        if werte:
            sauber.setlist(name, werte)
    return sauber


def saubere_adresse(request: HttpRequest) -> str:
    """Pfad der Anfrage mit den gewählten Filtern, ohne leere Parameter und ohne Seitenzahl."""
    params = ohne_leere(request.GET)
    params.pop("page", None)
    query = params.urlencode()
    return f"{request.path}?{query}" if query else request.path
