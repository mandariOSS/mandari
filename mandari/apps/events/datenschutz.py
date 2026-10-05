# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DSGVO: personenbezogene Nutzlasten im Journal neutralisieren (Issue #511, Spezifikation 4.9 und 6).

Löscht oder anonymisiert ein Eigentümer die Daten einer Person, veröffentlicht er ein Ereignis mit
``operation=redact`` zu dieser Person. Danach werden die Journaleinträge mit der Sichtbarkeit
``personenbezogen`` zu dieser Person neutralisiert: **Die Kennung bleibt, die Felder werden leer.** Zeile,
Ereignis-ID, Typ, Version, Objekt (``aggregate_id``), Folgenummer und Zeitpunkte bleiben stehen, damit Cursor,
Feed und Sicherheitsnetze weiter stimmen; die Nutzlast wird ``{}``.

**Welche Person ein ``redact`` meint** (``persons_of``), bestimmt das Ereignis selbst:

- sein Objekt, wenn das Objekt eine Person ist (``aggregate_type`` in ``PERSON_AGGREGATES``, heute ``User``),
- und die Personenfelder seiner Nutzlast laut Vertrag seines Typs und seiner Version (``"x-person": true``).

Ein ``redact`` zu einem anderen Objekt (etwa einer Sitzung) meint also nicht dieses Objekt: Die Absage einer
Person zu einer Sitzung unkenntlich zu machen, neutralisiert die Einträge dieser Person, nicht die anderer
Personen derselben Sitzung. Nennt das Ereignis keine Person (öffentliche Daten, Objekt ohne Personenbezug),
entsteht kein Auftrag.

**Was „zu dieser Person“ gehört** (``_zu_person``): Einträge, deren Objekt diese Person ist (Objekttyp in
``PERSON_AGGREGATES``) oder deren Personenfeld sie nennt. Welche Felder Personen nennen, steht in den
Verträgen (``hub.contracts``). Die Plattform kennt die Drehscheibe nicht
(``docs/adr/20260929-schichtenmodell.md``); ``hub.contracts`` hängt die Liste deshalb beim Start über
``set_person_fields_provider`` ein.

Wege:

- ``publish(..., operation="redact")`` legt in derselben Transaktion je genannter Person den Auftrag
  ``journal_neutralisieren`` an, wenn ``EVENTS_REDACT_NEUTRALIZE`` eingeschaltet ist (Standard aus). Der
  Auftrag landet immer in ``events_task`` (``tasks_backend.journal_backend``), auch wenn ``TASKS_BACKEND``
  Aufträge sonst sofort ausführt: Das Neutralisieren durchsucht das Journal und gehört nicht in die Anfrage
  und Transaktion des Eigentümers. Er läuft im Worker (Warteschlange ``default``) und ist wiederholbar. Nur
  über die Standard-Datenbank (``using``); über eine andere entsteht kein Auftrag, nur eine Warnung.
- ``manage.py events_neutralize --person <uuid>`` (mit ``--dry-run``) für Anfragen von Hand; steht im
  Sicherheitsprotokoll.

Neutralisierte Ereignisse stellt die Zustellung weiter zu (Nachspielen); Abonnenten personenbezogener Typen
müssen eine leere Nutzlast vertragen. Heute abonniert niemand solche Typen, und weder der Änderungsfeed noch
die Suche lesen sie.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from functools import reduce
from operator import or_
from typing import Final

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models import Q, QuerySet
from django.tasks import task

from .models import Event, Visibility

logger = logging.getLogger(__name__)

#: Felder je (Typ, Version), die eine Person nennen
PersonFields = Mapping[tuple[str, int], Sequence[str]]

#: Zeilen je Schreibschritt
BATCH: Final = 1000

#: Objekttypen (``aggregate_type``), deren Kennung eine Person ist
PERSON_AGGREGATES: Final = frozenset({"User"})

_provider: Callable[[], PersonFields] | None = None


def set_person_fields_provider(provider: Callable[[], PersonFields] | None) -> Callable[[], PersonFields] | None:
    """Hängt die Quelle der Personenfelder ein (``hub.contracts``); gibt die vorige zurück."""
    global _provider
    vorher = _provider
    _provider = provider
    return vorher


def person_fields() -> PersonFields:
    """Personenfelder aus den Verträgen; leer, solange nichts eingehängt ist (dann nur Personen-Objekte)."""
    return _provider() if _provider is not None else {}


def enabled() -> bool:
    """Neutralisiert ``publish(operation="redact")`` automatisch (``EVENTS_REDACT_NEUTRALIZE``)?"""
    return bool(getattr(settings, "EVENTS_REDACT_NEUTRALIZE", False))


def persons_of(event: Event) -> list[uuid.UUID]:
    """Personen, die ein Ereignis nennt: sein Objekt, wenn es eine Person ist, und seine Personenfelder."""
    personen: list[uuid.UUID] = []
    if event.aggregate_type in PERSON_AGGREGATES:
        personen.append(uuid.UUID(str(event.aggregate_id)))
    nutzlast = event.payload if isinstance(event.payload, Mapping) else {}
    for feld in person_fields().get((event.type, event.version), ()):
        wert = nutzlast.get(feld)
        if not isinstance(wert, str | uuid.UUID):
            continue
        try:
            person = uuid.UUID(str(wert))
        except ValueError:
            continue
        if person not in personen:
            personen.append(person)
    return personen


def _zu_person(person: uuid.UUID) -> Q:
    kennung = str(person)
    bedingungen = [Q(aggregate_type__in=sorted(PERSON_AGGREGATES), aggregate_id=person)]
    for (typ, version), felder in sorted(person_fields().items()):
        for feld in felder:
            bedingungen.append(Q(type=typ, version=version, **{f"payload__{feld}": kennung}))
    return reduce(or_, bedingungen)


def _betroffen(person: uuid.UUID) -> QuerySet[Event]:
    return Event.objects.filter(visibility=Visibility.PERSONENBEZOGEN).exclude(payload={}).filter(_zu_person(person))


def count(person: uuid.UUID) -> int:
    """Wie viele Journaleinträge ``neutralize`` ändern würde (Probelauf)."""
    return _betroffen(person).count()


def neutralize(person: uuid.UUID, *, batch: int = BATCH) -> int:
    """Leert die Nutzlast personenbezogener Journaleinträge zu ``person``; gibt ihre Zahl zurück.

    Wiederholbar: Schon neutralisierte Einträge (leere Nutzlast) zählen nicht mehr.
    """
    if batch < 1:
        raise ValueError("batch muss mindestens 1 sein")
    geaendert = 0
    while True:
        with transaction.atomic():
            schritt = list(_betroffen(person).order_by("pk").values_list("pk", flat=True)[:batch])
            if not schritt:
                break
            geaendert += Event.objects.filter(pk__in=schritt).update(payload={})
    if geaendert:
        logger.info("Journal: %s personenbezogene Nutzlasten neutralisiert", geaendert)
    return geaendert


@task
def journal_neutralisieren(person: str) -> int:
    """Auftrag nach einem ``redact``: personenbezogene Nutzlasten zu ``person`` neutralisieren."""
    return neutralize(uuid.UUID(person))


def after_redact(event: Event, using: str = DEFAULT_DB_ALIAS) -> int:
    """Von ``publish()`` nach einem ``redact`` aufgerufen, in dessen Transaktion; gibt die Zahl der Aufträge zurück.

    Je Person, die das Ereignis nennt (``persons_of``), ein Auftrag in ``events_task``, immer über das
    ``JournalBackend``: Mit dem sofort ausführenden Backend liefe das Neutralisieren sonst in der Anfrage und
    Transaktion des Eigentümers.
    """
    if not enabled():
        return 0
    personen = persons_of(event)
    if not personen:
        return 0
    if using != DEFAULT_DB_ALIAS:
        # Der Auftrag entstünde auf der Standard-Datenbank, nicht in der Transaktion des Ereignisses
        logger.warning(
            "redact über die Datenbank %s: kein Auftrag zum Neutralisieren angelegt (events_neutralize von Hand)",
            using,
        )
        return 0
    from .tasks_backend import journal_backend

    backend = journal_backend()
    for person in personen:
        backend.enqueue(journal_neutralisieren, [str(person)], {})
    return len(personen)
