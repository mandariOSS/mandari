# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Filter für Listen des Bürgerportals (Issue #841): Spalten nur zeigen, wenn sie etwas enthalten; Seitenlinks; Ziele
der Listen im Bürgerportal einer Körperschaft (``insight_url``, ``insight_rel``, Issue #939); Dateityp der
Dokumentlinks (``dateityp``, Issue #1103); Ergebnis-Etikett (``ergebnis``) und Reiter mit den übrigen Filtern der
Adresse (``adresse_mit``, Issue #1260).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django import template
from django.http import QueryDict
from django.urls import reverse

from ..listenadresse import ohne_leere
from ..ris_links import INSIGHT
from ..services.ergebnis import Ergebnis
from ..services.ergebnis import ergebnis as ergebnis_zuordnen

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


@register.filter
def dateityp(datei: Any) -> str:
    """
    Kurzer Dateityp für Dokumentlinks der Listen (Issue #1103): „PDF“, sonst die Endung des Dateinamens („DOCX“),
    sonst „Dokument“.
    """
    if "pdf" in str(getattr(datei, "mime_type", "") or "").lower():
        return "PDF"
    name = str(getattr(datei, "file_name", "") or "")
    endung = name.rsplit(".", 1)[1] if "." in name else ""
    if endung.isascii() and endung.isalnum() and 2 <= len(endung) <= 4:
        return endung.upper()
    return "Dokument"


@register.filter
def ergebnis(roh: Any) -> Ergebnis | None:
    """
    Ergebnis-Etikett zu einem Ergebnistext des Ratsinformationssystems (Issue #1260, ``services.ergebnis``), für
    ``<c-insight.ergebnis>``: ``{% with etikett=item.result|ergebnis %}``. ``None`` ohne Text.
    """
    return ergebnis_zuordnen(roh)


@register.simple_tag(takes_context=True)
def adresse_mit(context: Any, behalten: str = "", **werte: Any) -> str:
    """
    Adresse eines Reiters der Liste (Issue #1260): die Werte des Reiters und von der Anfrage nur die Parameter aus
    ``behalten`` (Komma-Liste, etwa der Suchbegriff), ohne leere Werte und ohne Seitenzahl. Fremde Parameter der
    Anfrage landen so nicht in den Links der Seite: ``{% adresse_mit behalten="q" period="past" %}`` → ``?q=Rad&period=past``.
    """
    request = context.get("request")
    params = QueryDict(mutable=True)
    if request is not None:
        for name in filter(None, (teil.strip() for teil in behalten.split(","))):
            if name in request.GET:
                params.setlist(name, request.GET.getlist(name))
    for name, wert in werte.items():
        params[name] = str(wert)
    return "?" + str(ohne_leere(params).urlencode())


@register.simple_tag(takes_context=True)
def seiten_url(context: Any, nummer: int) -> str:
    """Link auf Seite ``nummer`` einer Liste mit allen übrigen Parametern der Anfrage (Suche, Filter, Reiter)."""
    request = context.get("request")
    params = request.GET.copy() if request is not None else None
    if params is None:
        return f"?page={nummer}"
    params["page"] = str(nummer)
    return "?" + str(params.urlencode())


@register.simple_tag(takes_context=True)
def insight_url(context: Any, name: str) -> str:
    """
    Ziel einer Seite des Bürgerportals (URL-Name ohne Namensraum) für Navigation und Übersichten.

    Im Bürgerportal einer Körperschaft führen die Listen auf ihre Adresse je Kommune (``/insight/k/<slug>/vorgaenge/``,
    Issue #939), sonst wie ``{% url 'insight_core:insight:<name>' %}`` (``portal.PortalContext.url``).
    """
    portal = context.get("insight_portal")
    if portal is not None:
        return str(portal.url(name))
    return reverse(f"insight_core:insight:{name}")


@register.simple_tag(takes_context=True)
def insight_rel(context: Any, name: str) -> str:
    """
    ``rel`` zum Link aus ``insight_url``: ``nofollow``, wenn er über den Einstieg mit Pfad führt, der eine Sitzung
    anlegt (``portal.PortalContext.ueber_einstieg``); sonst leer. ``rel="{% insight_rel 'map' %}"`` an den Bausteinen.
    """
    portal = context.get("insight_portal")
    return str(portal.rel(name)) if portal is not None else ""


@register.simple_tag
def ris_url(links: Any, art: str, kennung: Any) -> str:
    """
    Ziel eines Eintrags in den gemeinsamen RIS-Bausteinen (Issue #853): über ``links`` (``insight_core.ris_links``),
    ohne Angabe die Detailseite in Insight. ``{% ris_url links "vorgang" paper.pk as ziel %}``.
    """
    return str((links or INSIGHT).url(art, kennung))
