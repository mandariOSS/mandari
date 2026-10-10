# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Texterkennung der RIS-Dateien aus der Ablage: der eine Weg zur Erkennung (Issue #919,
``docs/adr/20261007-dokumentkette.md``, Abschnitte 1, 2, 4 und 10).

Nur der Auftrag ``file.extract_text`` erkennt Text. Registriert bleibt der Pfad
``insight_core.background_tasks.file_extract_text`` (wartende Aufträge laufen über einen Deploy weiter); er ruft
``erkennen`` auf. Der Auftrag liest ausschließlich über ``file_store.local_copy`` (lokal, sonst aus dem
Objektspeicher) und ruft **nie** bei der Quelle ab. Der Zeitplan ``texterkennung_einplanen`` (``einplanen``), der
Befehl ``extract_texts`` (``befehl_einplanen``) und ``dokumentkette nacharbeiten`` (``nacharbeiten``) planen nur ein.

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
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import mandari_dokumente
from django.conf import settings
from django.db import InterfaceError, OperationalError, router, transaction
from django.db.models import Case, Count, F, IntegerField, Q, QuerySet, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.db.models.signals import post_save
from django.utils import timezone
from mandari_dokumente import MEMORY_LIMIT_REASON, METHOD_NONE, OcrMemoryLimitError, extract_text

from insight_core.models import OParlFile
from insight_core.services import file_store
from insight_core.services.text_extraction_job import queued_file_ids, runner_is_worker, waiting_tasks

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


# =============================================================================
# Beanspruchen, Freigeben, Beenden
# =============================================================================


@dataclass(frozen=True)
class Beanspruchung:
    """Beanspruchte Datei und ihr Zustand der Erkennung davor (``pending`` oder ``completed``)."""

    file_id: Any
    vorher: str


def beanspruchen(file_id: Any, *, now: datetime | None = None) -> Beanspruchung | None:
    """Datei atomar beanspruchen (``processing``, Versuch zählt); ``None``, wenn es nichts zu tun gibt."""
    now = now or timezone.now()
    with transaction.atomic():
        vorher = (
            OParlFile.objects.select_for_update(skip_locked=True)
            .filter(pk=file_id)
            .filter(claimable_q())
            .values_list("text_extraction_status", flat=True)
            .first()
        )
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


def candidates(limit: int, *, base: QuerySet[Any] | None = None) -> list[Any]:
    """
    Dateien, die ein Auftrag beanspruchen könnte und für die noch keiner wartet: wartende vor der Neuerkennung, je
    die neuesten zuerst.
    """
    qs = (base if base is not None else OParlFile.objects.all()).filter(claimable_q())
    eingereiht = queued_file_ids()
    if eingereiht:
        qs = qs.exclude(pk__in=eingereiht)
    vorrang = Case(When(text_extraction_status=PENDING, then=Value(0)), default=Value(1), output_field=IntegerField())
    return list(
        qs.annotate(erkennung_vorrang=vorrang)
        .order_by("erkennung_vorrang", "-created_at")
        .values_list("pk", flat=True)[: max(0, limit)]
    )


def _enqueue(file_ids: list[Any], *, run_after: datetime | None = None) -> int:
    """
    Je Datei einen Auftrag ins Journal (Warteschlange ``ocr``), auch wenn Aufträge sonst sofort laufen; mit
    ``run_after`` frühestens dann. Den Zeitpunkt setzt die Zeile direkt: ``Task.using(run_after=…)`` prüft gegen das
    konfigurierte Backend, das ihn nicht kennen muss.
    """
    from apps.events.models import Task as TaskRow
    from apps.events.tasks_backend import journal_backend
    from insight_core.background_tasks import file_extract_text

    if not file_ids:
        return 0
    backend = journal_backend()
    with transaction.atomic():
        for file_id in file_ids:
            ergebnis = backend.enqueue(file_extract_text, [str(file_id)], {})
            if run_after is not None:
                TaskRow.objects.filter(pk=ergebnis.id).update(run_after=run_after)
    return len(file_ids)


def einplanen(now: datetime | None = None) -> int:
    """
    Zeitplan ``texterkennung_einplanen`` (nur mit ``TEXT_EXTRACTION_RUNNER=worker``): liegen gebliebene
    Beanspruchungen freigeben, nie abgelegte zu große Dateien überspringen, dann Aufträge für Dateien ohne wartenden
    Auftrag einreihen, bis ``TEXT_EXTRACTION_QUEUE_DEPTH`` warten. Rückgabe: Zahl der eingereihten Aufträge.
    """
    if not runner_is_worker():
        return 0
    from insight_core.services.document_extraction import purge_leftover_temp_files

    now = now or timezone.now()
    release_stale(now)
    skip_too_large(now)
    # Reste beendeter Prozesse (Speicherwächter, Zeitgrenze des Runners) in diesem Container
    purge_leftover_temp_files()
    frei = queue_depth() - waiting_tasks()
    if frei <= 0:
        return 0
    eingereiht = _enqueue(candidates(frei))
    if eingereiht:
        logger.info("Texterkennung: %d Aufträge eingereiht", eingereiht)
    return eingereiht


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


def _erkennen(datei: OParlFile, beanspruchung: Beanspruchung) -> str:
    from apps.common.db_connections import release_idle_thread_connections
    from insight_core.services.document_extraction import extraction_config

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
            return _spaeter(beanspruchung)
        groesse = pfad.stat().st_size
        if groesse > _max_bytes():
            # Die Ablage nimmt größere Dateien (FILE_CACHE_MAX_MB) als die Texterkennung
            _beenden(beanspruchung, SKIPPED, f"File too large: > {_max_bytes()} bytes")
            return UEBERSPRUNGEN
        sha256 = _sha256(datei, pfad)
    except OSError:
        logger.warning("Datei %s: Ablage nicht lesbar, Texterkennung später erneut", datei.pk, exc_info=True)
        return _spaeter(beanspruchung)
    # Die Erkennung dauert; die Datenbankverbindung geht solange an den Pool zurück
    release_idle_thread_connections()
    # Öffentliche RIS-Datei: externe Texterkennung zulässig (nur mit Endpunkt aus KI_ERLAUBTE_HOSTS, Issue #950).
    # Ohne die ausdrückliche Erlaubnis bliebe der einzige Erkennungsweg auch mit freigegebenem Endpunkt lokal.
    ergebnis = extract_text(pfad, mime, datei.file_name or datei.name or "", extraction_config(allow_external=True))
    if not _speichern(datei, ergebnis, sha256):
        return VERWORFEN
    return ERLEDIGT if ergebnis.text else OHNE_TEXT


def _gestoert(beanspruchung: Beanspruchung) -> str:
    """Störung der Umgebung: wie ``_spaeter``; ist auch die Datenbank weg, gibt die Zeitgrenze die Beanspruchung frei."""
    try:
        return _spaeter(beanspruchung)
    except (OperationalError, InterfaceError):
        # Weiter gestört: Die Beanspruchung gibt der Zeitplan nach TEXT_EXTRACTION_STALE_MINUTES frei
        logger.warning("Datei %s: Beanspruchung bleibt bis zur Zeitgrenze", beanspruchung.file_id, exc_info=True)
        return ZURUECKGESTELLT


def _spaeter(beanspruchung: Beanspruchung) -> str:
    """Ablage gestört: Stand vor der Beanspruchung, derselbe Auftrag nach ``STORAGE_RETRY`` (kein Quellabruf)."""
    freigeben(beanspruchung)
    try:
        _enqueue([beanspruchung.file_id], run_after=timezone.now() + STORAGE_RETRY)
    except Exception:
        # Das Sicherheitsnetz plant die Datei ohnehin wieder ein
        logger.warning("Datei %s: Texterkennung nicht neu eingeplant", beanspruchung.file_id, exc_info=True)
    return ZURUECKGESTELLT


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
    #: freie Plätze der Warteschlange ``ocr`` (``TEXT_EXTRACTION_QUEUE_DEPTH`` minus wartende und laufende Aufträge)
    frei: int = 0


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

    Ohne ``limit`` höchstens so viele, wie in der Warteschlange ``ocr`` frei sind (``TEXT_EXTRACTION_QUEUE_DEPTH``
    minus wartende und laufende Aufträge, ADR Abschnitte 2 und 8); den Rest plant der Zeitplan
    ``texterkennung_einplanen`` schrittweise ein. Sonst flutete ein Aufruf das Journal mit einem Auftrag je Datei
    (mit ``reprocess`` praktisch dem ganzen Bestand). Ein ausdrückliches ``limit`` gilt auch über die freien Plätze
    hinaus (bewusste Vorrangentscheidung, etwa für eine Kommune).
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
        stand.frei = max(0, queue_depth() - waiting_tasks())
        ids = candidates(limit if limit > 0 else stand.frei, base=basis)
        stand.auftraege = len(ids)
        stand.dem_zeitplan = max(0, bereit.count() - stand.schon_eingereiht - stand.auftraege)
        if ausfuehren:
            _enqueue(ids)
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
    Abgelegte Inhalte ohne Text (Erkennung wartet oder läuft) und mit Text aus einer älteren Erkennungsversion
    (Neuerkennung steht aus), eine Abfrage.
    """
    zahlen = (
        OParlFile.objects.filter(abruf.not_excluded_q(), local_status=abruf.OK)
        .filter(text_extraction_status__in=[PENDING, PROCESSING, COMPLETED])
        .aggregate(
            ohne_text=Count("pk", filter=Q(text_extraction_status__in=[PENDING, PROCESSING])),
            veraltet=Count("pk", filter=Q(text_extraction_status=COMPLETED) & outdated_q()),
        )
    )
    return {"stored_without_text": int(zahlen["ohne_text"] or 0), "text_outdated": int(zahlen["veraltet"] or 0)}


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
