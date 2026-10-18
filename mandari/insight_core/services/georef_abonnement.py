# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnement ``insight.verortung`` (Issue #919, ADR Dokumentkette, Abschnitt 9): neuer Text stößt die Verortung
seines Vorgangs an, statt dass erst die Abfrage alle 15 Minuten (Zeitplan ``verortung_automatisch``) ihn findet.

Für jede Anlage mit neuem Text (``ris.file.text_extracted``) gilt für ihren Vorgang:

- **Markieren:** Ein Vorgang, den der automatische Lauf schon verortet hat oder mangels Text nicht verorten konnte
  (``completed``, ``ai_needed``, ``skipped``, ``failed``), geht zurück auf ``pending``; der neue Text kann weitere
  Orte nennen. Ausgenommen sind Vorgänge, deren Verortung von der KI stammt (``georef_method`` enthält ``ai``) oder
  die die KI ohne Ort abgeschlossen hat (``no_locations``): Der automatische Lauf ersetzte deren Orte bzw.
  Ergebnis, die KI läuft nie automatisch. Offizielle, manuelle und Umring-Orte bleiben bei jedem Lauf erhalten
  (``georeferencing.PRESERVED_SOURCES``), entfernte bleiben gesperrt.
- **Einreihen:** Mit ``TASKS_BACKEND=journal`` reiht es je Vorgang im Stand ``pending`` den Auftrag
  ``georef_runner.verortung_vorgang`` ein (Idempotenzschlüssel ``<vorgang>:<ereignis>``). Ohne Journal liefe der
  Auftrag sofort in der Zustellung; dann bleibt es beim Markieren, und der Zeitplan verortet.
- **Nur, wo der automatische Lauf arbeitet:** Kommunen mit Straßenverzeichnis, nicht gelöschte Vorgänge, und nur
  mit ``GEOREF_ENABLED`` und ``GEOREF_AUTO_ENABLED``. Dateien ohne Vorgang (Sitzungsdokumente) und unbekannte
  Kennungen bleiben ohne Wirkung.

- **Datenbank-Sicht:** transaktional und kurz; je Batch wenige Abfragen, ein ``UPDATE`` und die Aufträge. Gerechnet
  wird nie im Handler, nur im Auftrag bzw. im Zeitplan.
- **Idempotent:** Eine erneute Zustellung findet die Vorgänge schon auf ``pending`` und reiht wegen des Schlüssels
  nichts doppelt ein. Wer ältere Ereignisse nachspielt, stößt höchstens eine erneute Verortung an; sie ist
  deterministisch und behält die geschützten Orte.
- **Schattenbetrieb:** zählt nur, was markiert und eingereiht würde (``mandari_georef_subscription_total``); der
  Zeitplan arbeitet unverändert.

Schalter ``GEOREF_SUBSCRIPTION`` (``aus``, ``schatten``, ``aktiv``), siehe ``insight_core/subscribers.py``. Wirksam
nur mit ``aktiv`` und solange das Abonnement in der Datenbank nicht im Schatten steht (wie beim Suchindex).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Final

from django.conf import settings
from django.db.models import Exists, OuterRef, Q
from prometheus_client import Counter

from .georef_runner import auto_georef_enabled, verortung_vorgang

if TYPE_CHECKING:
    from apps.events.models import Event
    from apps.events.registry import Delivery

logger = logging.getLogger(__name__)

NAME: Final = "insight.verortung"
TYPES: Final = ("ris.file.text_extracted",)
BATCH: Final = 200
QUEUE: Final = "default"
#: Stände, aus denen neuer Text die Verortung erneut anstößt (siehe Moduldokumentation)
NEU_VERORTEN: Final = ("completed", "ai_needed", "skipped", "failed")

ERGEBNIS = Counter(
    "mandari_georef_subscription_total",
    "Vorgänge des Abonnements insight.verortung je Ziel (schatten, live) und Ergebnis (markiert, eingereiht)",
    ["target", "result"],
)


def neu_verorten_q() -> Q:
    """Vorgänge, die neuer Text zurück auf ``pending`` setzt: automatisch verortet, nie von der KI."""
    ohne_ki = Q(georef_method__isnull=True) | ~Q(georef_method__icontains="ai")
    return Q(georef_status__in=NEU_VERORTEN) & ohne_ki


def wirksam(delivery: Delivery) -> bool:
    """Markieren und Einreihen nur mit Schalter ``aktiv`` und außerhalb des Schattenbetriebs in der Datenbank."""
    return not delivery.shadow and str(getattr(settings, "GEOREF_SUBSCRIPTION", "aus")) == "aktiv"


def journal_aktiv() -> bool:
    """Landet der Auftrag ``verortung_vorgang`` im Journal (``TASKS_BACKEND=journal``)?"""
    from django.tasks import task_backends

    from apps.events.tasks_backend import JournalBackend

    return isinstance(task_backends[verortung_vorgang.backend], JournalBackend)


def _vorgaenge(events: list[Event]) -> dict[uuid.UUID, uuid.UUID]:
    """Vorgang → Kennung des ersten Ereignisses, das ihm neuen Text meldet (Folgenummer-Reihenfolge)."""
    from insight_core.models import OParlFile

    datei_je_ereignis: list[tuple[uuid.UUID, uuid.UUID]] = []
    for event in events:
        roh = event.aggregate_id if event.aggregate_type == "File" else (event.payload or {}).get("file")
        try:
            datei_je_ereignis.append((uuid.UUID(str(roh)), event.event_id))
        except (TypeError, ValueError):
            continue
    if not datei_je_ereignis:
        return {}
    vorgang_je_datei = dict(
        OParlFile.objects.filter(pk__in={datei for datei, _ in datei_je_ereignis}, paper_id__isnull=False).values_list(
            "pk", "paper_id"
        )
    )
    vorgaenge: dict[uuid.UUID, uuid.UUID] = {}
    for datei, ereignis in datei_je_ereignis:
        vorgang = vorgang_je_datei.get(datei)
        if vorgang is not None:
            vorgaenge.setdefault(vorgang, ereignis)
    return vorgaenge


def verortung(events: list[Event], delivery: Delivery) -> None:
    """Handler des Abonnements: Vorgänge mit neuem Text markieren und ihre Verortung einreihen (idempotent)."""
    from apps.events.tasks_backend import enqueue_once
    from insight_core.models import OParlPaper, Street

    if not auto_georef_enabled():
        return
    vorgaenge = _vorgaenge(events)
    if not vorgaenge:
        return
    kandidaten = OParlPaper.objects.filter(pk__in=list(vorgaenge), deleted=False).filter(
        Exists(Street.objects.filter(body_id=OuterRef("body_id")))
    )
    live = wirksam(delivery)
    ziel = "live" if live else "schatten"
    markieren = kandidaten.filter(neu_verorten_q())
    markiert = markieren.update(georef_status="pending") if live else markieren.count()
    if markiert:
        ERGEBNIS.labels(target=ziel, result="markiert").inc(markiert)
    if not journal_aktiv():
        return
    wartend = (
        kandidaten.filter(georef_status="pending")
        if live
        else kandidaten.filter(Q(georef_status="pending") | neu_verorten_q())
    )
    eingereiht = 0
    for vorgang in wartend.values_list("pk", flat=True):
        if live:
            enqueue_once(verortung_vorgang, f"{vorgang}:{vorgaenge[vorgang]}", str(vorgang))
        eingereiht += 1
    if eingereiht:
        ERGEBNIS.labels(target=ziel, result="eingereiht").inc(eingereiht)
    if live and (markiert or eingereiht):
        logger.info("Abonnement %s: %d Vorgänge markiert, %d Verortungen eingereiht", NAME, markiert, eingereiht)
