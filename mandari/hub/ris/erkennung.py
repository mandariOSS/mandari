# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Texterkennung der RIS-Dateien aus der Ablage: der eine Weg zur Erkennung (Issue #919,
``docs/adr/20261007-dokumentkette.md``, Abschnitte 1, 2, 4 und 10).

Nur die Aufträge der Texterkennung erkennen Text. Registriert bleiben die Pfade
``insight_core.background_tasks.file_extract_text`` (ruft ``erkennen`` auf) und ``file_read_text_layer``
(``textebene_lesen``); wartende Aufträge laufen über einen Deploy weiter. Die Aufträge lesen ausschließlich über
``file_store.local_copy`` (lokal, sonst aus dem Objektspeicher) und rufen **nie** bei der Quelle ab. Der Zeitplan
``texterkennung_einplanen`` (``einplanen``), der Befehl ``extract_texts`` (``befehl_einplanen``) und
``dokumentkette nacharbeiten`` (``nacharbeiten``) planen nur ein.

**Zwei Stufen** (Issue #1107): Rund 90 % der Dateien bringen ihren Text in der Textebene mit. Der leichte Auftrag
``file.read_text_layer`` (Warteschlange ``text``, Dienst ``worker`` mit mehreren Plätzen) liest nur sie, in einem
eigenen Prozess mit Speicher- und Zeitgrenze (``mandari_dokumente.extract_text_layer_isolated``). Reicht sie
(``TextLayer.sufficient``, dieselbe Entscheidung wie in der vollständigen Kette), speichert er den Text wie die zweite
Stufe. Sonst vermerkt er den Inhalt (``text_ocr_needed_sha256``), gibt die Datei frei und reiht ``file.extract_text``
in ``ocr`` ein, solange dort Platz ist (``TEXT_EXTRACTION_QUEUE_DEPTH``); den Rest plant der Zeitplan nach. Ebenso,
wenn sein Prozess an Speicher oder Zeit scheitert: Die zweite Stufe hat mehr von beidem. Der Zeitplan füllt ``text``
mit unvermerkten Dateien (``TEXT_LAYER_QUEUE_DEPTH``) und ``ocr`` nur mit vermerkten. **Ohne Worker für ``text``**
(``apps.events.presence.queue_served``, etwa eine eigene Compose-Datei ohne ``text`` in ``--queues``) plant er wie
bisher alles in ``ocr`` ein: Dort läuft die vollständige Kette, nichts bleibt liegen. Beide Stufen werden **reihum über
die Quellen** gefüllt (``_reihum``): zuerst die Quelle mit den wenigsten Aufträgen in der Warteschlange, damit eine
große Quelle die kleinen nicht über Tage blockiert.

**Beanspruchen** (Abschnitt 2): atomar beim Start des Auftrags (``SELECT … FOR UPDATE SKIP LOCKED``), nur Dateien
mit abgelegtem Inhalt (``local_status = ok``), die nicht ausgeschlossen sind (gelöscht, gesperrt, nach #787
geleert), und nur aus ``pending`` oder aus ``completed`` mit älterer Erkennungsversion. Von zwei Aufträgen derselben
Datei erkennt einer, der andere endet ohne Wirkung. Zeitplan und Befehle beanspruchen nie: Sie reihen nur Dateien
ein, für die noch kein Auftrag wartet, Zeitplan und ``extract_texts`` (ohne ``--limit``) höchstens bis
``TEXT_EXTRACTION_QUEUE_DEPTH`` Aufträge warten, und geben liegen gebliebene Beanspruchungen frei.

**Lesen** (Abschnitte 4 und 5):

- Inhalt vorhanden: erkennen. Fehlt bei Altbestand ``sha256_hash``, berechnet der Auftrag ihn aus dem Inhalt und
  trägt ihn nach.
- Bestätigt fehlend (weder lokal noch im Objektspeicher): ``local_status = none`` (den Abruf übernimmt der
  Abrufweg), die Erkennung zurück auf den Stand vor der Beanspruchung, Ende.
- Gestört (Objektspeicher antwortet nicht, Ablage nicht eingehängt oder nicht lesbar, Platte voll; ebenso eine
  gestörte Datenbank beim Speichern und kein Platz für Zwischendateien der Erkennung): Stand vor der Beanspruchung,
  kein Versuch gezählt, derselbe Auftrag nach ``STORAGE_RETRY`` erneut. Nie ein Quellabruf.

``failed`` heißt nur noch: Inhalt nicht lesbar oder Speichergrenze. Ein Abruffehler ändert die Erkennung nie; eine
Datei, deren Abruf an der Größengrenze endet (``local_status = too_large``), wird ``skipped``.

**Obergrenze des Dokument-Caches** (#961): Ein verdrängtes Dokument (``local_status = evicted``) gilt nicht als
abgelegt. Die Erkennung beansprucht es nicht, es löst keinen Abruf aus (auch nicht über ``dokumentkette nacharbeiten``)
und zählt nicht in ``mandari_files_stored_without_text``; wartet seine Erkennung, bleibt sie, bis es ausdrücklich
wieder abgerufen wird (Vorschau, ``cache_files --verdraengte``). Solange die Erkennung wartet oder läuft (auch mit
eingereihtem Auftrag), verdrängt die Grenze die Kopie nicht (``file_cache_limit``).

**Bedingt speichern** (Abschnitt 4): ``UPDATE … WHERE id = … AND sha256_hash IS NOT DISTINCT FROM …`` (nicht über
``blob_id``, das im alten Layout leer ist), dazu: noch beansprucht und nicht ausgeschlossen – sonst schriebe ein
Auftrag den Text einer eben nach #787 geleerten Datei zurück. Hat sich der Inhalt inzwischen geändert, wird das
Ergebnis verworfen und der neue Inhalt eigens erkannt. ``text_source_sha256`` und ``text_extraction_version`` halten
fest, woraus und womit der Text stammt; ``ris.file.text_extracted`` meldet ihn mit dem SHA-256 in derselben
Transaktion.

**Neue Erkennungsversion** (``mandari_dokumente.EXTRACTION_VERSION``): Der Zeitplan plant Dateien mit älterer Version
nach den wartenden ein. Der alte Text bleibt bis zum bedingten Überschreiben stehen; liefert die Neuerkennung keinen
Text oder scheitert sie, bleibt er erledigt. Texte ohne Version (vor #919) gelten als Version 1.
``extract_texts --reprocess`` markiert erledigte Dateien mit der Version ``0`` (älter als jede) und nutzt denselben Weg.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import random
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import mandari_dokumente
from django.conf import settings
from django.db import InterfaceError, OperationalError, router, transaction
from django.db.models import BooleanField, Case, Count, F, IntegerField, Q, QuerySet, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.db.models.signals import post_save
from django.utils import timezone
from mandari_dokumente import (
    MEMORY_LIMIT_REASON,
    METHOD_NONE,
    IsolationLimits,
    OcrMemoryLimitError,
    extract_text,
    extract_text_layer_isolated,
)

from insight_core.models import OParlFile
from insight_core.services import file_store
from insight_core.services.text_extraction_job import (
    TASK_QUEUE,
    TEXT_LAYER_QUEUE,
    queued_file_ids,
    runner_is_worker,
    task_file_ids,
    waiting_tasks,
)

from . import abruf

if TYPE_CHECKING:
    from mandari_dokumente import ExtractionResult

logger = logging.getLogger(__name__)

# --- Zustände (``OParlFile.text_extraction_status``) -----------------------------------------------------
PENDING: Final = "pending"
PROCESSING: Final = "processing"
COMPLETED: Final = "completed"
FAILED: Final = "failed"
SKIPPED: Final = "skipped"

# --- Ergebniscodes des Auftrags (Protokoll und Tests) ----------------------------------------------------
ERLEDIGT: Final = "erledigt"
OHNE_TEXT: Final = "ohne_text"
GESCHEITERT: Final = "gescheitert"
UEBERSPRUNGEN: Final = "uebersprungen"
#: Ablage bzw. Objektspeicher gestört: Stand wiederhergestellt, derselbe Auftrag später erneut
ZURUECKGESTELLT: Final = "zurueckgestellt"
#: Inhalt bestätigt fehlend: ``local_status = none``, der Abruf holt ihn
OHNE_INHALT: Final = "ohne_inhalt"
#: Inhalt während der Erkennung ersetzt (oder Datei ausgeschlossen): Ergebnis verworfen
VERWORFEN: Final = "verworfen"
#: Nicht zu beanspruchen (schon beansprucht, erledigt, ausgeschlossen oder ohne abgelegten Inhalt)
NICHT_ZU_TUN: Final = "nicht_zu_tun"
#: Erste Stufe: Die Textebene reicht nicht, die Datei geht an die Erkennung aus dem Bild (``ocr``)
ZUR_TEXTERKENNUNG: Final = "zur_texterkennung"

#: Nach einer Störung der Ablage läuft derselbe Auftrag frühestens so viel später erneut
STORAGE_RETRY: Final = timedelta(minutes=15)
#: ``OSError`` mit diesen Nummern (kein Platz für Zwischendateien) ist eine Störung der Umgebung, kein unlesbarer
#: Inhalt; andere ``OSError`` der Bibliotheken (etwa „cannot identify image file“) bleiben ``failed``
_KEIN_PLATZ: Final = frozenset(n for n in (errno.ENOSPC, getattr(errno, "EDQUOT", None)) if n is not None)
#: Version, mit der ``extract_texts --reprocess`` eine Neuerkennung anfordert (älter als jede echte Version)
VERSION_REQUESTED: Final = "0"
#: Texte ohne Version (vor Issue #919) gelten als diese Version
LEGACY_VERSION: Final = 1
#: Fehlertexte, mit denen ein Abruf als Erkennungsfehler endete (Ingestor „Download failed: …“, Auftrag „Download
#: fehlgeschlagen“); ``dokumentkette nacharbeiten`` setzt diese Dateien zurück
DOWNLOAD_ERROR_PREFIX: Final = "Download"


# =============================================================================
# Einstellungen und Versionen
# =============================================================================


def _stale_after() -> timedelta:
    return timedelta(minutes=int(getattr(settings, "TEXT_EXTRACTION_STALE_MINUTES", 60)))


def _max_attempts() -> int:
    return int(getattr(settings, "TEXT_EXTRACTION_MAX_ATTEMPTS", 3))


def _max_bytes() -> int:
    return int(getattr(settings, "TEXT_EXTRACTION_MAX_SIZE_MB", 50)) * 1024 * 1024


def queue_depth() -> int:
    return int(getattr(settings, "TEXT_EXTRACTION_QUEUE_DEPTH", 20))


def text_layer_queue_depth() -> int:
    """Höchstens so viele Aufträge der ersten Stufe warten oder laufen in ``text`` (Issue #1107)."""
    return int(getattr(settings, "TEXT_LAYER_QUEUE_DEPTH", 300))


def _mixed_pages() -> bool:
    return bool(getattr(settings, "TEXT_EXTRACTION_OCR_MIXED_PAGES", True))


def _layer_limits() -> IsolationLimits:
    return IsolationLimits(memory_limit_mb=int(getattr(settings, "TEXT_LAYER_MEMORY_LIMIT_MB", 512)))


def text_stufe_aktiv() -> bool:
    """
    Bedient ein lebender Worker die Warteschlange ``text``? Sonst plant der Zeitplan wie vor Issue #1107 alles in
    ``ocr`` ein (vollständige Kette), damit ohne passenden Worker nichts liegen bleibt.
    """
    from apps.events.presence import queue_served

    return queue_served(TEXT_LAYER_QUEUE)


def backlog_alert_hours() -> float:
    """Ab so vielen Stunden ohne Text meldet die Prüfung ``dokumenttext`` einen abgelegten Inhalt."""
    return float(getattr(settings, "TEXT_EXTRACTION_BACKLOG_ALERT_HOURS", 24))


def current_version() -> int:
    """Version der Texterkennung (``mandari_dokumente.EXTRACTION_VERSION``)."""
    return int(mandari_dokumente.EXTRACTION_VERSION)


def outdated_versions() -> list[str]:
    """Gespeicherte Versionen, die älter sind als die aktuelle (``0`` = Neuerkennung angefordert)."""
    return [VERSION_REQUESTED, *(str(version) for version in range(1, current_version()))]


def outdated_q() -> Q:
    """Text aus einer älteren Erkennungsversion; ohne Version gilt Version 1."""
    bedingung = Q(text_extraction_version__in=outdated_versions())
    if current_version() > LEGACY_VERSION:
        bedingung |= Q(text_extraction_version__isnull=True) | Q(text_extraction_version="")
    return bedingung


def claimable_q() -> Q:
    """Dateien, die ein Auftrag beanspruchen darf (ADR Abschnitt 2)."""
    return (
        abruf.not_excluded_q()
        & Q(local_status=abruf.OK)
        & (Q(text_extraction_status=PENDING) | (Q(text_extraction_status=COMPLETED) & outdated_q()))
    )


def _has_text_q() -> Q:
    return Q(text_content__isnull=False) & ~Q(text_content="")


def ocr_needed_q() -> Q:
    """Die erste Stufe hat für den aktuellen Inhalt vermerkt: Die Textebene reicht nicht (Issue #1107)."""
    return Q(text_ocr_needed_sha256=F("sha256_hash"))


def _ocr_vermerkt() -> Case:
    """``ocr_needed_q`` als Wahrheitswert ohne NULL (für Filter in beide Richtungen)."""
    return Case(When(ocr_needed_q(), then=Value(True)), default=Value(False), output_field=BooleanField())


# =============================================================================
# Beanspruchen, Freigeben, Beenden
# =============================================================================


@dataclass(frozen=True)
class Beanspruchung:
    """Beanspruchte Datei und ihr Zustand der Erkennung davor (``pending`` oder ``completed``)."""

    file_id: Any
    vorher: str


def beanspruchen(file_id: Any, *, now: datetime | None = None, erste_stufe: bool = False) -> Beanspruchung | None:
    """
    Datei atomar beanspruchen (``processing``, Versuch zählt); ``None``, wenn es nichts zu tun gibt. Die erste Stufe
    (``erste_stufe``) lässt Dateien aus, deren Textebene schon als unzureichend vermerkt ist: Sie warten auf ``ocr``.
    """
    now = now or timezone.now()
    with transaction.atomic():
        auswahl = OParlFile.objects.select_for_update(skip_locked=True).filter(pk=file_id).filter(claimable_q())
        if erste_stufe:
            auswahl = auswahl.alias(ocr_vermerkt=_ocr_vermerkt()).filter(ocr_vermerkt=False)
        vorher = auswahl.values_list("text_extraction_status", flat=True).first()
        if vorher is None:
            return None
        OParlFile.objects.filter(pk=file_id).update(
            text_extraction_status=PROCESSING,
            text_extraction_attempts=F("text_extraction_attempts") + 1,
            text_extraction_started_at=now,
            updated_at=now,
        )
    return Beanspruchung(file_id, str(vorher))


def freigeben(beanspruchung: Beanspruchung) -> None:
    """Ende ohne Ergebnis: Stand vor der Beanspruchung, der Versuch zählt nicht (nur, solange noch beansprucht)."""
    OParlFile.objects.filter(pk=beanspruchung.file_id, text_extraction_status=PROCESSING).update(
        text_extraction_status=beanspruchung.vorher,
        text_extraction_attempts=Greatest(F("text_extraction_attempts") - 1, Value(0)),
        text_extraction_started_at=None,
        updated_at=timezone.now(),
    )


def _beenden(beanspruchung: Beanspruchung, status: str, fehler: str) -> None:
    """
    Ende ohne Text (übersprungen, gescheitert). Bei einer Neuerkennung bleibt der bisherige Text erledigt, und die
    aktuelle Version gilt als versucht – sonst liefe dieselbe Datei immer wieder.
    """
    werte: dict[str, Any] = {
        "text_extraction_status": status,
        "text_extraction_error": fehler.replace("\x00", "")[:500],
        "text_extraction_attempts": 0,
        "text_extraction_started_at": None,
        "updated_at": timezone.now(),
    }
    if beanspruchung.vorher == COMPLETED:
        werte["text_extraction_status"] = COMPLETED
        werte["text_extraction_version"] = str(current_version())
    OParlFile.objects.filter(pk=beanspruchung.file_id, text_extraction_status=PROCESSING).update(**werte)


def release_stale(now: datetime | None = None) -> tuple[int, int]:
    """
    Liegen gebliebene Beanspruchungen freigeben (Worker beendet, wie ``release_stale_extractions`` im Ingestor):
    länger als ``TEXT_EXTRACTION_STALE_MINUTES`` in ``processing`` → zurück; nach ``TEXT_EXTRACTION_MAX_ATTEMPTS``
    begonnenen, nie beendeten Versuchen aufgegeben („Speichergrenze“), mit dem Zeitpunkt in ``text_extracted_at``.
    Dateien mit Text (Neuerkennung) gehen zurück auf ``completed`` und behalten ihn. Dateien mit wartendem Auftrag
    bleiben unberührt. Rückgabe: (zurück, aufgegeben).
    """
    now = now or timezone.now()
    cutoff = now - _stale_after()
    stale = Q(text_extraction_status=PROCESSING) & (
        Q(text_extraction_started_at__lt=cutoff) | Q(text_extraction_started_at__isnull=True, updated_at__lt=cutoff)
    )
    # Erst die (wenigen) liegen gebliebenen Dateien, dann nur gegen sie die wartenden Aufträge: keine Liste aller
    # wartenden Aufträge in den UPDATEs (NOT IN), und ohne liegen gebliebene Dateien keine Abfrage des Journals
    kandidaten = list(OParlFile.objects.filter(stale).values_list("pk", flat=True))
    if not kandidaten:
        return 0, 0
    eingereiht = set(queued_file_ids())
    # Eingereiht, aber noch nicht begonnen: kein Abbruch, sonst entstünde ein zweiter Auftrag je Datei
    offen = [pk for pk in kandidaten if str(pk) not in eingereiht]
    if not offen:
        return 0, 0
    stale &= Q(pk__in=offen)
    maximum = _max_attempts()
    grund = f"{MEMORY_LIMIT_REASON}: Bearbeitung {maximum}-mal abgebrochen (Worker beendet)"
    neu_aufgegeben = OParlFile.objects.filter(stale & _has_text_q(), text_extraction_attempts__gte=maximum).update(
        text_extraction_status=COMPLETED,
        text_extraction_error=f"{grund}, der bisherige Text bleibt",
        text_extraction_attempts=0,
        text_extraction_started_at=None,
        text_extraction_version=str(current_version()),
        updated_at=now,
    )
    neu_zurueck = OParlFile.objects.filter(stale & _has_text_q()).update(
        text_extraction_status=COMPLETED, text_extraction_started_at=None, updated_at=now
    )
    aufgegeben = OParlFile.objects.filter(stale, text_extraction_attempts__gte=maximum).update(
        text_extraction_status=FAILED,
        text_extraction_error=grund,
        text_extraction_started_at=None,
        text_extracted_at=now,
        updated_at=now,
    )
    zurueck = OParlFile.objects.filter(stale).update(
        text_extraction_status=PENDING, text_extraction_started_at=None, updated_at=now
    )
    if zurueck or neu_zurueck:
        logger.warning("Texterkennung: %d abgebrochene Dateien zurückgestellt", zurueck + neu_zurueck)
    if aufgegeben or neu_aufgegeben:
        logger.error(
            "Texterkennung: %d Dateien nach %d Abbrüchen aufgegeben (%s)",
            aufgegeben + neu_aufgegeben,
            maximum,
            MEMORY_LIMIT_REASON,
        )
    return zurueck + neu_zurueck, aufgegeben + neu_aufgegeben


def skip_too_large(now: datetime | None = None) -> int:
    """Wartende Dateien, deren Abruf an der Größengrenze endete (``too_large``): ``skipped`` (ADR Abschnitt 4)."""
    return OParlFile.objects.filter(text_extraction_status=PENDING, local_status=abruf.TOO_LARGE).update(
        text_extraction_status=SKIPPED,
        text_extraction_error="File too large: über der Größengrenze der Ablage (FILE_CACHE_MAX_MB)",
        updated_at=now or timezone.now(),
    )


# =============================================================================
# Einplanen (Zeitplan ``texterkennung_einplanen``, ohne Beanspruchung)
# =============================================================================


def candidates(
    limit: int,
    *,
    base: QuerySet[Any] | None = None,
    ocr_vermerkt: bool | None = None,
    belegt: Counter[Any] | None = None,
) -> list[Any]:
    """
    Dateien, die ein Auftrag beanspruchen könnte und für die noch keiner wartet (in keiner der beiden Stufen), reihum
    über die Quellen (``_reihum``); je Quelle wartende vor der Neuerkennung, je die neuesten zuerst. ``ocr_vermerkt``:
    nur Dateien mit (``True``, zweite Stufe) bzw. ohne (``False``, erste Stufe) Vermerk, ``None`` alle. ``belegt``:
    Aufträge je Quelle, die in der Ziel-Warteschlange schon warten oder laufen.
    """
    if limit <= 0:
        return []
    qs = (base if base is not None else OParlFile.objects.all()).filter(claimable_q())
    eingereiht = queued_file_ids()
    if eingereiht:
        qs = qs.exclude(pk__in=eingereiht)
    if ocr_vermerkt is not None:
        qs = qs.alias(ocr_vermerkt=_ocr_vermerkt()).filter(ocr_vermerkt=ocr_vermerkt)
    quellen = list(qs.order_by().values_list("body__source_id", flat=True).distinct())
    vorrang = Case(When(text_extraction_status=PENDING, then=Value(0)), default=Value(1), output_field=IntegerField())
    reihen: dict[Any, deque[Any]] = {}
    for quelle in quellen:
        je_quelle = qs.filter(body__source_id=quelle) if quelle is not None else qs.filter(body__source__isnull=True)
        reihen[quelle] = deque(
            je_quelle.annotate(erkennung_vorrang=vorrang)
            .order_by("erkennung_vorrang", "-created_at")
            .values_list("pk", flat=True)[:limit]
        )
    return _reihum(reihen, limit, belegt or Counter())


def _reihum(reihen: dict[Any, deque[Any]], limit: int, belegt: Counter[Any]) -> list[Any]:
    """
    Reihum über die Quellen (Issue #1107): Jeder Platz geht an die Quelle mit den wenigsten Aufträgen in der
    Warteschlange (``belegt`` plus die eben vergebenen), bei Gleichstand in zufälliger Reihenfolge. So bekommt jede
    Quelle mit Arbeit ihren Anteil, auch wenn weniger Plätze frei sind, als Quellen warten, und eine große Quelle
    blockiert die kleinen nicht.
    """
    reihenfolge = list(reihen)
    random.shuffle(reihenfolge)  # noqa: S311 - nur die Reihenfolge bei Gleichstand, kein Geheimnis
    zaehler = Counter({quelle: belegt.get(quelle, 0) for quelle in reihenfolge})
    ergebnis: list[Any] = []
    while len(ergebnis) < limit:
        offen = [quelle for quelle in reihenfolge if reihen[quelle]]
        if not offen:
            break
        quelle = min(offen, key=lambda q: zaehler[q])
        ergebnis.append(reihen[quelle].popleft())
        zaehler[quelle] += 1
    return ergebnis


def _belegung(stufe: str) -> Counter[Any]:
    """Wartende und laufende Aufträge der Warteschlange ``stufe`` je Quelle (für ``_reihum``)."""
    from apps.events.models import TaskStatus

    ids = task_file_ids([stufe], [TaskStatus.WARTEND, TaskStatus.LAEUFT])
    if not ids:
        return Counter()
    return Counter(
        dict(
            OParlFile.objects.filter(pk__in=ids)
            .order_by()
            .values("body__source_id")
            .annotate(anzahl=Count("pk"))
            .values_list("body__source_id", "anzahl")
        )
    )


def _enqueue(file_ids: list[Any], *, run_after: datetime | None = None, stufe: str = TASK_QUEUE) -> int:
    """
    Je Datei einen Auftrag ins Journal (``stufe``: ``ocr`` = ``file.extract_text``, ``text`` =
    ``file.read_text_layer``), auch wenn Aufträge sonst sofort laufen; mit ``run_after`` frühestens dann. Den Zeitpunkt
    setzt die Zeile direkt: ``Task.using(run_after=…)`` prüft gegen das konfigurierte Backend, das ihn nicht kennen
    muss.
    """
    from apps.events.models import Task as TaskRow
    from apps.events.tasks_backend import journal_backend
    from insight_core.background_tasks import file_extract_text, file_read_text_layer

    if not file_ids:
        return 0
    auftrag = file_read_text_layer if stufe == TEXT_LAYER_QUEUE else file_extract_text
    backend = journal_backend()
    with transaction.atomic():
        for file_id in file_ids:
            ergebnis = backend.enqueue(auftrag, [str(file_id)], {})
            if run_after is not None:
                TaskRow.objects.filter(pk=ergebnis.id).update(run_after=run_after)
    return len(file_ids)


def _fuellen(stufe: str, tiefe: int, *, ocr_vermerkt: bool | None) -> int:
    """Warteschlange ``stufe`` bis ``tiefe`` wartende und laufende Aufträge auffüllen, reihum über die Quellen."""
    frei = tiefe - waiting_tasks(stufe)
    if frei <= 0:
        return 0
    eingereiht = _enqueue(candidates(frei, ocr_vermerkt=ocr_vermerkt, belegt=_belegung(stufe)), stufe=stufe)
    if eingereiht:
        logger.info("Texterkennung: %d Aufträge in %s eingereiht", eingereiht, stufe)
    return eingereiht


def einplanen(now: datetime | None = None) -> int:
    """
    Zeitplan ``texterkennung_einplanen`` (nur mit ``TEXT_EXTRACTION_RUNNER=worker``): liegen gebliebene
    Beanspruchungen freigeben, nie abgelegte zu große Dateien überspringen, dann Aufträge für Dateien ohne wartenden
    Auftrag einreihen. Mit Worker für ``text`` (Issue #1107): ``text`` mit unvermerkten Dateien bis
    ``TEXT_LAYER_QUEUE_DEPTH``, ``ocr`` mit vermerkten bis ``TEXT_EXTRACTION_QUEUE_DEPTH``; ohne ihn alles in ``ocr``.
    Rückgabe: Zahl der eingereihten Aufträge.
    """
    if not runner_is_worker():
        return 0
    from insight_core.services.document_extraction import purge_leftover_temp_files

    now = now or timezone.now()
    release_stale(now)
    skip_too_large(now)
    # Reste beendeter Prozesse (Speicherwächter, Zeitgrenze des Runners) in diesem Container
    purge_leftover_temp_files()
    if not text_stufe_aktiv():
        return _fuellen(TASK_QUEUE, queue_depth(), ocr_vermerkt=None)
    erste = _fuellen(TEXT_LAYER_QUEUE, text_layer_queue_depth(), ocr_vermerkt=False)
    return erste + _fuellen(TASK_QUEUE, queue_depth(), ocr_vermerkt=True)


# =============================================================================
# Der Auftrag ``file.extract_text``
# =============================================================================


def erkennen(file_id: str) -> str:
    """Auftrag ``file.extract_text`` für eine Datei; Rückgabe: Ergebniscode (siehe oben)."""
    beanspruchung = beanspruchen(file_id)
    if beanspruchung is None:
        return NICHT_ZU_TUN
    # text_content/raw_json sind groß und werden hier nicht gebraucht
    datei = OParlFile.objects.defer("text_content", "raw_json").filter(pk=file_id).first()
    if datei is None:
        return NICHT_ZU_TUN
    try:
        return _erkennen(datei, beanspruchung)
    except OcrMemoryLimitError as exc:
        logger.warning("Texterkennung an der Speichergrenze für Datei %s", file_id)
        # Fester Text der Bibliothek („Speichergrenze: …“), keine Inhalte
        _beenden(beanspruchung, FAILED, str(exc))
        return GESCHEITERT
    except (OperationalError, InterfaceError):
        # Datenbank gestört (etwa beim Speichern): eine Störung der Umgebung, kein unlesbarer Inhalt
        logger.warning("Texterkennung der Datei %s: Datenbank gestört, später erneut", file_id, exc_info=True)
        return _gestoert(beanspruchung)
    except Exception as exc:
        if isinstance(exc, OSError) and exc.errno in _KEIN_PLATZ:
            # Kein Platz für Zwischendateien (volles Temp-Verzeichnis): eine Störung der Umgebung
            logger.warning("Texterkennung der Datei %s: kein Platz, später erneut", file_id, exc_info=True)
            return _gestoert(beanspruchung)
        # Inhalt nicht lesbar (oder vom Inhalt abhängiger Fehler beim Speichern, etwa ungültige Zeichen): Die Datei endet
        # sauber statt in "processing" zu bleiben, wo jede Wiederholung des Auftrags als Abbruch zählte
        logger.exception("Texterkennung für Datei %s fehlgeschlagen", file_id)
        _beenden(beanspruchung, FAILED, f"Extraction failed ({type(exc).__name__})")
        return GESCHEITERT


def _inhalt(datei: OParlFile, beanspruchung: Beanspruchung, stufe: str) -> tuple[Path, str] | str:
    """
    Inhalt der beanspruchten Datei für beide Stufen: (Pfad, SHA-256) oder der Ergebniscode, mit dem der Auftrag endet
    (Bild, Video und Ton sowie zu große Dateien übersprungen, Inhalt fehlt, Ablage gestört).
    """
    mime = datei.mime_type or ""
    if mime.startswith(("image/", "video/", "audio/")):
        _beenden(beanspruchung, SKIPPED, f"Unsupported MIME type: {mime}")
        return UEBERSPRUNGEN
    try:
        # Der einzige Lesezugriff: lokal, sonst aus dem Objektspeicher (mit Hashprüfung); nie bei der Quelle
        kopie = file_store.local_copy(datei)
        if kopie.missing:
            # Bestätigt fehlend: den Abruf übernimmt der Abrufweg, die Erkennung wartet wieder
            abruf.mark_content_missing(datei)
            freigeben(beanspruchung)
            return OHNE_INHALT
        pfad = kopie.path
        if kopie.disturbed or pfad is None:
            return _spaeter(beanspruchung, stufe)
        groesse = pfad.stat().st_size
        if groesse > _max_bytes():
            # Die Ablage nimmt größere Dateien (FILE_CACHE_MAX_MB) als die Texterkennung
            _beenden(beanspruchung, SKIPPED, f"File too large: > {_max_bytes()} bytes")
            return UEBERSPRUNGEN
        return pfad, _sha256(datei, pfad)
    except OSError:
        logger.warning("Datei %s: Ablage nicht lesbar, Texterkennung später erneut", datei.pk, exc_info=True)
        return _spaeter(beanspruchung, stufe)


def _erkennen(datei: OParlFile, beanspruchung: Beanspruchung) -> str:
    from apps.common.db_connections import release_idle_thread_connections
    from insight_core.services.document_extraction import extraction_config

    gelesen = _inhalt(datei, beanspruchung, TASK_QUEUE)
    if isinstance(gelesen, str):
        return gelesen
    pfad, sha256 = gelesen
    # Die Erkennung dauert; die Datenbankverbindung geht solange an den Pool zurück
    release_idle_thread_connections()
    # Öffentliche RIS-Datei: externe Texterkennung zulässig (nur mit Endpunkt aus KI_ERLAUBTE_HOSTS, Issue #950).
    # Ohne die ausdrückliche Erlaubnis bliebe der einzige Erkennungsweg auch mit freigegebenem Endpunkt lokal.
    config = replace(extraction_config(allow_external=True), ocr_mixed_pages=_mixed_pages())
    ergebnis = extract_text(pfad, datei.mime_type or "", datei.file_name or datei.name or "", config)
    if not _speichern(datei, ergebnis, sha256):
        return VERWORFEN
    return ERLEDIGT if ergebnis.text else OHNE_TEXT


def _gestoert(beanspruchung: Beanspruchung, stufe: str = TASK_QUEUE) -> str:
    """Störung der Umgebung: wie ``_spaeter``; ist auch die Datenbank weg, gibt die Zeitgrenze die Beanspruchung frei."""
    try:
        return _spaeter(beanspruchung, stufe)
    except (OperationalError, InterfaceError):
        # Weiter gestört: Die Beanspruchung gibt der Zeitplan nach TEXT_EXTRACTION_STALE_MINUTES frei
        logger.warning("Datei %s: Beanspruchung bleibt bis zur Zeitgrenze", beanspruchung.file_id, exc_info=True)
        return ZURUECKGESTELLT


def _spaeter(beanspruchung: Beanspruchung, stufe: str = TASK_QUEUE) -> str:
    """Ablage gestört: Stand vor der Beanspruchung, derselbe Auftrag nach ``STORAGE_RETRY`` (kein Quellabruf)."""
    freigeben(beanspruchung)
    try:
        _enqueue([beanspruchung.file_id], run_after=timezone.now() + STORAGE_RETRY, stufe=stufe)
    except Exception:
        # Das Sicherheitsnetz plant die Datei ohnehin wieder ein
        logger.warning("Datei %s: Texterkennung nicht neu eingeplant", beanspruchung.file_id, exc_info=True)
    return ZURUECKGESTELLT


# =============================================================================
# Der Auftrag ``file.read_text_layer`` (erste Stufe, Issue #1107)
# =============================================================================


def textebene_lesen(file_id: str) -> str:
    """
    Auftrag ``file.read_text_layer`` für eine Datei: beanspruchen wie die zweite Stufe (nur ohne Vermerk), aus der
    Ablage lesen, die Textebene in einem eigenen Prozess lesen. Reicht sie, bedingt speichern wie ``erkennen``; sonst
    an ``ocr`` übergeben (``_zur_texterkennung``). Rückgabe: Ergebniscode.
    """
    beanspruchung = beanspruchen(file_id, erste_stufe=True)
    if beanspruchung is None:
        return NICHT_ZU_TUN
    datei = OParlFile.objects.defer("text_content", "raw_json").filter(pk=file_id).first()
    if datei is None:
        return NICHT_ZU_TUN
    try:
        return _textebene(datei, beanspruchung)
    except (OperationalError, InterfaceError):
        logger.warning("Textebene der Datei %s: Datenbank gestört, später erneut", file_id, exc_info=True)
        return _gestoert(beanspruchung, TEXT_LAYER_QUEUE)
    except Exception as exc:
        if isinstance(exc, OSError) and exc.errno in _KEIN_PLATZ:
            logger.warning("Textebene der Datei %s: kein Platz, später erneut", file_id, exc_info=True)
            return _gestoert(beanspruchung, TEXT_LAYER_QUEUE)
        # Unerwartet: Die zweite Stufe übernimmt mit der vollständigen Kette; ein unlesbarer Inhalt endet dort als
        # gescheitert. Sofort eingereiht, sonst landete die Datei ohne Vermerk wieder hier.
        logger.exception("Textebene der Datei %s nicht gelesen, weiter mit der Texterkennung", file_id)
        return _zur_texterkennung(datei, beanspruchung, datei.sha256_hash, sofort=True)


def _textebene(datei: OParlFile, beanspruchung: Beanspruchung) -> str:
    from apps.common.db_connections import release_idle_thread_connections
    from insight_core.services.document_extraction import extraction_config

    gelesen = _inhalt(datei, beanspruchung, TEXT_LAYER_QUEUE)
    if isinstance(gelesen, str):
        return gelesen
    pfad, sha256 = gelesen
    release_idle_thread_connections()
    # Nur die Textebene: kein externer Dienst, Seitengrenze und Entscheidung wie in der zweiten Stufe
    config = replace(extraction_config(), ocr_mixed_pages=_mixed_pages())
    ausgang = extract_text_layer_isolated(
        pfad, datei.mime_type or "", datei.file_name or datei.name or "", config, _layer_limits()
    )
    if ausgang.result is None:
        logger.info("Textebene der Datei %s reicht nicht (%s), weiter mit der Texterkennung", datei.pk, ausgang.reason)
        return _zur_texterkennung(datei, beanspruchung, sha256)
    if not _speichern(datei, ausgang.result, sha256):
        return VERWORFEN
    return ERLEDIGT if ausgang.result.text else OHNE_TEXT


def _zur_texterkennung(
    datei: OParlFile, beanspruchung: Beanspruchung, sha256: str | None, *, sofort: bool = False
) -> str:
    """
    Die Textebene reicht nicht: Inhalt vermerken (nur, solange er noch der aktuelle und die Datei beansprucht ist),
    freigeben (der Versuch zählt nicht) und ``file.extract_text`` in ``ocr`` einreihen, wenn dort Platz ist (bzw.
    ``sofort``); sonst plant der Zeitplan die vermerkte Datei ein.
    """
    with transaction.atomic():
        vermerkt = bool(sha256) and bool(
            OParlFile.objects.filter(pk=datei.pk, sha256_hash=sha256, text_extraction_status=PROCESSING).update(
                text_ocr_needed_sha256=sha256, updated_at=timezone.now()
            )
        )
        freigeben(beanspruchung)
        if sofort or (vermerkt and waiting_tasks(TASK_QUEUE) < queue_depth()):
            _enqueue([datei.pk], stufe=TASK_QUEUE)
    return ZUR_TEXTERKENNUNG


def _sha256(datei: OParlFile, pfad: Path) -> str:
    """SHA-256 des Inhalts; fehlt er (Altbestand), aus dem Inhalt berechnen und nachtragen (nur, wenn noch leer)."""
    if datei.sha256_hash:
        # Unverändert: Das bedingte Schreiben vergleicht mit genau diesem Wert
        return datei.sha256_hash
    digest = hashlib.sha256()
    with open(pfad, "rb") as quelle:
        for stueck in iter(lambda: quelle.read(1024 * 1024), b""):
            digest.update(stueck)
    berechnet = digest.hexdigest()
    OParlFile.objects.filter(Q(sha256_hash__isnull=True) | Q(sha256_hash=""), pk=datei.pk).update(sha256_hash=berechnet)
    return berechnet


def _speichern(datei: OParlFile, ergebnis: ExtractionResult, sha256: str) -> bool:
    """
    Ergebnis bedingt speichern: nur, wenn ``sha256`` noch der Inhalt der Datei ist, sie noch beansprucht und nicht
    ausgeschlossen ist. Mit Text meldet ``ris.file.text_extracted`` das Ergebnis in derselben Transaktion; scheitert nur
    das Ereignis, gilt das Ergebnis trotzdem (eigener Sicherungspunkt). Der Suchindex folgt nach dem Commit
    (Signal ``index_file``). Liefert eine Neuerkennung keinen Text, bleibt der bisherige stehen.
    Rückgabe: gespeichert?
    """
    from hub.ris.text_extraction import report_text_extracted

    jetzt = timezone.now()
    werte: dict[str, Any] = {
        "text_extraction_status": COMPLETED,
        "text_extraction_error": "; ".join(ergebnis.notes)[:500] or None,
        "text_extraction_attempts": 0,
        "text_extraction_started_at": None,
        "text_source_sha256": sha256,
        "text_extraction_version": str(current_version()),
        "updated_at": jetzt,
    }
    if ergebnis.page_count is not None:
        werte["page_count"] = ergebnis.page_count
    if ergebnis.text:
        werte.update(text_content=ergebnis.text, text_extraction_method=ergebnis.method or METHOD_NONE)
        werte["text_extracted_at"] = jetzt
    elif not OParlFile.objects.filter(_has_text_q(), pk=datei.pk).exists():
        werte.update(text_extraction_method=ergebnis.method or METHOD_NONE, text_extracted_at=jetzt)
    # IS NOT DISTINCT FROM: sha256_hash=<Wert> (nie leer, siehe _sha256)
    bedingung = Q(pk=datei.pk, sha256_hash=sha256, text_extraction_status=PROCESSING) & abruf.not_excluded_q()
    with transaction.atomic():
        if not OParlFile.objects.filter(bedingung).update(**werte):
            _verwerfen(datei.pk)
            return False
        for name, wert in werte.items():
            setattr(datei, name, wert)
        # Bedingtes UPDATE statt save(): Die Empfänger von post_save (Suchindex) bekommen dasselbe Signal
        post_save.send(
            sender=OParlFile,
            instance=datei,
            created=False,
            update_fields=frozenset(werte),
            raw=False,
            using=router.db_for_write(OParlFile),
        )
        if ergebnis.text:
            try:
                with transaction.atomic():
                    report_text_extracted(
                        datei.pk,
                        datei.body_id,
                        method=werte["text_extraction_method"],
                        characters=len(ergebnis.text),
                        sha256=sha256,
                    )
            except Exception:
                logger.exception("Texterkennung der Datei %s ohne Ereignis im Journal", datei.pk)
    return True


def _verwerfen(file_id: Any) -> None:
    """
    Inhalt während der Erkennung ersetzt bzw. Datei ausgeschlossen: Ergebnis verworfen. Steht die Datei noch auf
    ``processing`` (der neue Inhalt kam ohne Zurücksetzen der Erkennung), wartet sie wieder.
    """
    OParlFile.objects.filter(pk=file_id, text_extraction_status=PROCESSING).update(
        text_extraction_status=PENDING,
        text_extraction_attempts=0,
        text_extraction_started_at=None,
        updated_at=timezone.now(),
    )
    logger.info("Texterkennung der Datei %s verworfen: Inhalt inzwischen geändert oder Datei ausgeschlossen", file_id)


# =============================================================================
# Befehl ``extract_texts`` (nur einplanen)
# =============================================================================


@dataclass
class Einplanung:
    """Ergebnis von ``befehl_einplanen``."""

    #: eingereihte (bzw. im Probelauf einzureihende) Aufträge
    auftraege: int = 0
    #: für ``--reprocess`` neu angeforderte erledigte bzw. zurückgesetzte nicht erledigte Dateien
    neu_angefordert: int = 0
    zurueckgesetzt: int = 0
    #: warten auf ihren Inhalt (noch nicht abgelegt): die holt zuerst der Abruf
    ohne_inhalt: int = 0
    #: warten, sind aber von der Obergrenze verdrängt (#961): kein Abruf von selbst (``cache_files --verdraengte``)
    verdraengt: int = 0
    #: für sie wartet schon ein Auftrag
    schon_eingereiht: int = 0
    #: bereit, aber nicht eingereiht: plant der Zeitplan ``texterkennung_einplanen`` schrittweise ein
    dem_zeitplan: int = 0
    #: freie Plätze der ersten Warteschlange (``text`` mit ``TEXT_LAYER_QUEUE_DEPTH`` bzw. ohne Worker für ``text``
    #: ``ocr`` mit ``TEXT_EXTRACTION_QUEUE_DEPTH``, jeweils minus wartende und laufende Aufträge)
    frei: int = 0
    #: erste Warteschlange: ``text`` (zwei Stufen, Issue #1107) oder ``ocr``
    warteschlange: str = TASK_QUEUE
    #: davon gleich in ``ocr`` (Textebene schon als unzureichend vermerkt)
    in_ocr: int = 0
    #: ``--limit`` reiht mehr ein, als in einer Warteschlange frei ist
    ueber_frei: bool = False


def befehl_einplanen(
    *,
    body_id: Any = None,
    pdf_only: bool = False,
    reprocess: bool = False,
    limit: int = 0,
    ausfuehren: bool = True,
) -> Einplanung:
    """
    Aufträge ``file.extract_text`` für Dateien mit abgelegtem Inhalt einreihen (``extract_texts``); beansprucht und
    erkennt nicht. ``reprocess``: erledigte Dateien als „Neuerkennung angefordert“ markieren (Version ``0``, der Text
    bleibt), übersprungene und gescheiterte zurück auf ``pending``.

    Ohne ``limit`` höchstens so viele, wie in den Warteschlangen frei sind (``TEXT_EXTRACTION_QUEUE_DEPTH`` bzw.
    ``TEXT_LAYER_QUEUE_DEPTH`` minus wartende und laufende Aufträge, ADR Abschnitte 2 und 8); den Rest plant der
    Zeitplan ``texterkennung_einplanen`` schrittweise ein. Sonst flutete ein Aufruf das Journal mit einem Auftrag je
    Datei (mit ``reprocess`` praktisch dem ganzen Bestand). Ein ausdrückliches ``limit`` gilt auch über die freien
    Plätze hinaus (bewusste Vorrangentscheidung, etwa für eine Kommune). Verteilt wird wie im Zeitplan (Issue #1107):
    Mit Worker für ``text`` gehen unvermerkte Dateien an die erste Stufe und vermerkte an ``ocr``, sonst alle an ``ocr``.
    """
    basis = OParlFile.objects.filter(abruf.not_excluded_q())
    if body_id is not None:
        basis = basis.filter(body_id=body_id)
    if pdf_only:
        basis = basis.filter(Q(mime_type__icontains="pdf") | Q(file_name__iendswith=".pdf"))
    stand = Einplanung()
    with transaction.atomic():
        if reprocess:
            abgelegt = basis.filter(local_status=abruf.OK)
            erledigt = abgelegt.filter(text_extraction_status=COMPLETED).exclude(outdated_q())
            offen = abgelegt.filter(text_extraction_status__in=[FAILED, SKIPPED, "ocr_needed"])
            stand.neu_angefordert = erledigt.update(text_extraction_version=VERSION_REQUESTED)
            stand.zurueckgesetzt = offen.update(
                text_extraction_status=PENDING, text_extraction_attempts=0, updated_at=timezone.now()
            )
        wartend = basis.filter(text_extraction_status=PENDING)
        stand.verdraengt = wartend.filter(local_status=abruf.EVICTED).count()
        stand.ohne_inhalt = wartend.exclude(local_status__in=[abruf.OK, abruf.EVICTED]).count()
        eingereiht = set(queued_file_ids())
        bereit = basis.filter(claimable_q())
        stand.schon_eingereiht = bereit.filter(pk__in=eingereiht).count() if eingereiht else 0
        frei_ocr = max(0, queue_depth() - waiting_tasks(TASK_QUEUE))
        ids_text: list[Any] = []
        if text_stufe_aktiv():
            stand.warteschlange = TEXT_LAYER_QUEUE
            stand.frei = max(0, text_layer_queue_depth() - waiting_tasks(TEXT_LAYER_QUEUE))
            ids_text = candidates(limit if limit > 0 else stand.frei, base=basis, ocr_vermerkt=False)
            rest = max(0, limit - len(ids_text)) if limit > 0 else frei_ocr
            ids_ocr = candidates(rest, base=basis, ocr_vermerkt=True)
        else:
            stand.frei = frei_ocr
            ids_ocr = candidates(limit if limit > 0 else stand.frei, base=basis)
        stand.auftraege = len(ids_text) + len(ids_ocr)
        zwei_stufen = stand.warteschlange == TEXT_LAYER_QUEUE
        stand.in_ocr = len(ids_ocr) if zwei_stufen else 0
        stand.ueber_frei = len(ids_ocr) > frei_ocr or (zwei_stufen and len(ids_text) > stand.frei)
        stand.dem_zeitplan = max(0, bereit.count() - stand.schon_eingereiht - stand.auftraege)
        if ausfuehren:
            _enqueue(ids_text, stufe=TEXT_LAYER_QUEUE)
            _enqueue(ids_ocr, stufe=TASK_QUEUE)
        else:
            transaction.set_rollback(True)
    return stand


# =============================================================================
# Befehl ``dokumentkette nacharbeiten``
# =============================================================================

#: Ergebnisse je Datei der Nacharbeit
NACH_ERKENNUNG: Final = "erkennung"
NACH_ABRUF: Final = "abruf_und_erkennung"
NACH_WARTET: Final = "erkennung_wartet_auf_abruf"
NACH_UNVERAENDERT: Final = "unveraendert"
#: Von der Obergrenze verdrängt (#961): bleibt unverändert, kein Abruf (nur ausdrücklich, ``cache_files --verdraengte``)
NACH_VERDRAENGT: Final = "verdraengt_unveraendert"


@dataclass
class Nacharbeit:
    """Zahlen der Nacharbeit je Quelle (Kennung oder ``None`` ohne Quelle)."""

    je_quelle: dict[Any, Counter[str]] = field(default_factory=dict)

    def gesamt(self) -> Counter[str]:
        summe: Counter[str] = Counter()
        for zahlen in self.je_quelle.values():
            summe.update(zahlen)
        return summe


def nacharbeiten(*, ausfuehren: bool = False, chunk: int = 1000) -> Nacharbeit:
    """
    Dateien, deren Erkennung an einem Abruffehler gescheitert ist (``failed``, Fehlertext beginnt mit „Download“),
    zurück in die Kette (ADR Abschnitt 11, Etappe 1). Mit abgelegtem Inhalt: Erkennung ``pending``. Ohne: Abruf
    ``none`` (aus ``none``/``error``; ein laufender oder geplanter Abruf, eine Verweigerung, ``missing`` und
    ``too_large`` behalten ihren Zustand) und Erkennung ``pending``. Dateien, die nicht abgelegt werden (nicht
    freigegebener Altbestand, Quelle ohne Dateiabruf, Stichtag ausstehend), bleiben ohne Inhalt unverändert, ebenso
    von der Obergrenze verdrängte (``evicted``, #961): Sie zurückzusetzen hieße, sie von selbst neu abzurufen.
    Ohne ``ausfuehren`` nur zählen. Idempotent: Zurückgesetzte Dateien sind nicht mehr ``failed``.
    """
    auswahl = (
        OParlFile.objects.filter(abruf.not_excluded_q())
        .filter(text_extraction_status=FAILED, text_extraction_error__startswith=DOWNLOAD_ERROR_PREFIX)
        .select_related("body", "body__source")
        .only(
            "pk",
            "local_status",
            "created_at",
            "body__is_listed",
            "body__source_id",
            "body__source__is_active",
            "body__source__sync_config",
        )
        .order_by("pk")
    )
    stand = Nacharbeit()
    gruppen: dict[str, list[Any]] = {NACH_ERKENNUNG: [], NACH_ABRUF: [], NACH_WARTET: []}
    for datei in auswahl.iterator(chunk_size=chunk):
        if datei.local_status == abruf.OK:
            ergebnis = NACH_ERKENNUNG
        elif datei.local_status == abruf.EVICTED:
            ergebnis = NACH_VERDRAENGT
        elif not abruf.stores_file(datei):
            ergebnis = NACH_UNVERAENDERT
        elif datei.local_status in (abruf.NONE, abruf.ERROR):
            ergebnis = NACH_ABRUF
        else:
            ergebnis = NACH_WARTET
        quelle = datei.body.source_id if datei.body is not None else None
        stand.je_quelle.setdefault(quelle, Counter())[ergebnis] += 1
        if ergebnis in gruppen:
            gruppen[ergebnis].append(datei.pk)
    if not ausfuehren:
        return stand
    jetzt = timezone.now()
    for ergebnis, ids in gruppen.items():
        for start in range(0, len(ids), chunk):
            teil = ids[start : start + chunk]
            with transaction.atomic():
                if ergebnis == NACH_ABRUF:
                    OParlFile.objects.filter(pk__in=teil, local_status__in=[abruf.NONE, abruf.ERROR]).update(
                        local_status=abruf.NONE, fetch_attempts=0, fetch_next_at=None, fetch_error=""
                    )
                OParlFile.objects.filter(pk__in=teil, text_extraction_status=FAILED).update(
                    text_extraction_status=PENDING,
                    text_extraction_attempts=0,
                    text_extraction_started_at=None,
                    updated_at=jetzt,
                )
    logger.info("Dokumentkette nachgearbeitet: %s", dict(stand.gesamt()))
    return stand


# =============================================================================
# Kennzahlen und Prüfung (ADR Abschnitt 10)
# =============================================================================


def counts() -> dict[str, int]:
    """
    Abgelegte Inhalte ohne Text (Erkennung wartet oder läuft), davon mit unzureichender Textebene (warten auf die
    Erkennung aus dem Bild, Issue #1107), und mit Text aus einer älteren Erkennungsversion (Neuerkennung steht aus),
    eine Abfrage.
    """
    ohne_text = Q(text_extraction_status__in=[PENDING, PROCESSING])
    zahlen = (
        OParlFile.objects.filter(abruf.not_excluded_q(), local_status=abruf.OK)
        .filter(text_extraction_status__in=[PENDING, PROCESSING, COMPLETED])
        .aggregate(
            ohne_text=Count("pk", filter=ohne_text),
            fuer_ocr=Count("pk", filter=ohne_text & ocr_needed_q()),
            veraltet=Count("pk", filter=Q(text_extraction_status=COMPLETED) & outdated_q()),
        )
    )
    return {
        "stored_without_text": int(zahlen["ohne_text"] or 0),
        "awaiting_ocr": int(zahlen["fuer_ocr"] or 0),
        "text_outdated": int(zahlen["veraltet"] or 0),
    }


def overdue_without_text(now: datetime | None = None) -> int:
    """Abgelegte Inhalte, die länger als ``TEXT_EXTRACTION_BACKLOG_ALERT_HOURS`` auf ihren Text warten."""
    now = now or timezone.now()
    grenze = now - timedelta(hours=backlog_alert_hours())
    return (
        OParlFile.objects.filter(abruf.not_excluded_q(), local_status=abruf.OK)
        .filter(text_extraction_status__in=[PENDING, PROCESSING])
        .annotate(abgelegt=Coalesce("local_cached_at", "created_at"))
        .filter(abgelegt__lt=grenze)
        .count()
    )
