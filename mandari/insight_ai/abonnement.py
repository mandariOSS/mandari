# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnement ``insight.zusammenfassung`` (Issue #919, ADR Dokumentkette, Abschnitt 9): neuer Text verwirft die
KI-Zusammenfassung seines Vorgangs.

Eine Zusammenfassung fasst den Text aller Anlagen eines Vorgangs zusammen. Kommt für eine Anlage neuer Text hinzu
(``ris.file.text_extracted``, aus dem OCR-Worker des Ingestors oder dem Auftrag ``file.extract_text``), ist sie
veraltet. Das Abonnement **verwirft** sie (``summary`` leer), es erzeugt keine neue: Die entsteht wie bisher erst,
wenn jemand sie auf der Vorgangsseite anfordert (Kosten- und Lastgrenzen in ``insight_core.services.summary_guard``).
So verwerfen auch die Rücknahme einer Anlage und der Löschabgleich eine Zusammenfassung.

- **Datenbank-Sicht:** transaktional und kurz; je Batch ein ``UPDATE`` beim Eigentümer des Bestands
  (``insight_core.services.summary_store``). Keine Aufrufe nach außen, keine KI.
- **Idempotent:** Eine erneute Zustellung findet keine Zusammenfassung mehr vor und ändert nichts. Wer ältere
  Ereignisse nachspielt (``events_dispatch --replay``), verwirft auch eine danach neu erstellte Zusammenfassung; sie
  entsteht auf Abruf neu.
- **Nur RIS-Dateien:** Ereignisse ohne bekannte Datei, Dateien ohne Vorgang (Sitzungsdokumente) und Vorgänge ohne
  Zusammenfassung bleiben ohne Wirkung.
- **Schattenbetrieb:** zählt nur, was verworfen würde (``mandari_summary_subscription_total{target="schatten"}``);
  es gibt keinen bisherigen Weg, mit dem zu vergleichen wäre.

Schalter ``SUMMARY_SUBSCRIPTION`` (``aus``, ``schatten``, ``aktiv``), siehe ``insight_ai/subscribers.py``. Verworfen
wird nur mit ``aktiv`` und solange das Abonnement in der Datenbank nicht im Schatten steht (wie beim Suchindex).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Final

from django.conf import settings
from prometheus_client import Counter

if TYPE_CHECKING:
    from apps.events.models import Event
    from apps.events.registry import Delivery

logger = logging.getLogger(__name__)

NAME: Final = "insight.zusammenfassung"
TYPES: Final = ("ris.file.text_extracted",)
BATCH: Final = 200
QUEUE: Final = "default"

ERGEBNIS = Counter(
    "mandari_summary_subscription_total",
    "Vorgänge des Abonnements insight.zusammenfassung je Ziel (schatten, live) und Ergebnis",
    ["target", "result"],
)


def file_ids(events: list[Event]) -> set[uuid.UUID]:
    """Dateien der Ereignisse (Aggregat ``File``, sonst Feld ``file`` der Nutzlast); ungültige Kennungen entfallen."""
    kennungen: set[uuid.UUID] = set()
    for event in events:
        roh = event.aggregate_id if event.aggregate_type == "File" else (event.payload or {}).get("file")
        try:
            kennungen.add(uuid.UUID(str(roh)))
        except (TypeError, ValueError):
            continue
    return kennungen


def wirksam(delivery: Delivery) -> bool:
    """Verwerfen nur mit Schalter ``aktiv`` und außerhalb des Schattenbetriebs in der Datenbank."""
    return not delivery.shadow and str(getattr(settings, "SUMMARY_SUBSCRIPTION", "aus")) == "aktiv"


def zusammenfassung_verwerfen(events: list[Event], delivery: Delivery) -> None:
    """Handler des Abonnements: verwirft die Zusammenfassungen der Vorgänge mit neuem Text (idempotent)."""
    from insight_core.services import summary_store

    dateien = file_ids(events)
    if not dateien:
        return
    live = wirksam(delivery)
    anzahl = summary_store.discard_for_files(dateien, dry_run=not live)
    if not anzahl:
        return
    ERGEBNIS.labels(target="live" if live else "schatten", result="verworfen").inc(anzahl)
    if live:
        logger.info("Abonnement %s: %d Zusammenfassungen nach neuem Text verworfen", NAME, anzahl)
