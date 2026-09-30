# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Namensregeln für Ereignisse und Befehle (``docs/adr/20260929-ereignisvertraege.md``).

- Aufbau ``<bereich>.<objekt>.<ereignis>``: Kleinbuchstaben und Ziffern, mehrteilige Wörter mit
  einfachem Unterstrich (``ris.resolution.implementation_changed``). Ist das Objekt der Bereich
  selbst, entfällt es (``submission.status_changed``, Befehl ``submission.submit``).
- Ereignisse stehen in der Vergangenheitsform (``ris.paper.released``), Befehle im Imperativ
  (``attendance.respond``). Geprüft wird das letzte Wort; die Heuristik kennt regelmäßige Formen
  auf ``-ed`` und die Ausnahmen in den Listen unten. Fehlt ein Verb, wird die Liste ergänzt.
- Die Version steht in der Hülle, nie im Namen (kein ``….v2``).
"""

from __future__ import annotations

import re
from typing import Final, Literal

Kind = Literal["event", "command"]
EVENT: Final = "event"
COMMAND: Final = "command"
KINDS: Final[frozenset[str]] = frozenset({EVENT, COMMAND})

#: Erlaubte Bereiche (erstes Namenssegment). ``ris`` spricht das kanonische RIS-Modell;
#: ``invitation`` trägt den Befehl ``invitation.acknowledge`` (``docs/adr/20260929-befehle-synchron.md``).
DOMAINS: Final[frozenset[str]] = frozenset(
    {"ris", "submission", "attendance", "invitation", "session", "work", "portal", "core"}
)

#: Interne Bereiche der Fachmodule: nur der genannte Eigentümer, nie öffentlich.
INTERNAL_DOMAINS: Final[dict[str, str]] = {
    "session": "apps.session",
    "work": "apps.work",
    "portal": "apps.portal",
}

#: Bereich ``core`` gehört der Plattform.
PLATFORM_PACKAGES: Final[tuple[str, ...]] = ("apps.accounts", "apps.tenants", "apps.common", "apps.events")

MAX_NAME_LENGTH: Final = 100

_SEGMENT = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
#: Muster eines gültigen Namens (zwei oder drei Segmente); steht gleichlautend in ``envelope/v1.json``.
NAME_PATTERN: Final = rf"^{_SEGMENT}(?:\.{_SEGMENT}){{1,2}}$"
_NAME_RE = re.compile(NAME_PATTERN)

#: Unregelmäßige Partizipien, die als Vergangenheitsform gelten.
IRREGULAR_PAST: Final[frozenset[str]] = frozenset(
    {
        "begun",
        "bought",
        "brought",
        "built",
        "chosen",
        "done",
        "drawn",
        "found",
        "frozen",
        "given",
        "held",
        "hidden",
        "kept",
        "left",
        "lost",
        "made",
        "met",
        "paid",
        "rebuilt",
        "rewritten",
        "sent",
        "shown",
        "sold",
        "spent",
        "taken",
        "told",
        "undone",
        "withdrawn",
        "won",
        "written",
    }
)
#: Verben mit gleicher Grund- und Vergangenheitsform: für Ereignisse und Befehle zulässig.
SAME_FORM: Final[frozenset[str]] = frozenset(
    {"broadcast", "cast", "cut", "put", "read", "reset", "set", "shut", "split", "spread", "upset"}
)
#: Grundformen auf ``-ed``, die keine Vergangenheit sind.
ED_BASE_FORMS: Final[frozenset[str]] = frozenset(
    {"embed", "exceed", "feed", "need", "proceed", "seed", "shed", "shred", "speed", "succeed"}
)


def last_word(name: str) -> str:
    """Letztes Wort des Namens, z. B. ``changed`` aus ``submission.status_changed``."""
    return name.rsplit(".", 1)[-1].rsplit("_", 1)[-1]


def is_past_tense(word: str) -> bool:
    if word in SAME_FORM or word in IRREGULAR_PAST:
        return True
    return word.endswith("ed") and word not in ED_BASE_FORMS


def is_imperative(word: str) -> bool:
    return word in SAME_FORM or not is_past_tense(word)


def domain_of(name: str) -> str:
    return name.split(".", 1)[0]


def check_name(name: object, kind: str) -> list[str]:
    """Verstöße eines Namens gegen die Namensregeln; leere Liste, wenn der Name gültig ist."""
    if not isinstance(name, str):
        return ["Name muss eine Zeichenkette sein"]
    rolle = "Ereignis" if kind == EVENT else "Befehl"
    if len(name) > MAX_NAME_LENGTH:
        return [f"Name ist länger als {MAX_NAME_LENGTH} Zeichen"]
    if not _NAME_RE.fullmatch(name):
        return [
            f"Name entspricht nicht <bereich>[.<objekt>].<{rolle.lower()}> "
            "(Kleinbuchstaben und Ziffern, Wörter mit einfachem Unterstrich, zwei oder drei Teile)"
        ]
    problems: list[str] = []
    domain = domain_of(name)
    if domain not in DOMAINS:
        problems.append(f"unbekannter Bereich „{domain}“ (erlaubt: {', '.join(sorted(DOMAINS))})")
    word = last_word(name)
    if kind == EVENT and not is_past_tense(word):
        problems.append(f"Ereignisse stehen in der Vergangenheitsform, „{word}“ ist keine (z. B. ris.paper.released)")
    elif kind == COMMAND and not is_imperative(word):
        problems.append(f"Befehle stehen im Imperativ, „{word}“ ist Vergangenheit (z. B. submission.submit)")
    return problems
