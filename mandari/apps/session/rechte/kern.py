# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fachfreier Kern der Rechte mit Geltungsbereich (Issue #772, ADR „Rechte mit Geltungsbereich“).

Kennt weder Session-Modelle noch den Rechtekatalog: Bereiche sind Paare aus Art und Kennung, Bäume ordnen jedem
Bereich die Bereiche darunter zu, eine Zuweisung gibt eine Menge von Rechten für einen Bereich und einen Zeitraum.
Daraus entsteht der **Zugriffskontext**: je Recht „mandantenweit“ oder die Menge der Bereiche, in denen es gilt –
einmal je Anfrage berechnet, damit Prüfung und Listenfilter keine Abfrage je Objekt brauchen.

Wird in die Plattform gehoben, sobald Work dasselbe Modell nutzt; deshalb keine Importe aus ``apps.session``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Final

#: Art des Geltungsbereichs „ganzer Mandant“ (ohne Kennung)
MANDANT: Final = "mandant"


@dataclass(frozen=True, order=True)
class Bereich:
    """Ein Geltungsbereich: Art (``koerperschaft``, ``gremium``, ``amt`` …) und Kennung des Objekts."""

    art: str
    kennung: str


class Baum:
    """
    Bäume der Geltungsbereiche eines Mandanten: je Bereich seine direkten Unterbereiche.

    Nur bekannte Bereiche wirken; eine Zuweisung auf einen unbekannten oder gelöschten Bereich gewährt nichts.
    Zyklen in den Daten (fehlerhaftes ``parent``) führen nicht zu Endlosschleifen.
    """

    def __init__(self, kinder: Mapping[Bereich, Iterable[Bereich]]) -> None:
        self._kinder: dict[Bereich, tuple[Bereich, ...]] = {b: tuple(k) for b, k in kinder.items()}
        self._bekannt: set[Bereich] = set(self._kinder)
        for unter in self._kinder.values():
            self._bekannt.update(unter)

    def kennt(self, bereich: Bereich) -> bool:
        return bereich in self._bekannt

    def teilbaum(self, bereich: Bereich) -> frozenset[Bereich]:
        """Der Bereich selbst und alle Bereiche darunter; leer für unbekannte Bereiche."""
        if bereich not in self._bekannt:
            return frozenset()
        gefunden = {bereich}
        offen = [bereich]
        while offen:
            for kind in self._kinder.get(offen.pop(), ()):
                if kind not in gefunden:
                    gefunden.add(kind)
                    offen.append(kind)
        return frozenset(gefunden)


@dataclass(frozen=True)
class Zuweisung:
    """Rechte für einen Bereich (``None`` = ganzer Mandant) und einen Zeitraum (Grenzen einschließlich, leer = offen)."""

    rechte: frozenset[str]
    bereich: Bereich | None = None
    gueltig_von: date | None = None
    gueltig_bis: date | None = None

    def wirkt_am(self, tag: date) -> bool:
        if self.gueltig_von is not None and tag < self.gueltig_von:
            return False
        return self.gueltig_bis is None or tag <= self.gueltig_bis


@dataclass(frozen=True)
class Zugriffskontext:
    """Je Recht: mandantenweit oder die Bereiche (Teilbäume aufgelöst), in denen es gilt."""

    mandantenweit: frozenset[str] = frozenset()
    bereiche: Mapping[str, frozenset[Bereich]] = field(default_factory=dict)

    def gilt_mandantenweit(self, recht: str) -> bool:
        return recht in self.mandantenweit

    def bereiche_fuer(self, recht: str, art: str | None = None) -> frozenset[Bereich]:
        """Bereiche, in denen ``recht`` gilt (ohne mandantenweite Geltung), wahlweise nur einer Art."""
        alle = self.bereiche.get(recht, frozenset())
        if art is None:
            return alle
        return frozenset(b for b in alle if b.art == art)

    def kennungen(self, recht: str, art: str) -> frozenset[str]:
        """Kennungen der Bereiche einer Art, in denen ``recht`` gilt – Grundlage der Listenfilter."""
        return frozenset(b.kennung for b in self.bereiche_fuer(recht, art))

    def gilt_in(self, recht: str, bereiche: Iterable[Bereich]) -> bool:
        """Gilt ``recht`` für ein Objekt mit diesen Geltungsbereichen?"""
        if recht in self.mandantenweit:
            return True
        eigene = self.bereiche.get(recht, frozenset())
        return any(b in eigene for b in bereiche)


def mandantenweite_rechte(zuweisungen: Iterable[Zuweisung], tag: date) -> frozenset[str]:
    """Rechte aus allen am ``tag`` wirksamen Zuweisungen für den ganzen Mandanten."""
    rechte: set[str] = set()
    for zuweisung in zuweisungen:
        if zuweisung.bereich is None and zuweisung.wirkt_am(tag):
            rechte |= zuweisung.rechte
    return frozenset(rechte)


def zugriffskontext(zuweisungen: Iterable[Zuweisung], tag: date, baum: Baum) -> Zugriffskontext:
    """Zugriffskontext aus den am ``tag`` wirksamen Zuweisungen; Bereiche werden über ``baum`` aufgelöst."""
    liste = [z for z in zuweisungen if z.wirkt_am(tag)]
    mandantenweit = mandantenweite_rechte(liste, tag)
    bereiche: dict[str, set[Bereich]] = {}
    for zuweisung in liste:
        if zuweisung.bereich is None:
            continue
        teilbaum = baum.teilbaum(zuweisung.bereich)
        if not teilbaum:
            continue
        for recht in zuweisung.rechte - mandantenweit:
            bereiche.setdefault(recht, set()).update(teilbaum)
    return Zugriffskontext(
        mandantenweit=mandantenweit,
        bereiche={recht: frozenset(menge) for recht, menge in bereiche.items()},
    )
