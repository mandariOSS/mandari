# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gelesene Fraktionen auf bekannte Bezeichnungen abbilden (Issue #915).

Die Texterkennung liest dieselbe Fraktion unterschiedlich: Umlaute fehlen oder sind verstümmelt („Südliste“,
„Sudliste“, „Su�dliste“), lange Bezeichnungen sind abgeschnitten („Internationale Fraktion Beisp…“). Ohne Abgleich
entstünden Dubletten, und die Entprellung hielte jede Lesart für einen Personenwechsel.

**Bekannte Bezeichnungen einer Kommune** (``bekannte_fraktionen``): zuerst die des Profils (``fraktionen``), dann die
bisher gelesenen (Wortmeldungen aller Übertragungen der Kommune, häufigste zuerst). Eine gelesene Bezeichnung, die
nur der Anfang einer anderen ist, zählt nicht (sie war abgeschnitten).

**Abgleich** (``kanonisch``) in der Vergleichsform (``lesung.vereinfacht``: klein, ohne Umlautzeichen), Wort für
Wort: Wörter ab ``MIN_WORT`` Zeichen dürfen sich ähneln (``AEHNLICH``, Lesefehler wie „Suüdliste“ statt „Südliste“),
kürzere Wörter wie Parteikürzel müssen genau gleich sein („Fraktion FDP“ ist nie „Fraktion SPD“).

1. gleich → die bekannte Bezeichnung (hat die Lesung mehr Umlaute, die Lesung: „Sudliste“ war verlesen);
2. eine bekannte ist der Anfang der Lesung (ganze Wörter) → die Lesung (vollständiger als die bekannte);
3. gleich bis auf Lesefehler → die ähnlichste bekannte;
4. die Lesung (mindestens ``MIN_PRAEFIX`` Zeichen) ist der Anfang genau einer bekannten, das letzte Wort darf
   abgeschnitten sein, die übrigen dürfen Lesefehler haben → die bekannte (abgeschnitten);
5. sonst die Lesung selbst.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

from django.db.models import Count

from .lesung import aehnlichkeit, vereinfacht

#: Ab dieser Ähnlichkeit gelten zwei Wörter als dasselbe Wort mit Lesefehlern
AEHNLICH: Final = 0.8
#: Kürzere Wörter (Kürzel, Buchstaben) müssen genau gleich sein
MIN_WORT: Final = 5
#: Kürzeste Lesung (Vergleichsform), die als abgeschnittene Bezeichnung gilt: abgeschnitten werden nur lange
MIN_PRAEFIX: Final = 12
#: So viele gelesene Bezeichnungen je Kommune werden höchstens berücksichtigt
MAX_BEKANNTE: Final = 100
_ABGESCHNITTEN: Final = " .…-/"


def _form(text: str) -> str:
    return vereinfacht(text.rstrip(_ABGESCHNITTEN))


def _wort_gleich(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= MIN_WORT and aehnlichkeit(a, b) >= AEHNLICH)


def gleich_bis_auf_lesefehler(a: str, b: str) -> bool:
    """Gleich viele Wörter, jedes gleich oder (ab ``MIN_WORT`` Zeichen) ähnlich (Vergleichsformen)."""
    woerter_a, woerter_b = a.split(), b.split()
    return len(woerter_a) == len(woerter_b) and all(
        _wort_gleich(x, y) for x, y in zip(woerter_a, woerter_b, strict=True)
    )


def _anfang(gelesen: str, bekannt: str) -> bool:
    """Ist ``gelesen`` der Anfang von ``bekannt``? Das letzte Wort darf abgeschnitten, die übrigen verlesen sein."""
    woerter, ziel = gelesen.split(), bekannt.split()
    if not woerter or len(woerter) > len(ziel):
        return False
    *vorne, letztes = woerter
    if not all(_wort_gleich(x, y) for x, y in zip(vorne, ziel, strict=False)):
        return False
    gegenueber = ziel[len(woerter) - 1]
    return gegenueber.startswith(letztes) or _wort_gleich(letztes, gegenueber[: len(letztes)])


def kanonisch(gelesen: str | None, bekannte: Sequence[str]) -> str | None:
    """Gelesene Fraktion als bekannte Bezeichnung (siehe Moduldokumentation)."""
    if not gelesen:
        return gelesen
    form = _form(gelesen)
    if not form:
        return gelesen
    formen = [(b, _form(b)) for b in bekannte if b and _form(b)]
    for bekannt, bekannt_form in formen:
        if bekannt_form == form:
            # Gleiche Bezeichnung: die Schreibweise mit Umlauten gilt („Sudliste“ war verlesen)
            return gelesen if _umlaute(gelesen) > _umlaute(bekannt) else bekannt
    if any(form.startswith(f + " ") for _, f in formen):
        return gelesen
    # Bei gleicher Ähnlichkeit gewinnt die zuerst genannte (Profil vor Lesungen, häufige vor seltenen)
    aehnliche = [
        (aehnlichkeit(form, f), -nummer, b)
        for nummer, (b, f) in enumerate(formen)
        if gleich_bis_auf_lesefehler(form, f)
    ]
    if aehnliche:
        return max(aehnliche)[2]
    if len(form) >= MIN_PRAEFIX:
        abgeschnitten: dict[str, str] = {}
        for bekannt, bekannt_form in formen:
            if len(bekannt_form) > len(form) and _anfang(form, bekannt_form):
                abgeschnitten.setdefault(bekannt_form, bekannt)
        if len(abgeschnitten) == 1:
            return next(iter(abgeschnitten.values()))
    return gelesen


def ohne_abgeschnittene(feste: Sequence[str], gelesene: Sequence[str]) -> list[str]:
    """
    Erst die festen Bezeichnungen (Profil), dann die gelesenen ohne Doppelte und ohne die, deren Vergleichsform nur
    der Anfang einer anderen ist (abgeschnitten gelesen). Reihenfolge bleibt.
    """
    alle = [_form(b) for b in [*feste, *gelesene] if b and _form(b)]
    ergebnis: list[str] = []
    #: Vergleichsform → (Stelle im Ergebnis, aus dem Profil?)
    gesehen: dict[str, tuple[int, bool]] = {}
    for nummer, bezeichnung in enumerate([*feste, *gelesene]):
        form = _form(bezeichnung) if bezeichnung else ""
        if not form:
            continue
        fest = nummer < len(feste)
        if form in gesehen:
            stelle, war_fest = gesehen[form]
            # Gleiche Bezeichnung, einmal ohne Umlaute gelesen: die Schreibweise mit Umlauten gilt
            if not war_fest and not fest and _umlaute(bezeichnung) > _umlaute(ergebnis[stelle]):
                ergebnis[stelle] = bezeichnung
            continue
        if not fest and any(anders != form and anders.startswith(form) for anders in alle):
            continue
        gesehen[form] = (len(ergebnis), fest)
        ergebnis.append(bezeichnung)
    return ergebnis


def _umlaute(text: str) -> int:
    return sum(1 for zeichen in text if zeichen in "ÄÖÜäöüß")


def bekannte_fraktionen(body_id: uuid.UUID | None, profil_fraktionen: Sequence[str] = ()) -> list[str]:
    """Bekannte Bezeichnungen der Kommune: Profil, dann bisher gelesene (häufigste zuerst)."""
    from .models import BroadcastSpeech

    gelesen: list[str] = []
    if body_id is not None:
        gelesen = list(
            BroadcastSpeech.objects.filter(broadcast__source__body_id=body_id)
            .exclude(faction_read="")
            .values("faction_read")
            .annotate(anzahl=Count("id"))
            .order_by("-anzahl", "faction_read")
            .values_list("faction_read", flat=True)[:MAX_BEKANNTE]
        )
    return ohne_abgeschnittene(profil_fraktionen, gelesen)
