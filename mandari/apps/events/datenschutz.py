# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DSGVO: personenbezogene Nutzlasten im Journal neutralisieren (Issue #511, Spezifikation 4.9 und 6).

Löscht oder anonymisiert ein Eigentümer die Daten einer Person, veröffentlicht er ein Ereignis mit
``operation=redact`` zu dieser Person. Danach werden die Journaleinträge mit der Sichtbarkeit
``personenbezogen`` zu dieser Person neutralisiert: **Die Kennung bleibt, die Felder werden leer.** Zeile,
Ereignis-ID, Typ, Version, Objekt (``aggregate_id``), Folgenummer und Zeitpunkte bleiben stehen, damit Cursor,
Feed und Sicherheitsnetze weiter stimmen; die Nutzlast wird ``{}``.

„Zu dieser Person“ heißt: das Objekt des Ereignisses ist die Person (``aggregate_id``), oder ein Feld der
Nutzlast nennt sie. Welche Felder Personen nennen, steht in den Verträgen (``"x-person": true`` an der
Eigenschaft, ``hub.contracts``). Die Plattform kennt die Drehscheibe nicht
(``docs/adr/20260929-schichtenmodell.md``); ``hub.contracts`` hängt die Liste deshalb beim Start über
``set_person_fields_provider`` ein.

Wege:

- ``publish(..., operation="redact")`` legt in derselben Transaktion den Auftrag ``journal_neutralisieren``
  an, wenn ``EVENTS_REDACT_NEUTRALIZE`` eingeschaltet ist (Standard aus). Der Auftrag ist wiederholbar.
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
from django.db import transaction
from django.db.models import Q, QuerySet
from django.tasks import task

from .models import Event, Visibility

logger = logging.getLogger(__name__)

#: Felder je (Typ, Version), die eine Person nennen
PersonFields = Mapping[tuple[str, int], Sequence[str]]

#: Zeilen je Schreibschritt
BATCH: Final = 1000

_provider: Callable[[], PersonFields] | None = None


def set_person_fields_provider(provider: Callable[[], PersonFields] | None) -> Callable[[], PersonFields] | None:
    """Hängt die Quelle der Personenfelder ein (``hub.contracts``); gibt die vorige zurück."""
    global _provider
    vorher = _provider
    _provider = provider
    return vorher


def person_fields() -> PersonFields:
    """Personenfelder aus den Verträgen; leer, solange nichts eingehängt ist (dann nur ``aggregate_id``)."""
    return _provider() if _provider is not None else {}


def enabled() -> bool:
    """Neutralisiert ``publish(operation="redact")`` automatisch (``EVENTS_REDACT_NEUTRALIZE``)?"""
    return bool(getattr(settings, "EVENTS_REDACT_NEUTRALIZE", False))


def _zu_person(person: uuid.UUID) -> Q:
    kennung = str(person)
    bedingungen = [Q(aggregate_id=person)]
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


def after_redact(event: Event) -> None:
    """Von ``publish()`` nach einem ``redact`` aufgerufen, in dessen Transaktion."""
    if enabled():
        journal_neutralisieren.enqueue(str(event.aggregate_id))
