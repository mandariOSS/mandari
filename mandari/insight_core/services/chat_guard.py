# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Belegung vor dem Anbieteraufruf des KI-Assistenten im Bürgerportal.

Nutzungsgrenze je Besucher und Tagesobergrenze zählen gebuchte Antworten (``ChatUsage``). Gebucht wird erst nach der
Antwort, und die dauert bis zu ``INSIGHT_CHAT_TIME_LIMIT_SECONDS``. Anfragen, die in dieser Zeit eintreffen, sähen
ohne Belegung alle denselben alten Stand. Deshalb belegt jede Anfrage vor dem Aufruf, jeweils mit ``cache.add`` in
einem Schritt:

- **je Besucher** eine laufende Antwort (``besucher_sperren``): Sitzung, Adresse und Konto. Die Sperre gilt von vor
  der Prüfung der Nutzungsgrenze bis nach dem Buchen; überlappende Anfragen desselben Besuchers laufen damit
  nacheinander durch die Zählung.
- **einen Platz** (``platz_belegen``): Höchstens ``INSIGHT_CHAT_MAX_CONCURRENT`` Antworten laufen zugleich. Die
  Tagesobergrenze rechnet jede andere laufende Antwort mit ihrem höchsten möglichen Verbrauch ein (``vorbelegt``),
  bevor eine weitere beginnt.

Sperre und Platz verfallen von selbst (Zeitlimit der Antwort plus Reserve), falls ein Prozess mitten in der Antwort
endet. Der Cache ist dabei nur die Belegung laufender Antworten; der gebuchte Verbrauch steht weiter in der
Datenbank und bleibt die maßgebliche Summe.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from .. import throttle

#: So viel länger als das Zeitlimit der Antwort gelten Sperre und Platz (Werkzeuge, Buchen)
RESERVE_SECONDS = 60
#: Geschätzter Verbrauch eines Modellaufrufs (Verlauf, Werkzeugergebnisse und Antwort)
TOKEN_JE_AUFRUF = 12_000


def _dauer() -> int:
    try:
        zeitlimit = int(getattr(settings, "INSIGHT_CHAT_TIME_LIMIT_SECONDS", 90))
    except (TypeError, ValueError):
        zeitlimit = 90
    return max(zeitlimit, 45) + RESERVE_SECONDS


def _aufrufe_je_antwort() -> int:
    """Höchstens so viele Modellaufrufe braucht eine Antwort: die Werkzeugrunden und die Schlussrunde."""
    try:
        runden = int(getattr(settings, "INSIGHT_CHAT_MAX_TOOL_ROUNDS", 4))
    except (TypeError, ValueError):
        runden = 4
    return max(1, runden) + 1


def besucher_sperren(kennungen: Iterable[str]) -> list[str] | None:
    """
    Je Kennung (Sitzung, Adresse, Konto) höchstens eine laufende Antwort.

    Rückgabe: die belegten Schlüssel für ``freigeben``; ``None``, wenn für eine der Kennungen schon eine Antwort
    läuft (dann ist nichts belegt). Kennungen landen nur als Hash im Cache.
    """
    belegt: list[str] = []
    for kennung in kennungen:
        schluessel = "insight:chat:laeuft:" + hashlib.sha256(kennung.encode()).hexdigest()[:32]
        if not cache.add(schluessel, 1, timeout=_dauer()):
            freigeben(belegt)
            return None
        belegt.append(schluessel)
    return belegt


def freigeben(schluessel: Iterable[str]) -> None:
    cache.delete_many(list(schluessel))


@dataclass(frozen=True)
class Platz:
    """Belegter Platz einer laufenden Antwort und der Verbrauch, den die anderen laufenden höchstens haben."""

    schluessel: str
    #: Modellaufrufe und Token, die die anderen laufenden Antworten höchstens noch buchen
    aufrufe_vorbelegt: int
    token_vorbelegt: int


def platz_belegen() -> Platz | None:
    """
    Einen Platz für eine laufende Antwort belegen; ``None``, wenn alle belegt sind.

    Erst belegen, dann zählen: Zwei Anfragen, die zugleich eintreffen, sehen dadurch mindestens die jeweils andere.
    Im ungünstigsten Fall weist die Tagesobergrenze dann beide ab, nie lässt sie beide über die Grenze.
    """
    plaetze = [
        f"insight:chat:platz:{nummer}" for nummer in range(max(1, throttle.setting("INSIGHT_CHAT_MAX_CONCURRENT")))
    ]
    eigener = next((platz for platz in plaetze if cache.add(platz, 1, timeout=_dauer())), None)
    if eigener is None:
        return None
    andere = max(0, len(cache.get_many(plaetze)) - 1)
    aufrufe = andere * _aufrufe_je_antwort()
    return Platz(eigener, aufrufe, aufrufe * TOKEN_JE_AUFRUF)
