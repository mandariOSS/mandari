# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Textbausteine als Befehle (Issue #85, Befehlsschicht #1588): anlegen, ändern, löschen.

Das kleine Beispiel des Musters aus ``apps.session.befehle``: Vertrag in ``hub/contracts/schemas/session.textblock.*``,
Fachfunktion hier, die View (``apps/session/views/textblocks.py``) liest nur das Formular. Recht: „Einstellungen
verwalten“. Textbausteine sind interne Formulierungshilfen des Mandanten, kein Teil des RIS-Bestands; deshalb gibt es
keine Ereignisse für die Drehscheibe, wohl aber Einträge im Prüfprotokoll.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from hub.commands import HandlerResult

from ..models import SessionTextBlock
from . import Kontext, befehl

ANLEGEN: Final = "session.textblock.create"
AENDERN: Final = "session.textblock.update"
LOESCHEN: Final = "session.textblock.delete"
RECHT: Final = "manage_settings"
#: Felder des Vertrags, die ein Textbaustein speichert
FELDER: Final = ("title", "content", "category", "order", "is_active")
UNBEKANNT: Final = "Der Textbaustein ist nicht bekannt."


@befehl(ANLEGEN, recht=RECHT)
def anlegen(kontext: Kontext) -> HandlerResult:
    """Textbaustein anlegen."""
    baustein = SessionTextBlock.objects.create(tenant=kontext.tenant, **_werte(kontext))
    kontext.protokoll("create", baustein, titel=baustein.title, kategorie=baustein.category)
    return kontext.quittung(baustein)


@befehl(AENDERN, recht=RECHT)
def aendern(kontext: Kontext) -> HandlerResult:
    """Textbaustein ändern; nur die genannten Felder ändern sich."""
    baustein = kontext.hole(SessionTextBlock, kontext.inhalt["textblock"], meldung=UNBEKANNT, sperren=True)
    werte = _werte(kontext)
    for feld, wert in werte.items():
        setattr(baustein, feld, wert)
    baustein.save()
    kontext.protokoll("update", baustein, titel=baustein.title, kategorie=baustein.category, felder=sorted(werte))
    return kontext.quittung(baustein)


@befehl(LOESCHEN, recht=RECHT)
def loeschen(kontext: Kontext) -> HandlerResult:
    """Textbaustein löschen."""
    baustein = kontext.hole(SessionTextBlock, kontext.inhalt["textblock"], meldung=UNBEKANNT, sperren=True)
    quittung = kontext.quittung(baustein)
    kontext.protokoll("delete", baustein)
    baustein.delete()
    return quittung


def _werte(kontext: Kontext) -> Mapping[str, Any]:
    """Felder des Inhalts für das Modell; Texte ohne Leerraum am Rand (nur Leerraum: 422)."""
    werte = {feld: kontext.inhalt[feld] for feld in FELDER if feld in kontext.inhalt}
    return {**werte, **kontext.texte("title", "content")}
