# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnements im Code: ``@subscriber`` registriert einen Handler unter einem Namen.

    from apps.events import Delivery, subscriber

    @subscriber("work.rueckmeldung", types=["submission.*", "ris.paper.*"], batch=200)
    def rueckmeldung(events: list[Event], delivery: Delivery) -> None:
        ...  # MUSS idempotent sein

Die Zustellung (``apps.events.dispatch``) ruft den Handler mit Ereignissen in Folgenummer-
Reihenfolge auf. Handler liegen beim Empfänger, in einem Modul ``subscribers`` seiner App (z. B.
``apps/work/subscribers.py``); der Zusteller importiert diese Module beim Start
(``load_subscribers``). Diese App importiert keine Fachmodule.

- ``transactional=True`` (Standard, Datenbank-Sichten): Handler und Cursor-Fortschritt laufen in
  **einer** Transaktion, der Effekt tritt genau einmal ein. Die Transaktion hält den Sequenzierer
  auf, solange sie läuft; der Handler muss deshalb kurz sein und darf keine externen Dienste rufen.
- ``transactional=False`` (externe Effekte wie Suchindex, Mail, Fremdsysteme): Der Handler läuft
  außerhalb einer Transaktion, der Cursor wird danach festgeschrieben. Zustellung mindestens
  einmal; die Idempotenz regelt der Handler (z. B. externe Version gleich ``seq``).
- ``shadow=True``: Ein neu angelegtes Abonnement startet im Schattenbetrieb. Der Handler erkennt
  ihn an ``delivery.shadow`` und schreibt in ein Schattenziel (z. B. einen Schattenindex). Danach
  gilt der Zustand in der Datenbank (``events_subscription.state``).
- ``from_beginning=True``: Ein neu angelegtes Abonnement beginnt am Anfang des Journals statt an
  dessen Ende. Sichten werden sonst beim Umschalten einmal aus dem Bestand gebaut.

Wirft ein Handler eine Ausnahme, wird das betroffene Ereignis geparkt und wiederholt; wirft er
``TargetUnavailableError``, ist das Ziel als Ganzes nicht erreichbar: Dann wird nichts geparkt und
der ganze Batch später erneut zugestellt.
"""

from __future__ import annotations

import functools
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.utils.module_loading import autodiscover_modules

if TYPE_CHECKING:
    from .models import Event

#: Standard-Batchgröße (Spezifikation N5: 100 Ereignisse/s je Abonnement bei Batch 200)
DEFAULT_BATCH = 200
#: Obergrenze, damit ein Batch den Speicher des Workers nicht sprengt
MAX_BATCH = 5000

_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)*$")
_QUEUE = re.compile(r"^[a-z][a-z0-9_]*$")
# ``ris.paper.released`` (genau), ``ris.paper.*`` (alles darunter) oder ``*`` (alles)
_TYPMUSTER = re.compile(r"^(?:\*|[a-z0-9_]+(?:\.[a-z0-9_]+)*(?:\.\*)?)$")


class TargetUnavailableError(Exception):
    """Das Ziel des Handlers ist vorübergehend nicht erreichbar (Suchindex, Mailserver, Fremdsystem).

    Nichts wird geparkt, kein Versuch gezählt; der Zusteller wartet und stellt denselben Batch
    erneut zu. Für Fehler einzelner Ereignisse gilt dagegen jede andere Ausnahme.
    """


@dataclass(frozen=True)
class Delivery:
    """Umstände einer Zustellung, zweites Argument jedes Handlers."""

    #: Name des Abonnements
    subscription: str
    #: Schattenbetrieb: in das Schattenziel schreiben, nicht in das Live-Ziel
    shadow: bool
    #: Wiederholung geparkter Ereignisse statt fortlaufender Zustellung
    retry: bool = False
    #: Lebenszeichen der Zustellung (``alive``); im Dauerbetrieb belegt die Schleife es
    progress: Callable[[], None] | None = field(default=None, compare=False, repr=False)

    def alive(self) -> None:
        """
        Meldet, dass ein lange laufender Handler noch arbeitet (Issue #821).

        Der Worker hält eine Rolle für hängend, wenn sie ``STALE_AFTER`` Sekunden kein Lebenszeichen gibt,
        und die Lease des Abonnements läuft nach 30 s ab. Ein Handler, der für einen Batch lange braucht
        (etwa tausende Dokumente neu baut), ruft ``alive()`` deshalb regelmäßig auf, z. B. je Paket.
        """
        if self.progress is not None:
            self.progress()


Handler = Callable[[list["Event"], Delivery], None]


@dataclass(frozen=True)
class Subscriber:
    """Ein registriertes Abonnement."""

    name: str
    types: tuple[str, ...]
    handler: Handler
    batch: int = DEFAULT_BATCH
    queue: str = "default"
    transactional: bool = True
    shadow: bool = False
    from_beginning: bool = False

    @property
    def lease_name(self) -> str:
        """Leader-Lease, mit der genau ein Prozess dieses Abonnement bedient."""
        return f"dispatch:{self.name}"

    def type_filter(self) -> Q:
        """Filter auf ``Event.type`` für die Typmuster (leer bei ``*``)."""
        return type_filter(self.types)

    def matches(self, event_type: str) -> bool:
        return any(type_matches(muster, event_type) for muster in self.types)


def type_matches(pattern: str, event_type: str) -> bool:
    """``ris.paper.*`` trifft ``ris.paper.released``, nicht aber ``ris.paper`` oder ``ris.papers.x``."""
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return event_type.startswith(pattern[:-1])
    return event_type == pattern


def type_filter(patterns: tuple[str, ...]) -> Q:
    """Filter auf ``Event.type``; ``startswith`` maskiert ``_`` und ``%`` für ``LIKE`` selbst."""
    if "*" in patterns:
        return Q()
    bedingungen = [Q(type__startswith=m[:-1]) if m.endswith(".*") else Q(type=m) for m in patterns]
    return functools.reduce(operator.or_, bedingungen)


_REGISTRY: dict[str, Subscriber] = {}


def _pruefen(eintrag: Subscriber) -> None:
    if not _NAME.match(eintrag.name):
        raise ImproperlyConfigured(f"Ungültiger Abonnementname {eintrag.name!r} (klein, z. B. 'work.rueckmeldung').")
    if not eintrag.types:
        raise ImproperlyConfigured(f"Abonnement {eintrag.name!r} braucht mindestens ein Typmuster.")
    for muster in eintrag.types:
        if not _TYPMUSTER.match(muster):
            raise ImproperlyConfigured(
                f"Ungültiges Typmuster {muster!r} in {eintrag.name!r} (genau, 'bereich.*' oder '*')."
            )
    if not 1 <= eintrag.batch <= MAX_BATCH:
        raise ImproperlyConfigured(f"Batchgröße von {eintrag.name!r} muss zwischen 1 und {MAX_BATCH} liegen.")
    if not _QUEUE.match(eintrag.queue):
        raise ImproperlyConfigured(f"Ungültige Warteschlange {eintrag.queue!r} in {eintrag.name!r}.")


def _gleicher_handler(a: Handler, b: Handler) -> bool:
    # Erneuter Import desselben Moduls (Autoreload, Tests) erzeugt ein neues Funktionsobjekt
    return (getattr(a, "__module__", None), getattr(a, "__qualname__", None)) == (
        getattr(b, "__module__", None),
        getattr(b, "__qualname__", None),
    )


def subscriber(
    name: str,
    *,
    types: list[str] | tuple[str, ...],
    batch: int = DEFAULT_BATCH,
    queue: str = "default",
    transactional: bool = True,
    shadow: bool = False,
    from_beginning: bool = False,
) -> Callable[[Handler], Handler]:
    """Registriert den dekorierten Handler als Abonnement ``name`` (siehe Moduldokumentation)."""

    def registrieren(handler: Handler) -> Handler:
        eintrag = Subscriber(
            name=name,
            types=tuple(types),
            handler=handler,
            batch=batch,
            queue=queue,
            transactional=transactional,
            shadow=shadow,
            from_beginning=from_beginning,
        )
        _pruefen(eintrag)
        vorhanden = _REGISTRY.get(name)
        if vorhanden is not None and not _gleicher_handler(vorhanden.handler, handler):
            raise ImproperlyConfigured(f"Abonnement {name!r} ist bereits registriert.")
        _REGISTRY[name] = eintrag
        return handler

    return registrieren


def registered() -> list[Subscriber]:
    """Alle registrierten Abonnements, nach Namen sortiert."""
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def get(name: str) -> Subscriber:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ImproperlyConfigured(f"Abonnement {name!r} ist nicht registriert.") from None


def load_subscribers() -> list[Subscriber]:
    """Importiert das Modul ``subscribers`` jeder installierten App und gibt alle Abonnements zurück."""
    autodiscover_modules("subscribers")
    return registered()
