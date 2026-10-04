# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kommunenwechsel (Issue #783, Stufe 2): Vorschläge, Nähe und Stöbern für Tausende Kommunen.

Drei kleine JSON-Schnittstellen für den Dialog „Kommune wechseln“ (``frontend/alpine/kommunen-wahl.ts``) und eine
Seite, die ohne JavaScript dasselbe leistet (Suche per Formular, Stöbern über Links). Keine der Antworten hängt
von der gewählten Kommune ab oder legt eine Sitzung an; der Browser darf sie zwischenspeichern. Weil die
Anmeldeprüfung jede Anfrage sieht, tragen sie „Vary: Cookie“: Ein gemeinsamer Cache teilt sie nur zwischen Anfragen
mit demselben Cookie. Der Standort für „In meiner Nähe“ kommt nur als Zelle von 0,1 Grad an und wird nirgends
gespeichert. Stammt das Verzeichnis aus einer Datei, nennen alle Antworten ihre Quellen (``quellen``).
"""

from __future__ import annotations

import re
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.cache import patch_cache_control
from django.views.decorators.http import require_GET

from ..services import kommunenverzeichnis as verzeichnis

#: Länge der Eingabe, die überhaupt ausgewertet wird
MAX_EINGABE = 80
_LAND = re.compile(r"^\d{2}$")
_KREIS = re.compile(r"^\d{5}$")
_VERBAND = re.compile(r"^\d{9}$")
_ZELLE = re.compile(r"^(-?\d{1,2}(?:\.\d{1,2})?),(-?\d{1,3}(?:\.\d{1,2})?)$")
#: Grob Deutschland mit Rand; außerhalb gibt es keine Kommunen im Verzeichnis
_BREITE = (46.0, 56.0)
_LAENGE = (4.0, 16.5)


def _json(daten: dict[str, Any], max_age: int) -> JsonResponse:
    quellen = verzeichnis.quellen()
    if quellen:
        # Namensnennung der Quellen (u. a. ODbL) auch in den Auszügen des Verzeichnisses
        daten = {**daten, "quellen": quellen}
    antwort = JsonResponse(daten, json_dumps_params={"ensure_ascii": False})
    patch_cache_control(antwort, public=True, max_age=max_age)
    return antwort


def _stoebern_parameter(request: HttpRequest) -> tuple[str, str, str]:
    """Land, Kreis und Verband aus der Anfrage; ungültige oder widersprüchliche Angaben fallen weg."""
    land = request.GET.get("land", "")
    kreis = request.GET.get("kreis", "")
    verband = request.GET.get("verband", "")
    land = land if _LAND.match(land) else ""
    kreis = kreis if land and _KREIS.match(kreis) and kreis.startswith(land) else ""
    verband = verband if kreis and _VERBAND.match(verband) and verband.startswith(kreis) else ""
    return land, kreis, verband


@require_GET
def kommunen_vorschlaege(request: HttpRequest) -> JsonResponse:
    """Höchstens acht Vorschläge zu ``?q=`` (Name, Ortsteil oder Postleitzahl)."""
    eingabe = request.GET.get("q", "")[:MAX_EINGABE]
    treffer = verzeichnis.suchen(eingabe)
    return _json({"treffer": [t.als_dict() for t in treffer]}, max_age=300)


@require_GET
def kommunen_naehe(request: HttpRequest) -> JsonResponse:
    """Kandidaten rund um ``?zelle=breite,laenge`` (eine bis zwei Nachkommastellen, Deutschland)."""
    passend = _ZELLE.match(request.GET.get("zelle", ""))
    if passend is None:
        return JsonResponse({"fehler": "Standort fehlt oder ist ungültig."}, status=400)
    breite, laenge = float(passend.group(1)), float(passend.group(2))
    if not (_BREITE[0] <= breite <= _BREITE[1] and _LAENGE[0] <= laenge <= _LAENGE[1]):
        return _json({"zelle": None, "kandidaten": [], "naechste_mit_daten": None}, max_age=3600)
    return _json(verzeichnis.in_der_naehe(breite, laenge), max_age=3600)


@require_GET
def kommunen_stoebern(request: HttpRequest) -> JsonResponse:
    """Eine Stufe Land → Kreis → (Gemeindeverband →) Kommune."""
    land, kreis, verband = _stoebern_parameter(request)
    return _json(verzeichnis.stoebern(land, kreis, verband), max_age=300)


@require_GET
def kommunen_seite(request: HttpRequest) -> HttpResponse:
    """Kommune wählen ohne JavaScript: Suche per Formular und Stöbern über Links."""
    eingabe = request.GET.get("q", "")[:MAX_EINGABE].strip()
    land, kreis, verband = _stoebern_parameter(request)
    context: dict[str, Any] = {"eingabe": eingabe, "verzeichnis_leer": verzeichnis.ist_leer()}
    if eingabe:
        context["treffer"] = [t.als_dict() for t in verzeichnis.suchen(eingabe)]
    else:
        context["stufe"] = verzeichnis.stoebern(land, kreis, verband)
    context["seo"] = {"robots": "noindex, follow"}
    return render(request, "pages/portal/kommunen.html", context)
