# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufträge aus dem Admin des Bürgerportals (Issue #515): Löschen einer Kommune.

Sie lief bis Issue #515 in einem Faden im Webprozess. Jetzt legt der Admin sie immer im Journal an
(``apps.events.tasks_backend.journal_backend``), auch solange die Webprozesse Aufträge sonst sofort
ausführen: Eine Löschung dauert Minuten und gehört nicht in die Anfrage. Ausgeführt wird sie vom Worker
(Warteschlange ``default``); Zeitgrenze und Versuche stehen in ``TASKS["default"]["OPTIONS"]["tasks"]``.

Den Sync einer Quelle legt der Admin seit Issue #554 nicht mehr im Worker an: Abgeglichen wird nur noch
im Ingestor (``insight_sync.daemon.trigger_sync``). ``quelle_synchronisieren`` bleibt als leere Hülle.
"""

from __future__ import annotations

import logging
from typing import Final

from django.core.cache import cache
from django.tasks import task

logger = logging.getLogger(__name__)

#: Doppelklick-Schutz je Kommune (``cache.add``); verfällt von selbst, falls der Auftrag nie läuft
DELETION_LOCK_TTL: Final = 7200


def deletion_lock_key(body_id: str) -> str:
    return f"body-deletion-{body_id}"


@task
def quelle_synchronisieren(source_id: str, full: bool = False) -> None:
    """
    Früher: Sync einer Quelle im Worker der Anwendung. Seit Issue #554 gleicht nur noch der Ingestor ab; der Admin
    schickt dafür einen Auslöser über Redis. Dünne Hülle: Der registrierte Pfad bleibt, damit vor dem Deploy
    eingereihte Aufträge nicht scheitern; sie tun nichts außer einer Warnung im Log.
    """
    logger.warning(
        "Sync aus dem Admin im Worker übersprungen (Quelle %s, vollständig=%s): Quellen gleicht nur noch der "
        "Ingestor ab. Im Admin erneut anstoßen oder im Container ingestor python -m src.main sync --source "
        "<Basis-URL> ausführen.",
        source_id,
        full,
    )


@task(queue_name="ocr")
def file_extract_text(file_id: str) -> None:
    """
    Auftrag ``file.extract_text`` (Issues #530, #919): Text einer RIS-Datei aus der Ablage erkennen, eingereiht vom
    Zeitplan ``texterkennung_einplanen`` bei ``TEXT_EXTRACTION_RUNNER=worker``. Dünne Hülle: Der registrierte Pfad
    bleibt, damit wartende Aufträge über einen Deploy weiterlaufen; die Logik liegt in ``hub.ris.erkennung``.
    Wiederholbar: Eine erledigte oder beanspruchte Datei wird übersprungen.
    """
    from hub.ris.erkennung import erkennen

    ergebnis = erkennen(file_id)
    logger.info("Texterkennung Datei %s: %s", file_id, ergebnis)


@task
def kommune_loeschen(body_id: str) -> None:
    """Löscht eine Kommune mit allen RIS-Daten; gibt danach den Doppelklick-Schutz frei."""
    from .services.body_deletion import delete_body_data

    try:
        delete_body_data(body_id)
    finally:
        cache.delete(deletion_lock_key(body_id))
