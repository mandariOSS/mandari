# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne des Bürgerportals (``apps.events.schedule``, Issue #515).

- ``verortung_automatisch``: alle ``GEOREF_AUTO_INTERVAL_MINUTES`` (Standard 15) Minuten ein begrenzter
  Verortungslauf (Regex/Gazetteer, ``GEOREF_AUTO_LIMIT`` Vorlagen, abschaltbar mit
  ``GEOREF_AUTO_ENABLED=false``). Bis Issue #515 lief er in einem Faden im Webprozess.
- ``rueckmeldungen_aufraeumen``: täglich um 03:50 Uhr (``TIME_ZONE``) Rückmeldungen zu Seiten
  nach zwölf Monaten löschen (``INSIGHT_FEEDBACK_RETENTION_DAYS``). Idempotent; ein verpasster Termin
  wird einmal nachgeholt.
- ``kommunenverzeichnis_abgleichen``: stündlich die gelisteten Kommunen ins Kommunenverzeichnis übernehmen, soweit
  sie dort fehlen (Issue #783). Nach dem Deploy und nach dem Listen einer Kommune ist der Kommunenwechsel so ohne
  Handgriff vollständig; der Import der CSV-Datei bleibt ein eigener Schritt (docs/INSIGHT_KOMMUNENWECHSEL.md).
- ``texterkennung_einplanen``: alle zwei Minuten, nur mit ``TEXT_EXTRACTION_RUNNER=worker`` (Issues #530, #919):
  liegen gebliebene Beanspruchungen freigeben und je Datei mit abgelegtem Inhalt, deren Erkennung wartet oder
  veraltet ist, einen Auftrag ``file.extract_text`` in die Warteschlange ``ocr`` einreihen, ohne zu beanspruchen
  (``hub.ris.erkennung.einplanen``). Mit dem Standard ``ingestor`` erkennt der OCR-Worker des Ingestors den Text.
"""

from __future__ import annotations

from django.conf import settings
from django.tasks import task

from apps.events.schedule import cron, every

from .services.georef_runner import run_auto_georef_pass
from .services.kommunenverzeichnis_import import aus_koerperschaften
from .services.page_feedback import purge_expired


@every(minutes=max(1, int(settings.GEOREF_AUTO_INTERVAL_MINUTES)))
@task
def verortung_automatisch() -> None:
    """Begrenzter automatischer Verortungslauf; eine Cache-Sperre verhindert parallele Läufe."""
    run_auto_georef_pass()


@cron("50 3 * * *")
@task
def rueckmeldungen_aufraeumen() -> int:
    """Löscht Rückmeldungen nach der Aufbewahrungsfrist; liefert ihre Anzahl."""
    return purge_expired()


@every(hours=1)
@task
def kommunenverzeichnis_abgleichen() -> int:
    """Gelistete Kommunen ohne Verzeichniseintrag übernehmen (idempotent); Rückgabe: Zahl der neuen Einträge."""
    return aus_koerperschaften().neu


@task
def texterkennung_einplanen() -> int:
    """Aufträge file.extract_text einreihen (nur mit TEXT_EXTRACTION_RUNNER=worker); Rückgabe: ihre Zahl."""
    from hub.ris.erkennung import einplanen

    return einplanen()


# Nur eingeplant, wenn die Aufträge den Text erkennen; sonst entstünde alle zwei Minuten ein leerer Lauf
if str(getattr(settings, "TEXT_EXTRACTION_RUNNER", "ingestor")) == "worker":
    texterkennung_einplanen = every(minutes=2)(texterkennung_einplanen)
