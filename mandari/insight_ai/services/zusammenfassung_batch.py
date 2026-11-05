# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zusammenfassungen vorab im Message Batch der Claude API (halber Preis, Ergebnis meist binnen einer Stunde).

Zwei Zeitpläne im Worker (``insight_ai/schedules.py``):

- ``einreichen()``: Ist die Claude API für die Zusammenfassungen eingerichtet und der Batch eingeschaltet
  (``AISettings.summary_batch_enabled``), sammelt sie Vorgänge ohne Zusammenfassung aus den letzten
  ``summary_batch_days`` Tagen (neueste zuerst, nur mit gespeichertem Text, ``summary_store.ohne_zusammenfassung``)
  und reicht sie als einen Batch ein: höchstens ``MAX_JE_BATCH`` Anfragen und nur so viele, wie das Monatsbudget
  nach der Schätzung trägt (``insight_ai.kosten``). Solange ein Batch offen ist, kommt kein zweiter.
- ``abholen()``: Ist ein offener Batch beendet, übernimmt sie die Ergebnisse in einer Transaktion: Kosten verbuchen,
  Zusammenfassung über ``summary_store.speichern`` speichern, sofern sie noch passt (keine Rücknahme, kein neuer
  Text seit dem Einreichen, noch keine Zusammenfassung). Abgelehnte, abgeschnittene und fehlerhafte Anfragen werden
  eine Woche lang nicht erneut eingereicht; abgelaufene und abgebrochene beim nächsten Lauf wieder. Ist ein Batch
  nicht mehr abrufbar (HTTP 404, etwa nach einem Schlüssel aus einem anderen Workspace, oder älter als
  ``ERGEBNIS_TAGE``), gilt er als verloren: Seine Schätzung wird als Verbrauch verbucht (das Budget bleibt vorsichtig),
  und der nächste Batch kann kommen.

Ein Klick auf „Zusammenfassen“ erstellt weiterhin sofort (Einzelaufruf); kommt das Batch-Ergebnis danach, bleibt die
vorhandene Zusammenfassung stehen. Den Prompt baut für beide Wege ``SummaryService.vorbereiten``.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.common.db_connections import release_idle_thread_connections
from apps.common.ki_anbieter import ClaudeEndpunkt, claude_der_einstellungen, claude_fuer_zusammenfassung
from insight_ai import kosten
from insight_ai.models import KiVerbrauch, ZusammenfassungsBatch
from insight_ai.providers.claude import BatchErgebnis, ClaudeZusammenfassung
from insight_ai.providers.openai_kompatibel import KiAnbieterError

logger = logging.getLogger(__name__)

#: Höchstens so viele Anfragen je Batch (die API erlaubt 100.000; klein halten, damit ein Lauf kurz bleibt)
MAX_JE_BATCH: Final = 500
#: Sperre gegen parallele Läufe (Cache)
SPERRE: Final = "insight:summary:batch:sperre"
SPERRE_SEKUNDEN: Final = 60 * 60
#: So lange hält die Claude API die Ergebnisse eines Batches bereit (Tage ab dem Einreichen)
ERGEBNIS_TAGE: Final = 29
#: So lange wird ein Vorgang nach einer abgelehnten oder fehlerhaften Anfrage nicht erneut eingereicht
FEHLER_PAUSE_SEKUNDEN: Final = 7 * 24 * 60 * 60
#: Ergebnisarten, nach denen erst nach der Pause erneut eingereicht wird (abgelaufen/abgebrochen: beim nächsten Lauf)
PAUSE_NACH: Final = frozenset({"succeeded", "errored"})


def _pause_schluessel(vorgang_id: Any) -> str:
    return f"insight:summary:batch:pause:{vorgang_id}"


def einreichen(*, provider: Any = None) -> int:
    """Reicht einen Batch mit Vorgängen ohne Zusammenfassung ein; Rückgabe: Zahl der Anfragen (0: nichts zu tun)."""
    endpunkt = claude_fuer_zusammenfassung()
    if endpunkt is None or not endpunkt.batch:
        return 0
    if not cache.add(SPERRE, 1, timeout=SPERRE_SEKUNDEN):
        return 0
    try:
        # Erst unter der Sperre prüfen: Ein paralleler Lauf hat sonst womöglich gerade eingereicht
        if ZusammenfassungsBatch.objects.filter(status=ZusammenfassungsBatch.Status.EINGEREICHT).exists():
            return 0
        return _einreichen(endpunkt, provider or ClaudeZusammenfassung(endpunkt))
    finally:
        cache.delete(SPERRE)


def _einreichen(endpunkt: ClaudeEndpunkt, provider: Any) -> int:
    from insight_ai.services.summarizer import SummaryService
    from insight_core.services import summary_guard, summary_store

    rest = kosten.budget_rest(endpunkt)
    if rest <= 0:
        logger.info("Zusammenfassungs-Batch: Monatsbudget ausgeschöpft, nichts eingereicht")
        return 0

    beginn = timezone.now()
    seit = timezone.localtime(beginn).date() - timedelta(days=endpunkt.batch_tage)
    kandidaten = summary_store.ohne_zusammenfassung(seit=seit, limit=MAX_JE_BATCH * 2)
    dienst = SummaryService(provider=provider)
    grenze = provider.max_output_tokens
    anfragen: list[tuple[str, list[Any], int]] = []
    eintraege: dict[str, dict[str, Any]] = {}
    geschaetzt = Decimal(0)
    for vorgang in kandidaten:
        if len(anfragen) >= MAX_JE_BATCH:
            break
        if cache.get(_pause_schluessel(vorgang.pk)) or summary_guard.has_no_text(vorgang.pk):
            continue
        if vorgang.withdrawn_by_publisher or dienst._downloads_disabled(vorgang):
            continue
        vorbereitung = dienst.vorbereiten(vorgang)
        if vorbereitung is None:
            continue
        betrag = kosten.schaetzung(endpunkt.modell, zeichen=vorbereitung.zeichen, max_ausgabe=grenze, batch=True)
        if geschaetzt + betrag > rest:
            logger.info("Zusammenfassungs-Batch: Monatsbudget reicht nur für %d Anfragen", len(anfragen))
            break
        custom_id = str(vorgang.pk)
        anfragen.append((custom_id, vorbereitung.nachrichten, grenze))
        eintraege[custom_id] = {"vorgang": str(vorgang.pk), "dateien": [str(i) for i in vorbereitung.datei_ids]}
        geschaetzt += betrag
    if not anfragen:
        return 0

    # Das Einreichen kann dauern (bis zu einigen MB): Datenbankverbindung solange an den Pool zurückgeben
    release_idle_thread_connections()
    try:
        batch_id = provider.batch_einreichen(anfragen)
    except KiAnbieterError as fehler:
        logger.warning("Zusammenfassungs-Batch nicht eingereicht: %s", fehler)
        return 0
    logger.info(
        "Zusammenfassungs-Batch %s eingereicht: %d Anfragen, Modell %s, geschätzt höchstens %s USD",
        batch_id,
        len(anfragen),
        endpunkt.modell,
        geschaetzt.quantize(Decimal("0.0001")),
    )
    try:
        ZusammenfassungsBatch.objects.create(
            batch_id=batch_id,
            modell=endpunkt.modell,
            eingereicht_am=beginn,
            anfragen=eintraege,
            anzahl=len(anfragen),
            geschaetzt_usd=geschaetzt,
        )
    except Exception:
        # Ohne Eintrag würde der Batch nie abgeholt und nicht im Budget gezählt, die Vorgänge gingen erneut hinaus:
        # abbrechen (noch nicht bearbeitete Anfragen werden nicht berechnet)
        logger.exception("Zusammenfassungs-Batch %s eingereicht, aber nicht gespeichert: wird abgebrochen", batch_id)
        try:
            provider.batch_abbrechen(batch_id)
        except KiAnbieterError as fehler:
            logger.error("Zusammenfassungs-Batch %s nicht abgebrochen: %s", batch_id, fehler)
        return 0
    return len(anfragen)


def abholen(*, provider: Any = None) -> int:
    """Übernimmt die Ergebnisse beendeter Batches; Rückgabe: Zahl der gespeicherten Zusammenfassungen."""
    offen = list(
        ZusammenfassungsBatch.objects.filter(status=ZusammenfassungsBatch.Status.EINGEREICHT).order_by("eingereicht_am")
    )
    if not offen:
        return 0
    if provider is None:
        # Bezahlte Ergebnisse werden auch übernommen, wenn der Schalter „KI im Bürgerportal“ inzwischen aus ist;
        # ohne Zugang (Claude abgewählt, Schlüssel gelöscht, Host gesperrt) wird nichts abgerufen
        endpunkt = claude_der_einstellungen()
        if not isinstance(endpunkt, ClaudeEndpunkt):
            logger.warning("Zusammenfassungs-Batch: %d offen, aber kein Zugang zur Claude API", len(offen))
            return 0
        provider = ClaudeZusammenfassung(endpunkt)
    gespeichert = 0
    for batch in offen:
        try:
            if provider.batch_stand(batch.batch_id) != "ended":
                continue
            # Erst alles abrufen, dann in einer Transaktion übernehmen (kein Netz in der Transaktion)
            ergebnisse = list(provider.batch_ergebnisse(batch.batch_id))
        except KiAnbieterError as fehler:
            zu_alt = batch.eingereicht_am < timezone.now() - timedelta(days=ERGEBNIS_TAGE)
            if getattr(fehler, "status_code", None) == 404 or zu_alt:
                _verloren(batch.pk, str(fehler))
            else:
                logger.warning("Zusammenfassungs-Batch %s nicht abgerufen: %s", batch.batch_id, fehler)
            continue
        gespeichert += _uebernehmen(batch.pk, ergebnisse, provider)
    return gespeichert


def _verloren(batch_pk: Any, grund: str) -> None:
    """Batch nicht mehr abrufbar: als verloren abschließen und seine Schätzung als Verbrauch verbuchen."""
    with transaction.atomic():
        batch = ZusammenfassungsBatch.objects.select_for_update().get(pk=batch_pk)
        if batch.status != ZusammenfassungsBatch.Status.EINGEREICHT:
            return
        # Ob und wie viel Anthropic berechnet hat, ist unbekannt: vorsichtshalber die Schätzung
        KiVerbrauch.objects.create(art=KiVerbrauch.Art.BATCH, modell=batch.modell, kosten_usd=batch.geschaetzt_usd)
        batch.status = ZusammenfassungsBatch.Status.VERLOREN
        batch.abgeholt_am = timezone.now()
        batch.kosten_usd = batch.geschaetzt_usd
        batch.save(update_fields=["status", "abgeholt_am", "kosten_usd"])
    logger.warning(
        "Zusammenfassungs-Batch %s nicht mehr abrufbar (%s): als verloren abgeschlossen, Schätzung %s USD verbucht",
        batch.batch_id,
        grund,
        batch.geschaetzt_usd,
    )


def _uebernehmen(batch_pk: Any, ergebnisse: list[BatchErgebnis], provider: Any) -> int:
    """Ergebnisse eines Batches übernehmen, genau einmal (Zeilensperre, Status)."""
    from insight_core.services import summary_store

    with transaction.atomic():
        batch = ZusammenfassungsBatch.objects.select_for_update().get(pk=batch_pk)
        if batch.status != ZusammenfassungsBatch.Status.EINGEREICHT:
            return 0
        endpunkt = getattr(provider, "endpunkt", None)
        gespeichert = verworfen = fehlgeschlagen = 0
        summe = Decimal(0)
        for ergebnis in ergebnisse:
            eintrag = batch.anfragen.get(ergebnis.custom_id) or {}
            vorgang_id = eintrag.get("vorgang")
            modell = ergebnis.modell or batch.modell
            if ergebnis.usage is not None:
                summe += kosten.verbuchen(
                    art=KiVerbrauch.Art.BATCH, modell=modell, usage=ergebnis.usage, vorgang_id=vorgang_id
                )
            if not vorgang_id:
                continue
            if not ergebnis.text:
                fehlgeschlagen += 1
                if ergebnis.art in PAUSE_NACH:
                    cache.set(_pause_schluessel(vorgang_id), 1, timeout=FEHLER_PAUSE_SEKUNDEN)
                logger.info(
                    "Zusammenfassungs-Batch %s: Vorgang %s ohne Ergebnis (%s)",
                    batch.batch_id,
                    vorgang_id,
                    ergebnis.fehler or ergebnis.art,
                )
                continue
            hinweis = (
                replace(endpunkt, modell=modell).zusammenfassung_hinweis if isinstance(endpunkt, ClaudeEndpunkt) else ""
            )
            if summary_store.speichern(
                vorgang_id, ergebnis.text, hinweis, seit=batch.eingereicht_am, dateien=eintrag.get("dateien") or []
            ):
                gespeichert += 1
            else:
                verworfen += 1
        batch.status = ZusammenfassungsBatch.Status.ABGEHOLT
        batch.abgeholt_am = timezone.now()
        batch.gespeichert, batch.verworfen, batch.fehlgeschlagen = gespeichert, verworfen, fehlgeschlagen
        batch.kosten_usd = summe
        batch.save(update_fields=["status", "abgeholt_am", "gespeichert", "verworfen", "fehlgeschlagen", "kosten_usd"])
    logger.info(
        "Zusammenfassungs-Batch %s übernommen: %d gespeichert, %d verworfen, %d ohne Ergebnis, Kosten %s USD",
        batch.batch_id,
        gespeichert,
        verworfen,
        fehlgeschlagen,
        summe.quantize(Decimal("0.0001")),
    )
    return gespeichert
