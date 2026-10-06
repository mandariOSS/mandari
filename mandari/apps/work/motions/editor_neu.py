# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kontext des neuen Antragseditors (Teil von #856): Ablauf, Vorauswahl für die Abstimmung und die festen Einträge der
Menüs und der Werkzeugleiste (``templates/work/motions/editor_neu.html`` und ``partials/neu/``).

Der bisherige Editor braucht nichts davon; die Daten (Kommentare, Freigaben, Status) sind dieselben.
"""

from __future__ import annotations

from typing import Any

from . import ablauf, vorschlaege

#: Menüleiste in der Kopfzeile (Schlüssel, Beschriftung)
MENUE_LEISTE: tuple[tuple[str, str], ...] = (
    ("datei", "Datei"),
    ("bearbeiten", "Bearbeiten"),
    ("ansicht", "Ansicht"),
    ("einfuegen", "Einfügen"),
    ("format", "Format"),
    ("ablauf", "Ablauf"),
)
#: Absatzformate (Wert für documentEditor.absatzformat, Beschriftung)
ABSATZFORMATE: tuple[tuple[str, str], ...] = (
    ("p", "Text"),
    ("h1", "Überschrift 1"),
    ("h2", "Überschrift 2"),
    ("h3", "Überschrift 3"),
    ("quote", "Zitat"),
)
#: Zeichenformate (Befehl, Beschriftung, Symbol, Tastenkürzel); die ersten drei stehen in der Werkzeugleiste
ZEICHENFORMATE: tuple[tuple[str, str, str, str], ...] = (
    ("bold", "Fett", "bold", "Strg+B"),
    ("italic", "Kursiv", "italic", "Strg+I"),
    ("underline", "Unterstrichen", "underline", "Strg+U"),
    ("strike", "Durchgestrichen", "strikethrough", "Strg+Umschalt+S"),
    ("highlight", "Hervorheben", "highlighter", "Strg+Umschalt+H"),
)
#: Textfarben wie im bisherigen Editor (Farbwert, Name für Bildschirmleser)
TEXTFARBEN: tuple[tuple[str, str], ...] = (
    ("#dc2626", "Rot"),
    ("#ea580c", "Orange"),
    ("#d97706", "Bernstein"),
    ("#16a34a", "Grün"),
    ("#0891b2", "Cyan"),
    ("#2563eb", "Blau"),
    ("#7c3aed", "Violett"),
    ("#db2777", "Pink"),
    ("#059669", "Smaragd"),
    ("#0d9488", "Türkis"),
    ("#4f46e5", "Indigo"),
)
#: Ausrichtung (Wert, Beschriftung, Symbol)
AUSRICHTUNGEN: tuple[tuple[str, str, str], ...] = (
    ("left", "Linksbündig", "align-left"),
    ("center", "Zentriert", "align-center"),
    ("right", "Rechtsbündig", "align-right"),
    ("justify", "Blocksatz", "align-justify"),
)
#: Vorauswahl im Dialog „Zur Abstimmung geben“: angefragt als Ratsmitglied (MotionApproval.APPROVAL_TYPE_CHOICES)
ABSTIMMUNG_ART = "council"


def kontext(
    *,
    motion: Any,
    membership: Any,
    organization: Any,
    comments: Any,
    approvals: Any,
    darf_steuern: bool,
) -> dict[str, Any]:
    """Zusätzlicher Kontext des neuen Editors."""
    offene_kommentare, offene_vorschlaege = vorschlaege.offene_zahlen(comments)
    stand = ablauf.ablauf_fuer(
        motion,
        approvals=approvals,
        offene_kommentare=offene_kommentare,
        offene_vorschlaege=offene_vorschlaege,
        darf_steuern=darf_steuern,
    )
    stimmberechtigt: set[Any] = set()
    if stand.aktion == "abstimmung":
        stimmberechtigt = ablauf.stimmberechtigte_ids(organization)
        stimmberechtigt.discard(membership.id)
    return {
        "ablauf": stand,
        "darf_steuern": darf_steuern,
        "offene_kommentare": offene_kommentare,
        "offene_vorschlaege": offene_vorschlaege,
        # „Noch offen: 2 Kommentare, 1 Vorschlag“ für die Dialoge des Ablaufs (leer = nichts offen)
        "offen_text": ablauf.offen_text(offene_kommentare, offene_vorschlaege),
        "stimmberechtigte_ids": stimmberechtigt,
        # Vorauswahl als Text (Mitgliedschaften haben UUIDs; das Formular vergleicht Zeichenketten)
        "abstimmung_vorauswahl": sorted(str(member_id) for member_id in stimmberechtigt),
        "abstimmung_art": ABSTIMMUNG_ART,
        "menue_leiste": MENUE_LEISTE,
        "absatzformate": ABSATZFORMATE,
        "zeichenformate": ZEICHENFORMATE,
        "textfarben": TEXTFARBEN,
        "ausrichtungen": AUSRICHTUNGEN,
    }
