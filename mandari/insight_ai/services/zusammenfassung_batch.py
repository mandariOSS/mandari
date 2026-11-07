# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zusammenfassungen vorab im Message Batch der Claude API (halber Preis, Ergebnis meist binnen einer Stunde).

Zeitpläne im Worker (``insight_ai/schedules.py``):

- ``einreichen()`` einmal am Tag (Tagesbatch, Issue #1220): Ist die Claude API für die Zusammenfassungen eingerichtet
  und der Batch eingeschaltet (``AISettings.summary_batch_enabled``), sammelt sie Vorgänge ohne Zusammenfassung der
  ausgewählten Kommunen ab deren Stichtag (``TagesbatchKommune``, ``summary_store.fuer_tagesbatch``; ohne Auswahl
  nichts) und reicht sie als einen Batch ein: höchstens ``MAX_JE_BATCH`` Anfragen und nur so viele, wie das
  Monatsbudget nach der Schätzung trägt (``insight_ai.kosten``). Solange ein Batch offen ist, kommt kein zweiter.
  **Texterkennung ist Pflicht:** Ein Vorgang kommt erst, wenn der Text aller aktuellen Anlagen erkannt ist und (mit
  Prüfung der Verortung) der Verortungslauf danach gelaufen ist (``summary_store.tagesbatch_wartet``); wartende
  Vorgänge zählt das Protokoll. Am Abruf gescheiterte Anlagen dieser Kommunen reiht ``nacharbeiten()`` vorher neu ein.
- **Veraltete Zusammenfassungen (Issue #1257):** Nach den Vorgängen ohne Zusammenfassung kommen die derselben
  Kommunen, deren Zusammenfassung einen älteren Beratungsstand kennt (``summary_store.veraltete``, etwa: erstellt
  vor dem Beschluss). Die neue ersetzt sie bei der Übernahme, sofern sie inzwischen niemand neu erstellt hat; die
  Verortung wird dabei nicht erneut geprüft. Für den übrigen Bestand gibt es ``nachziehen()`` (``manage.py
  zusammenfassungen_nachziehen``): ein Batch je Lauf mit Obergrenze, nie neben einem offenen.
- **Verortung als zweiter Faktor (Issue #1222):** Für Kommunen mit ``verortung_pruefen`` bekommt Claude in derselben
  Anfrage die gefundenen Orte (``verortung_pruefung.orte_fuer_pruefung``) und antwortet als JSON
  (``prompts.TAGESBATCH_SCHEMA``): Zusammenfassung, Urteil je Ort, fehlende Orte. Nach dem Speichern der
  Zusammenfassung übernimmt ``verortung_pruefung.anwenden`` die Urteile (außerhalb der Transaktion, weil fehlende Orte
  geokodiert werden).
- **Mechanische Prüfung (Issue #1271):** Vor der Übernahme wird jedes Ergebnis gegen das Material seines Prompts
  geprüft (``zusammenfassung_pruefung``, Material neu zusammengestellt wie beim Einreichen). Verworfene werden
  protokolliert und nicht gespeichert; fällt nur die Fassung in Einfacher Sprache durch, entfällt nur sie. Einen
  Vorgang, dessen Zusammenfassung mit der aktuellen Prompt-Fassung verworfen wurde, reicht der Batch nicht erneut ein.
- ``abholen()``: Ist ein offener Batch beendet, übernimmt sie die Ergebnisse in einer Transaktion: Kosten verbuchen,
  Zusammenfassung über ``summary_store.speichern`` speichern, sofern sie noch passt (keine Rücknahme, kein neuer
  Text seit dem Einreichen, noch keine Zusammenfassung bzw. die veraltete unverändert). Abgelehnte, abgeschnittene
  und fehlerhafte Anfragen werden eine Woche lang nicht erneut eingereicht; abgelaufene und abgebrochene beim
  nächsten Lauf wieder. Ist ein Batch nicht mehr abrufbar (HTTP 404, etwa nach einem Schlüssel aus einem anderen
  Workspace, oder älter als ``ERGEBNIS_TAGE``), gilt er als verloren: Seine Schätzung wird als Verbrauch verbucht
  (das Budget bleibt vorsichtig), und der nächste Batch kann kommen.

Ein Klick auf „Zusammenfassen“ erstellt weiterhin sofort (Einzelaufruf); kommt das Batch-Ergebnis danach, bleibt die
vorhandene Zusammenfassung stehen. Ist sie veraltet, erstellt „Neu zusammenfassen“ auf der Vorgangsseite sie ebenfalls
sofort neu. Den Prompt baut für beide Wege ``SummaryService.vorbereiten``.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.common.db_connections import release_idle_thread_connections
from apps.common.ki_anbieter import ClaudeEndpunkt, claude_der_einstellungen, claude_fuer_zusammenfassung
from insight_ai import kosten
from insight_ai.models import KiVerbrauch, TagesbatchKommune, ZusammenfassungsBatch
from insight_ai.providers.claude import BatchErgebnis, ClaudeZusammenfassung
from insight_ai.providers.openai_kompatibel import KiAnbieterError
from insight_ai.services import zusammenfassung_pruefung
from insight_ai.services.prompts import PROMPT_FASSUNG
from insight_ai.services.summarizer import ZUSATZ_AUSGABE_EINFACH, zerlegen

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
#: Zusätzliche Ausgabe-Token je Anfrage mit Prüfung der Verortung (Urteile, Begründungen, fehlende Orte)
ZUSATZ_AUSGABE_VERORTUNG: Final = 1500
#: Zusatz zum Anbieterhinweis, wenn Claude in derselben Anfrage auch die Verortung geprüft hat
HINWEIS_VERORTUNG: Final = "; prüfte auch die Orte auf der Karte"


def kommunen() -> tuple[dict[Any, Any], set[Any]]:
    """Ausgewählte Kommunen des Tagesbatches: (Kennung → Stichtag, Kennungen mit Prüfung der Verortung)."""
    auswahl = list(TagesbatchKommune.objects.values_list("body_id", "ab", "verortung_pruefen"))
    return {kommune: ab for kommune, ab, _ in auswahl}, {kommune for kommune, _, pruefen in auswahl if pruefen}


def nacharbeiten() -> int:
    """
    Am Abruf gescheiterte Anlagen der ausgewählten Kommunen neu einreihen (``summary_store.abrufe_nacharbeiten``),
    damit ihr Text vor dem nächsten Tagesbatch vorliegt; Rückgabe: Zahl der zurückgesetzten Anlagen. Nur mit
    eingeschaltetem Batch und Auswahl.
    """
    from insight_core.services import summary_store

    endpunkt = claude_fuer_zusammenfassung()
    stichtage, _ = kommunen()
    if endpunkt is None or not endpunkt.batch or not stichtage:
        return 0
    anzahl = summary_store.abrufe_nacharbeiten(stichtage)
    if anzahl:
        logger.info("Tagesbatch: %d am Abruf gescheiterte Anlagen neu eingereiht", anzahl)
    return anzahl


def _pause_schluessel(vorgang_id: Any) -> str:
    return f"insight:summary:batch:pause:{vorgang_id}"


def einreichen(*, provider: Any = None) -> int:
    """
    Reicht den Tagesbatch ein: Vorgänge ohne Zusammenfassung, danach veraltete (Issue #1257), jeweils der
    ausgewählten Kommunen ab ihrem Stichtag. Rückgabe: Zahl der Anfragen (0: nichts zu tun).
    """
    endpunkt = claude_fuer_zusammenfassung()
    if endpunkt is None or not endpunkt.batch:
        return 0
    if not cache.add(SPERRE, 1, timeout=SPERRE_SEKUNDEN):
        return 0
    try:
        # Erst unter der Sperre prüfen: Ein paralleler Lauf hat sonst womöglich gerade eingereicht
        if _batch_offen():
            return 0
        return _einreichen(endpunkt, provider or ClaudeZusammenfassung(endpunkt))
    finally:
        cache.delete(SPERRE)


def _batch_offen() -> bool:
    return ZusammenfassungsBatch.objects.filter(status=ZusammenfassungsBatch.Status.EINGEREICHT).exists()


def _einreichen(endpunkt: ClaudeEndpunkt, provider: Any) -> int:
    from insight_core.services import summary_store

    rest = kosten.budget_rest(endpunkt)
    if rest <= 0:
        logger.info("Zusammenfassungs-Batch: Monatsbudget ausgeschöpft, nichts eingereicht")
        return 0
    stichtage, mit_verortung = kommunen()
    if not stichtage:
        logger.info("Zusammenfassungs-Batch: keine Kommune ausgewählt, nichts eingereicht")
        return 0
    beginn = timezone.now()
    # Zuerst Vorgänge ohne Zusammenfassung, dann veraltete (neues Beratungsergebnis seit der Zusammenfassung)
    # Mit der aktuellen Prompt-Fassung schon verworfene Vorgänge nicht erneut (Issue #1271)
    verworfen = zusammenfassung_pruefung.verworfene_vorgaenge()
    kandidaten = summary_store.fuer_tagesbatch(stichtage, limit=MAX_JE_BATCH * 2, ohne=verworfen)
    kandidaten += summary_store.veraltete(stichtage, limit=MAX_JE_BATCH, ohne=verworfen)
    plan = _planen(endpunkt, provider, kandidaten, rest=rest, mit_verortung=mit_verortung, hoechstens=MAX_JE_BATCH)
    if plan.wartend:
        logger.info("Tagesbatch: %d Vorgänge warten noch %s", sum(plan.wartend.values()), dict(plan.wartend))
    if not plan.anfragen:
        return 0
    return _abschicken(endpunkt, provider, plan, beginn)


@dataclass
class _Plan:
    """Anfragen eines Batches mit ihren Einträgen (``ZusammenfassungsBatch.anfragen``) und der Kostenschätzung."""

    anfragen: list[tuple[Any, ...]] = field(default_factory=list)
    eintraege: dict[str, dict[str, Any]] = field(default_factory=dict)
    geschaetzt: Decimal = Decimal(0)
    #: Vorgänge, die noch warten (Texterkennung, Abruf, Verortung), je Grund
    wartend: Counter[str] = field(default_factory=Counter)
    #: Kennung, Vorlagen-Nr. und Titel der geplanten Vorgänge (Ausgabe des Befehls)
    vorgaenge: list[tuple[str, str, str]] = field(default_factory=list)


def _planen(
    endpunkt: ClaudeEndpunkt,
    provider: Any,
    kandidaten: list[Any],
    *,
    rest: Decimal,
    mit_verortung: set[Any],
    hoechstens: int,
) -> _Plan:
    """
    Anfragen für die Kandidaten, höchstens ``hoechstens`` und nur so viele, wie ``rest`` (USD) nach der Schätzung
    trägt. Vorgänge mit Zusammenfassung sind veraltet (``summary_store.veraltete``): Sie ersetzen sie
    (``ersetzt`` mit dem alten Stand) und prüfen die Verortung nicht erneut, die Dokumente sind dieselben.
    """
    from insight_ai.services.summarizer import SummaryService
    from insight_core.services import summary_guard, summary_store, verortung_pruefung

    dienst = SummaryService(provider=provider)
    grenze = provider.max_output_tokens
    plan = _Plan()
    gesehen: set[str] = set()
    for vorgang in kandidaten:
        if len(plan.anfragen) >= hoechstens:
            break
        custom_id = str(vorgang.pk)
        if custom_id in gesehen:
            continue
        gesehen.add(custom_id)
        if cache.get(_pause_schluessel(vorgang.pk)) or summary_guard.has_no_text(vorgang.pk):
            continue
        if vorgang.withdrawn_by_publisher or dienst._downloads_disabled(vorgang):
            continue
        ersetzt = (vorgang.summary_stand or "") if vorgang.summary is not None else None
        pruefen = ersetzt is None and vorgang.body_id in mit_verortung
        grund = summary_store.tagesbatch_wartet(vorgang, verortung=pruefen)
        if grund:
            plan.wartend[grund] += 1
            continue
        orte = verortung_pruefung.orte_fuer_pruefung(vorgang) if pruefen else None
        vorbereitung = dienst.vorbereiten(vorgang, orte=orte, einfach=True)
        if vorbereitung is None:
            continue
        grenze_anfrage = grenze + ZUSATZ_AUSGABE_EINFACH + (ZUSATZ_AUSGABE_VERORTUNG if orte is not None else 0)
        betrag = kosten.schaetzung(
            endpunkt.modell, zeichen=vorbereitung.zeichen, max_ausgabe=grenze_anfrage, batch=True
        )
        if plan.geschaetzt + betrag > rest:
            logger.info("Zusammenfassungs-Batch: Budget reicht nur für %d Anfragen", len(plan.anfragen))
            break
        plan.anfragen.append((custom_id, vorbereitung.nachrichten, grenze_anfrage, vorbereitung.schema))
        eintrag: dict[str, Any] = {
            "vorgang": custom_id,
            "dateien": [str(i) for i in vorbereitung.datei_ids],
            "stand": vorbereitung.stand,
            # Prompt-Fassung beim Einreichen (Issue #1271); ein Deploy bis zur Übernahme ändert sie nicht
            "fassung": PROMPT_FASSUNG,
        }
        if vorbereitung.schema is not None:
            eintrag["json"] = True
        if vorbereitung.orte_ids is not None:
            eintrag["orte"] = vorbereitung.orte_ids
        if ersetzt is not None:
            eintrag["ersetzt"] = ersetzt
        plan.eintraege[custom_id] = eintrag
        plan.vorgaenge.append((custom_id, vorgang.reference or "", vorgang.name or ""))
        plan.geschaetzt += betrag
    return plan


def _abschicken(endpunkt: ClaudeEndpunkt, provider: Any, plan: _Plan, beginn: Any) -> int:
    """Reicht die Anfragen als einen Batch ein und legt ihn an; Rückgabe: Zahl der Anfragen (0: gescheitert)."""
    # Das Einreichen kann dauern (bis zu einigen MB): Datenbankverbindung solange an den Pool zurückgeben
    release_idle_thread_connections()
    try:
        batch_id = provider.batch_einreichen(plan.anfragen)
    except KiAnbieterError as fehler:
        logger.warning("Zusammenfassungs-Batch nicht eingereicht: %s", fehler)
        return 0
    logger.info(
        "Zusammenfassungs-Batch %s eingereicht: %d Anfragen, Modell %s, geschätzt höchstens %s USD",
        batch_id,
        len(plan.anfragen),
        endpunkt.modell,
        plan.geschaetzt.quantize(Decimal("0.0001")),
    )
    try:
        ZusammenfassungsBatch.objects.create(
            batch_id=batch_id,
            modell=endpunkt.modell,
            eingereicht_am=beginn,
            anfragen=plan.eintraege,
            anzahl=len(plan.anfragen),
            geschaetzt_usd=plan.geschaetzt,
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
    return len(plan.anfragen)


@dataclass(frozen=True)
class Nachzug:
    """Ergebnis von ``nachziehen``: geplante bzw. eingereichte Anfragen mit Schätzung, sonst der Grund."""

    #: Gefundene Vorgänge mit veralteter Zusammenfassung (höchstens die Obergrenze)
    gefunden: int = 0
    #: Davon im Batch (geplant bzw. eingereicht)
    anfragen: int = 0
    geschaetzt_usd: Decimal = Decimal(0)
    eingereicht: bool = False
    #: Warum nichts eingereicht wurde (leer: eingereicht oder nur gezählt)
    grund: str = ""
    #: Vorgänge, die noch auf ihren Text warten, je Grund
    wartend: dict[str, int] = field(default_factory=dict)
    #: Kennung, Vorlagen-Nr. und Titel der Vorgänge im Batch
    vorgaenge: list[tuple[str, str, str]] = field(default_factory=list)


def nachziehen(
    *,
    limit: int,
    kommunen: Mapping[Any, date | None] | None = None,
    ab: date | None = None,
    hoechstens_usd: Decimal | None = None,
    ausfuehren: bool = False,
    provider: Any = None,
) -> Nachzug:
    """
    Veraltete Zusammenfassungen im Bestand neu erzeugen (Issue #1257, ``manage.py zusammenfassungen_nachziehen``):
    ein Message Batch je Lauf mit höchstens ``limit`` (und ``MAX_JE_BATCH``) Vorgängen, neueste zuerst.

    Gedrosselt wie der Tagesbatch: nur mit eingerichtetem und eingeschaltetem Batch, nie neben einem offenen Batch
    (der nächste Lauf erst nach dessen Übernahme), höchstens so viel, wie das Monatsbudget und ``hoechstens_usd``
    nach der Schätzung tragen. Ohne ``ausfuehren`` wird nur geplant und geschätzt, nichts eingereicht.
    """
    from insight_core.services import summary_store

    endpunkt = claude_fuer_zusammenfassung()
    if endpunkt is None or not endpunkt.batch:
        return Nachzug(grund="Claude für die Zusammenfassungen oder der Batch ist nicht eingeschaltet.")
    obergrenze = max(0, min(limit, MAX_JE_BATCH))
    if ausfuehren and not cache.add(SPERRE, 1, timeout=SPERRE_SEKUNDEN):
        return Nachzug(grund="Ein anderer Lauf reicht gerade ein.")
    try:
        offen = _batch_offen()
        if ausfuehren and offen:
            return Nachzug(grund="Ein Batch ist noch offen; erst nach seiner Übernahme kommt der nächste.")
        rest = kosten.budget_rest(endpunkt)
        if hoechstens_usd is not None:
            rest = min(rest, hoechstens_usd)
        if rest <= 0:
            return Nachzug(grund="Budget ausgeschöpft.")
        provider = provider or ClaudeZusammenfassung(endpunkt)
        beginn = timezone.now()
        kandidaten = summary_store.veraltete(
            kommunen, limit=obergrenze, ab=ab, ohne=zusammenfassung_pruefung.verworfene_vorgaenge()
        )
        plan = _planen(endpunkt, provider, kandidaten, rest=rest, mit_verortung=set(), hoechstens=obergrenze)
        ergebnis = Nachzug(
            gefunden=len(kandidaten),
            anfragen=len(plan.anfragen),
            geschaetzt_usd=plan.geschaetzt,
            wartend=dict(plan.wartend),
            vorgaenge=plan.vorgaenge,
            grund="Ein Batch ist noch offen; eingereicht wird erst nach seiner Übernahme." if offen else "",
        )
        if not ausfuehren or not plan.anfragen:
            return ergebnis
        if not _abschicken(endpunkt, provider, plan, beginn):
            return replace(ergebnis, grund="Einreichen gescheitert (siehe Protokoll).")
        return replace(ergebnis, eingereicht=True)
    finally:
        if ausfuehren:
            cache.delete(SPERRE)


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


#: Ergebnis der Prüfung, wenn das Material eines Vorgangs nicht mehr vorliegt (nicht speichern, nicht protokollieren)
NICHT_PRUEFBAR: Final = zusammenfassung_pruefung.Pruefung(
    befund=zusammenfassung_pruefung.Befund("quelle", "Material nicht mehr vorhanden")
)


@dataclass(frozen=True)
class _Geprueft:
    """Ergebnis der mechanischen Prüfung eines Batch-Ergebnisses mit der Kommune des Vorgangs (Protokoll)."""

    pruefung: zusammenfassung_pruefung.Pruefung
    kommune_id: Any = None


def _pruefen(batch: ZusammenfassungsBatch, ergebnisse: list[BatchErgebnis], provider: Any) -> dict[str, _Geprueft]:
    """
    Mechanische Prüfung der Ergebnisse eines Batches (Issue #1271) je ``custom_id``, vor der Transaktion der
    Übernahme: Das Material (Metadaten, Beratungsverlauf, Dokumente) wird je Vorgang neu zusammengestellt wie beim
    Einreichen (``SummaryService.vorbereiten``). Fehlt es inzwischen, gilt das Ergebnis als nicht prüfbar.
    """
    from insight_ai.services.summarizer import SummaryService
    from insight_core.services import summary_store

    texte: dict[str, tuple[str, str, str]] = {}
    for ergebnis in ergebnisse:
        eintrag = batch.anfragen.get(ergebnis.custom_id) or {}
        if not eintrag.get("vorgang"):
            continue
        text, einfach = zerlegen(ergebnis.text, json_pflicht=bool(eintrag.get("json")))
        if text:
            texte[ergebnis.custom_id] = (str(eintrag["vorgang"]), text, einfach)
    if not texte:
        return {}
    dienst = SummaryService(provider=provider)
    vorgaenge = {str(v.pk): v for v in summary_store.vorgaenge(kennung for kennung, _t, _e in texte.values())}
    pruefungen: dict[str, _Geprueft] = {}
    for custom_id, (kennung, text, einfach) in texte.items():
        vorgang = vorgaenge.get(kennung)
        vorbereitung = dienst.vorbereiten(vorgang, einfach=bool(einfach)) if vorgang is not None else None
        if vorgang is None or vorbereitung is None:
            pruefungen[custom_id] = _Geprueft(NICHT_PRUEFBAR)
            continue
        pruefung = zusammenfassung_pruefung.pruefen_beide(text, einfach, vorbereitung.pruefquelle())
        pruefungen[custom_id] = _Geprueft(pruefung, vorgang.body_id)
    return pruefungen


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
    """
    Ergebnisse eines Batches übernehmen, genau einmal (Zeilensperre, Status). Geprüft wird vorher, außerhalb der
    Transaktion (``_pruefen``); ein Ergebnis ohne Prüfung gilt als nicht prüfbar und wird nicht gespeichert.
    """
    from insight_core.services import summary_store

    vorab = ZusammenfassungsBatch.objects.filter(pk=batch_pk, status=ZusammenfassungsBatch.Status.EINGEREICHT).first()
    if vorab is None:
        return 0
    geprueft_je_anfrage = _pruefen(vorab, ergebnisse, provider)

    with transaction.atomic():
        batch = ZusammenfassungsBatch.objects.select_for_update().get(pk=batch_pk)
        if batch.status != ZusammenfassungsBatch.Status.EINGEREICHT:
            return 0
        endpunkt = getattr(provider, "endpunkt", None)
        gespeichert = verworfen = fehlgeschlagen = geprueft_verworfen = 0
        summe = Decimal(0)
        pruefungen: list[tuple[str, list[str], list[dict[str, Any]], list[dict[str, Any]]]] = []
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
            text, einfach = zerlegen(ergebnis.text, json_pflicht=bool(eintrag.get("json")))
            pruefung = _verortung(ergebnis.text) if text and "orte" in eintrag else None
            if not text:
                fehlgeschlagen += 1
                if ergebnis.art in PAUSE_NACH:
                    cache.set(_pause_schluessel(vorgang_id), 1, timeout=FEHLER_PAUSE_SEKUNDEN)
                logger.info(
                    "Zusammenfassungs-Batch %s: Vorgang %s ohne Ergebnis (%s)",
                    batch.batch_id,
                    vorgang_id,
                    ergebnis.fehler or ergebnis.art or "Antwort nicht lesbar",
                )
                continue
            hinweis = (
                replace(endpunkt, modell=modell).zusammenfassung_hinweis if isinstance(endpunkt, ClaudeEndpunkt) else ""
            )
            if hinweis and pruefung is not None:
                hinweis += HINWEIS_VERORTUNG
            # Mechanische Prüfung (Issue #1271): Verworfenes wird protokolliert und nicht gespeichert. Die
            # Prompt-Fassung stammt aus dem Einreichen (Batches von vor #1271: 0)
            geprueft = geprueft_je_anfrage.get(ergebnis.custom_id) or _Geprueft(NICHT_PRUEFBAR)
            kommune_id = geprueft.kommune_id
            fassung = int(eintrag.get("fassung") or 0)
            if geprueft.pruefung.befund is not None:
                verworfen += 1
                if geprueft.pruefung is not NICHT_PRUEFBAR:
                    geprueft_verworfen += 1
                    zusammenfassung_pruefung.protokollieren(
                        vorgang_id,
                        kommune_id,
                        text,
                        geprueft.pruefung.befund,
                        weg="batch",
                        hinweis=hinweis,
                        batch_id=batch.batch_id,
                        prompt_fassung=fassung,
                    )
                continue
            if geprueft.pruefung.befund_einfach is not None:
                zusammenfassung_pruefung.protokollieren(
                    vorgang_id,
                    kommune_id,
                    einfach,
                    geprueft.pruefung.befund_einfach,
                    weg="batch",
                    teil="einfache_sprache",
                    hinweis=hinweis,
                    batch_id=batch.batch_id,
                    prompt_fassung=fassung,
                )
                einfach = ""
            if summary_store.speichern(
                vorgang_id,
                text,
                hinweis,
                seit=batch.eingereicht_am,
                dateien=eintrag.get("dateien") or [],
                einfach=einfach,
                stand=str(eintrag.get("stand") or ""),
                ersetzt=eintrag.get("ersetzt"),
                prompt_fassung=fassung,
            ):
                gespeichert += 1
                if pruefung is not None:
                    pruefungen.append((vorgang_id, list(eintrag.get("orte") or []), *pruefung))
            else:
                verworfen += 1
        batch.status = ZusammenfassungsBatch.Status.ABGEHOLT
        batch.abgeholt_am = timezone.now()
        batch.gespeichert, batch.verworfen, batch.fehlgeschlagen = gespeichert, verworfen, fehlgeschlagen
        batch.kosten_usd = summe
        batch.save(update_fields=["status", "abgeholt_am", "gespeichert", "verworfen", "fehlgeschlagen", "kosten_usd"])
    logger.info(
        "Zusammenfassungs-Batch %s übernommen: %d gespeichert, %d verworfen (davon %d von der Prüfung), "
        "%d ohne Ergebnis, Kosten %s USD",
        batch.batch_id,
        gespeichert,
        verworfen,
        geprueft_verworfen,
        fehlgeschlagen,
        summe.quantize(Decimal("0.0001")),
    )
    _verortung_anwenden(batch.batch_id, pruefungen)
    return gespeichert


def _verortung(antwort: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Urteile und fehlende Orte aus der JSON-Antwort (``TAGESBATCH_SCHEMA``); unlesbar: ``None``."""
    try:
        daten = json.loads(antwort)
    except ValueError:
        return None
    if not isinstance(daten, dict):
        return None
    urteile = [eintrag for eintrag in daten.get("orte") or [] if isinstance(eintrag, dict)]
    fehlende = [eintrag for eintrag in daten.get("fehlende_orte") or [] if isinstance(eintrag, dict)]
    return urteile, fehlende


def _verortung_anwenden(
    batch_id: str, pruefungen: list[tuple[str, list[str], list[dict[str, Any]], list[dict[str, Any]]]]
) -> None:
    """Urteile zur Verortung übernehmen, nachdem die Zusammenfassungen gespeichert sind (Fehler je Vorgang)."""
    from insight_core.services import verortung_pruefung

    for vorgang_id, orte_ids, urteile, fehlende in pruefungen:
        try:
            verortung_pruefung.anwenden(vorgang_id, orte_ids, urteile, fehlende)
        except Exception:
            logger.exception(
                "Zusammenfassungs-Batch %s: Verortung für Vorgang %s nicht übernommen", batch_id, vorgang_id
            )
