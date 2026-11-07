# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzverteilung eines Gremiums nach Fraktionen (Issue #1272): ein Balken aus den laufenden Mitgliedschaften.

Die Quellen nennen selten, für welche Fraktion jemand in einem Gremium sitzt (OParl ``onBehalfOf`` fehlt meist). Die
Fraktion ist deshalb **abgeleitet**: die laufende Fraktion der Person wie in der Personenliste
(``personen_liste.angaben_fuer``: Mitgliedschaft in einer Fraktion laut RIS, sonst die bestätigte Zuordnung in Insight
oder die gepflegte Liste). Die Seite sagt das unter dem Balken.

**Was zählt (bewusst eng, nach den Rollen der Quelle):**

- Ein Sitz ist eine laufende Mitgliedschaft mit Stimme, je Person einmal.
- Stellvertretungen („Stellv. Mitglied“, „Vertreter/in“) werden nur gezählt, nicht als Sitz; der stellvertretende
  Vorsitz dagegen ist ein Sitz.
- Beratende Mitglieder („beratendes Mitglied“, „ohne Stimmrecht“) werden nur gezählt.
- Verwaltung, Schriftführung, Gäste und ähnliche Rollen sind kein Sitz und werden nicht gezählt.
- Sitze ohne erkennbare Fraktion (oft sachkundige Bürgerinnen und Bürger) stehen als „ohne Fraktionsangabe“ am Ende.

Ein Balken erscheint nur, wenn er etwas sagt: mindestens drei Sitze, davon mindestens die Hälfte mit Fraktion, und
nie auf der Seite einer Fraktion selbst. Reihenfolge nach Sitzen, bei Gleichstand nach dem Kürzel, ohne Wertung.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from .fraktionskuerzel import GRAU, Kuerzel, kuerzel_der_fraktion

_I = re.IGNORECASE

#: Kein Sitz und nicht gezählt
_KEIN_SITZ: Final = re.compile(
    r"schriftf(?:ü|ue)hr|protokoll|verwaltung|gesch(?:ä|ae)ftsf(?:ü|ue)hr|(?<![\w])g(?:a|ä)st(?:e)?(?![\w])|"
    r"beigeordnet|dezernent|sachbearbeit|mitarbeiter|referent|amtsleit|fachbereichsleit|sitzungsdienst",
    _I,
)
#: Vorsitz und Bürgermeister sind Sitze, auch als Stellvertretung („Stellv. Vorsitz“)
_VORSITZ: Final = re.compile(r"vorsitz|bürgermeister|buergermeister", _I)
#: Stellvertretungen: nur gezählt
_STELLVERTRETUNG: Final = re.compile(
    r"^\s*(?:\d\.\s*)?(?:stellv(?:\.|ertretende[rs]?)?\s*(?:beratende[rs]?\s+|sachkundige[rs]?\s+|sachk\.\s*)?"
    r"(?:mitglied|ausschussmitglied|ratsmitglied|bezirksvertreter|bezirksverordnete|bürger|vertreter)"
    r"|(?:persönliche[rs]?\s+)?(?:stell)?vertreter(?:in|/in|\(in\)|innen)?(?![\w])|vertretung(?![\w]))",
    _I,
)
#: Beratende Mitglieder: nur gezählt
_BERATEND: Final = re.compile(r"beratend|ohne\s+stimm|o\.\s*stimmr|nicht\s+stimmberechtigt", _I)

#: Mindestzahl der Sitze für einen Balken
SITZE_MIN: Final = 3
#: Farbe des Teils ohne Fraktionsangabe kommt aus Klassen der Vorlage (hell und dunkel), nicht aus der Tabelle
OHNE_ANGABE: Final = "ohne Fraktionsangabe"

SITZ, BERATEND, STELLVERTRETEND, KEINER = "sitz", "beratend", "stellvertretend", ""


def rolle_einordnen(rolle: str | None) -> str:
    """Sitz, beratend, stellvertretend oder keiner (leer) für eine Rolle der Quelle."""
    text = (rolle or "").strip()
    if _KEIN_SITZ.search(text):
        return KEINER
    if _VORSITZ.search(text):
        return SITZ
    if _STELLVERTRETUNG.search(text):
        return STELLVERTRETEND
    if _BERATEND.search(text):
        return BERATEND
    return SITZ


@dataclass(frozen=True)
class Anteil:
    """Sitze einer Fraktion; ``kuerzel`` ist ``None`` für „ohne Fraktionsangabe“."""

    kuerzel: Kuerzel | None
    sitze: int
    #: Beginn im Balken (Sitze davor)
    x: int = 0

    @property
    def text(self) -> str:
        return self.kuerzel.text if self.kuerzel else OHNE_ANGABE

    @property
    def farbe(self) -> str:
        return self.kuerzel.farbe if self.kuerzel else ""

    @property
    def farbe_dunkel(self) -> str:
        return self.kuerzel.farbe_dunkel if self.kuerzel else ""

    @property
    def titel(self) -> str:
        """Tooltip im Balken: „SPD-Fraktion: 6 Sitze“."""
        name = (self.kuerzel.titel or self.kuerzel.text) if self.kuerzel else OHNE_ANGABE
        return f"{name}: {self.sitze} {'Sitz' if self.sitze == 1 else 'Sitze'}"


@dataclass(frozen=True)
class Sitzverteilung:
    anteile: tuple[Anteil, ...]
    sitze: int
    stellvertretungen: int = 0
    beratend: int = 0

    @property
    def grenzen(self) -> tuple[int, ...]:
        """Stellen zwischen zwei Anteilen (für die Lücke im Balken)."""
        return tuple(a.x for a in self.anteile[1:])


def _kuerzel(fraktion: Any) -> Kuerzel | None:
    """Kürzel einer Fraktion aus ``PersonAngaben``: Gremium im RIS oder gepflegte Zuordnung."""
    if fraktion is None:
        return None
    name = getattr(fraktion, "name", "") or ""
    kurz = getattr(fraktion, "short_name", "") or ""
    bezeichnung = getattr(fraktion, "bezeichnung", "") or ""
    # PersonFraktion: nur „bezeichnung“; GepflegteFraktion: „bezeichnung“ (für Leser) und „name“
    if bezeichnung and not kurz:
        kurz, name = (bezeichnung, name or bezeichnung)
    kuerzel = kuerzel_der_fraktion(name or kurz, kurz)
    if kuerzel is None and (kurz or name):
        kuerzel = Kuerzel(kurz or name, GRAU, name or kurz)
    return kuerzel


def berechnen(mitgliedschaften: Iterable[Any], fraktion_je_person: Mapping[Any, Any]) -> Sitzverteilung | None:
    """
    Sitzverteilung aus laufenden Mitgliedschaften (``person_id``, ``role``) und der Fraktion je Person
    (``PersonAngaben.fraktion`` bzw. ``fraktion_lokal``, ``None`` ohne). ``None``, wenn der Balken nichts sagt.
    """
    rang = {SITZ: 3, BERATEND: 2, STELLVERTRETEND: 1, KEINER: 0}
    je_person: dict[Any, str] = {}
    for m in mitgliedschaften:
        art = rolle_einordnen(getattr(m, "role", ""))
        je_person[m.person_id] = max(art, je_person.get(m.person_id, KEINER), key=rang.__getitem__)
    sitze = [pid for pid, art in je_person.items() if art == SITZ]
    gruppen: dict[str, tuple[Kuerzel, int]] = {}
    ohne = 0
    for pid in sitze:
        kuerzel = _kuerzel(fraktion_je_person.get(pid))
        if kuerzel is None:
            ohne += 1
            continue
        schluessel = kuerzel.text.casefold()
        bisher_kuerzel, n = gruppen.get(schluessel, (kuerzel, 0))
        gruppen[schluessel] = (bisher_kuerzel, n + 1)
    mit = len(sitze) - ohne
    if len(sitze) < SITZE_MIN or not gruppen or mit * 2 < len(sitze):
        return None
    reihe = sorted(gruppen.values(), key=lambda g: (-g[1], g[0].text.casefold()))
    anteile: list[Anteil] = []
    x = 0
    for kuerzel, n in reihe:
        anteile.append(Anteil(kuerzel, n, x))
        x += n
    if ohne:
        anteile.append(Anteil(None, ohne, x))
    return Sitzverteilung(
        anteile=tuple(anteile),
        sitze=len(sitze),
        stellvertretungen=sum(1 for art in je_person.values() if art == STELLVERTRETEND),
        beratend=sum(1 for art in je_person.values() if art == BERATEND),
    )
