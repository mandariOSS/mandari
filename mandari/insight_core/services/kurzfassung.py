# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kurzfassung „Worum es geht“ (Issue #1271): der erste Absatz der KI-Zusammenfassung eines Vorgangs.

Der Prompt verlangt ab Fassung 2 (``insight_ai.services.prompts.PROMPT_FASSUNG``) als ersten Absatz zwei bis vier
Sätze, die für sich verständlich sind. Ältere Zusammenfassungen ohne Absätze teilt ``absaetze`` nach Sätzen. Die
Vorgangsseite zeigt den ersten Absatz oben unter „Worum es geht“, den Rest nach „Weiterlesen“; Vorgangsliste und
Suchtreffer zeigen ihn als Vorschauzeile statt Dateigröße und Textausschnitt.

Eine veraltete Zusammenfassung (``summary_store.veraltet``: sie kennt den aktuellen Beratungsstand nicht, Issue #1257)
erscheint in Listen nicht: Dort fehlt der Hinweis, den die Vorgangsseite darüber setzt, und die Zeile könnte dem
Ergebnis-Etikett daneben widersprechen.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Final

#: Ab so vielen Wörtern wird ein Block ohne Absätze für die Anzeige geteilt
LANGER_BLOCK_WOERTER: Final = 80
#: Sätze je Absatz beim Teilen
SAETZE_JE_ABSATZ: Final = 3
#: Längste Vorschauzeile (Zeichen); die Liste kürzt sichtbar auf zwei bzw. drei Zeilen
VORSCHAU_ZEICHEN: Final = 360
#: Satzende: Punkt, Ausrufe- oder Fragezeichen, dann Leerraum und ein Großbuchstabe; nicht nach Abkürzungen
_SATZENDE: Final = re.compile(r"(?<=[.!?])(?<!\bNr\.)(?<!\bz\.)(?<!\bB\.)(?<!\bca\.)(?<!\bvgl\.)\s+(?=[A-ZÄÖÜ„])")


def absaetze(text: str | None) -> list[str]:
    """
    Absätze einer Zusammenfassung: getrennt an Leerzeilen, sonst an einzelnen Zeilenumbrüchen; Leerraum zusammengefasst.
    Ältere Zusammenfassungen kamen oft nur mit einfachen Zeilenumbrüchen oder als ein langer Block.
    """
    roh = (text or "").replace("\r\n", "\n").strip()
    if not roh:
        return []
    teile = re.split(r"\n\s*\n", roh) if re.search(r"\n\s*\n", roh) else roh.split("\n")
    gefunden = [" ".join(teil.split()) for teil in teile if teil.strip()]
    if len(gefunden) == 1 and len(gefunden[0].split()) > LANGER_BLOCK_WOERTER:
        # Ein langer Block ohne Absätze (das Modell hielt sich nicht an die Vorgabe): für die Anzeige nach je
        # SAETZE_JE_ABSATZ Sätzen teilen, damit der Kern vorn steht und der Rest einklappbar ist
        saetze = _SATZENDE.split(gefunden[0])
        gefunden = [" ".join(saetze[i : i + SAETZE_JE_ABSATZ]) for i in range(0, len(saetze), SAETZE_JE_ABSATZ)]
    return gefunden


def kurzfassung(text: str | None) -> str:
    """Erster Absatz der Zusammenfassung, höchstens ``VORSCHAU_ZEICHEN`` Zeichen (am Wortende gekürzt, mit „…“)."""
    teile = absaetze(text)
    if not teile:
        return ""
    erster = teile[0]
    if len(erster) <= VORSCHAU_ZEICHEN:
        return erster
    return erster[: VORSCHAU_ZEICHEN - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def vorschauen(vorgaenge: Iterable[Any], *, now: datetime | None = None) -> dict[str, str]:
    """
    Vorschauzeile je Vorgang (Kennung als Text → Kurzfassung) für Objekte mit ``summary`` und ``summary_stand``; nur
    Zusammenfassungen, die zum aktuellen Beratungsstand passen. Eine Abfrage, und nur, wenn eine Zusammenfassung da ist.
    """
    from . import summary_store

    mit_text = {str(v.pk): v for v in vorgaenge if (getattr(v, "summary", None) or "").strip()}
    if not mit_text:
        return {}
    staende = {str(k): stand for k, stand in summary_store.beratungsstaende(list(mit_text), now=now).items()}
    return {
        kennung: kurz
        for kennung, vorgang in mit_text.items()
        if (vorgang.summary_stand or "") == staende.get(kennung, "") and (kurz := kurzfassung(vorgang.summary))
    }


def anhaengen(vorgaenge: list[Any]) -> None:
    """Setzt ``vorschau`` an jedem Vorgang der Liste (leer ohne passende Zusammenfassung)."""
    gefunden = vorschauen(vorgaenge)
    for vorgang in vorgaenge:
        vorgang.vorschau = gefunden.get(str(vorgang.pk), "")


def an_treffer(treffer: list[dict[str, Any]]) -> None:
    """
    Setzt ``vorschau`` an den Vorgangstreffern der Suche (``search_presentation.present_groups``): zwei Abfragen für
    die ganze Seite, keine ohne Vorgangstreffer.
    """
    from ..models import OParlPaper

    kennungen = [t["pk"] for t in treffer if t.get("kind") == "vorgang" and _ist_uuid(t.get("pk"))]
    if not kennungen:
        return
    vorgaenge = OParlPaper.objects.filter(pk__in=kennungen, summary__isnull=False).only(
        "pk", "summary", "summary_stand"
    )
    gefunden = vorschauen(vorgaenge)
    for eintrag in treffer:
        if eintrag.get("kind") == "vorgang":
            eintrag["vorschau"] = gefunden.get(str(eintrag.get("pk")), "")


def _ist_uuid(wert: Any) -> bool:
    try:
        uuid.UUID(str(wert))
    except ValueError:
        return False
    return True
