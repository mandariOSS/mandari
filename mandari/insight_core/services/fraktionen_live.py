# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionen aus den Wortmeldungen der Live-Übertragungen (Issue #915, Regeln der Übernahme: Issue #916).

Abonnement ``insight.fraktionen_live`` auf ``ris.broadcast.speaker_changed`` (registriert in
``insight_core/subscribers.py``, solange ``LIVE_UEBERTRAGUNG_AKTIV`` an ist). Für jede neue Wortmeldung liest es die
Angaben über ``hub.live.selectors.wortmeldung``. Nennt sie eine Person und eine gelesene Fraktion (keine
Funktionsbezeichnung), verbucht ``fraktionen.fraktion_aus_einblendung`` die Fraktion: eindeutig, wenn die Person
eindeutig zugeordnet und mindestens zweimal gleich gelesen ist (``WortmeldungDaten.eindeutig``), dann entsteht eine
bestätigte Zuordnung; sonst ein Vorschlag, der im Admin auf Bestätigung wartet.

- **Idempotent:** ``PersonFraktionBeleg`` hält jede verbuchte Wortmeldung im selben Sicherungspunkt fest. Eine erneute
  Zustellung, ein Nachspielen (``events_dispatch --replay``) oder ``nachholen`` zählt sie nicht noch einmal.
- **Datenbank-Sicht:** transaktional; Zuordnung, Beleg, Ereignis ``ris.person.faction_assigned`` und Cursor werden
  zusammen festgeschrieben. Keine Abrufe nach außen, keine Bild- oder Tondaten, keine Redezeiten.
- **Kein Schattenbetrieb:** Es gibt keinen bisherigen Weg, mit dem zu vergleichen wäre. Steht das Abonnement in der
  Datenbank auf Schatten, verbucht es nichts. Abschalten lässt es sich im Admin (Abonnement pausieren).
- **Nachholen:** ``manage.py fraktionen_aus_wortmeldungen`` (``nachholen``) verbucht vorhandene Wortmeldungen, etwa
  aus eingespielten Protokollen (``live_protokoll_einspielen`` schreibt keine Ereignisse) oder aus der Zeit vor dem
  Abonnement, das am Ende des Journals beginnt.
"""

from __future__ import annotations

import enum
import logging
import uuid
from collections import Counter
from typing import TYPE_CHECKING, Final

from django.db import transaction

from hub.live import selectors as live

from ..models import OParlBody, OParlPerson, PersonFraktionBeleg
from . import fraktionen

if TYPE_CHECKING:
    from apps.events.models import Event
    from apps.events.registry import Delivery

logger = logging.getLogger(__name__)

NAME: Final = "insight.fraktionen_live"
TYPES: Final = ("ris.broadcast.speaker_changed",)
BATCH: Final = 100
QUEUE: Final = "default"


class Ergebnis(enum.StrEnum):
    """Was mit einer Wortmeldung geschah."""

    VERBUCHT = "verbucht"
    SCHON_VERBUCHT = "schon_verbucht"
    #: keine Person, keine gelesene Fraktion oder eine Funktion statt der Fraktion
    OHNE_FRAKTION = "ohne_fraktion"
    #: Wortmeldung, Person oder Körperschaft gibt es nicht (mehr)
    FEHLT = "fehlt"


def aus_wortmeldung(speech_id: uuid.UUID | str) -> Ergebnis:
    """Verbucht die gelesene Fraktion einer Wortmeldung höchstens einmal (siehe Moduldokumentation)."""
    daten = live.wortmeldung(speech_id)
    if daten is None:
        return Ergebnis.FEHLT
    if daten.person_id is None or not daten.fraktion_gelesen.strip() or daten.funktion_gelesen.strip():
        return Ergebnis.OHNE_FRAKTION
    person = OParlPerson.objects.filter(pk=daten.person_id).first()
    body = OParlBody.objects.filter(pk=daten.body_id).first()
    if person is None or body is None:
        return Ergebnis.FEHLT
    with transaction.atomic():
        beleg, neu = PersonFraktionBeleg.objects.get_or_create(wortmeldung=daten.speech_id)
        if not neu:
            return Ergebnis.SCHON_VERBUCHT
        zuordnung = fraktionen.fraktion_aus_einblendung(
            person=person,
            body=body,
            bezeichnung=daten.fraktion_gelesen,
            zeitpunkt=daten.begonnen_am,
            eindeutig=daten.eindeutig,
        )
        if zuordnung is None:
            # Die Bezeichnung war leer oder doch eine Funktion; der Beleg bleibt, die Wortmeldung ist erledigt
            return Ergebnis.OHNE_FRAKTION
        beleg.zuordnung = zuordnung
        beleg.save(update_fields=["zuordnung"])
    return Ergebnis.VERBUCHT


def fraktionen_live(events: list[Event], delivery: Delivery) -> None:
    """Handler des Abonnements (idempotent je Wortmeldung)."""
    if delivery.shadow:
        return
    for event in events:
        speech_id = (event.payload or {}).get("speech")
        if not speech_id:
            continue
        ergebnis = aus_wortmeldung(speech_id)
        logger.debug("Abonnement %s: Wortmeldung %s – %s", NAME, speech_id, ergebnis)


def nachholen(*, meeting_id: uuid.UUID | None = None, body_id: uuid.UUID | None = None) -> Counter[Ergebnis]:
    """Verbucht vorhandene Wortmeldungen (älteste zuerst); Rückgabe: Zahl je Ergebnis."""
    ergebnisse: Counter[Ergebnis] = Counter()
    for speech_id in live.wortmeldungen_mit_fraktion(meeting_id=meeting_id, body_id=body_id):
        ergebnisse[aus_wortmeldung(speech_id)] += 1
    return ergebnisse
