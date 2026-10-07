# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auftrag ``file.extract_text`` (Issue #530): Texterkennung einer RIS-Datei im Worker der Anwendung.

Dieselbe Implementierung wie im OCR-Worker des Ingestors (``mandari_dokumente``, shared/), dieselben Regeln
für Abbrüche (Issue #817). Wer arbeitet, wählt ``TEXT_EXTRACTION_RUNNER``:

- ``ingestor`` (Standard): der OCR-Worker des Ingestors; hier passiert nichts.
- ``worker``: Der Zeitplan ``texterkennung_einplanen`` stellt abgebrochene Bearbeitungen zurück (bzw. gibt sie
  nach ``TEXT_EXTRACTION_MAX_ATTEMPTS`` Abbrüchen mit dem Grund „Speichergrenze“ auf), beansprucht wartende
  Dateien (``pending`` → ``processing``) und reiht je Datei einen Auftrag in die Warteschlange ``ocr`` ein,
  höchstens ``TEXT_EXTRACTION_QUEUE_DEPTH`` gleichzeitig. Der Auftrag zählt den Versuch, lädt die Datei (aus
  der Dokumentablage, sonst einmal für Ablage und Text, sonst gestreamt in eine temporäre Datei) und speichert
  das Ergebnis; ``save()`` aktualisiert den Suchindex wie bisher.

Zeitgrenze: Eine eingereihte Datei kann lange auf ihren Auftrag warten (Parallelität der Warteschlange ``ocr``,
bis zu 30 min je Auftrag). Solange ihr Auftrag wartet, gilt sie weder als abgebrochen noch als hängend
(``queued_file_ids``); maßgeblich ist der Beginn, den der Auftrag selbst setzt (``_mark_started``). Steht die
Warteschlange, meldet das die Prüfung ``rueckstau``.

Zurückstellen: Ist die Quelle gerade nicht abrufbar (robots.txt nicht erreichbar, Drossel belegt, Quelle in
Schonung), geht die Datei ohne Abbruch zurück nach ``pending``, und der Zeitplan beansprucht aus dieser Kommune
``RETRY_UNREACHABLE_SECONDS`` lang nichts (wie ``_deferred_until`` im Ingestor). Quellen in Schonung und mit
abgeschaltetem Dateiabruf beansprucht er nie. Sonst blockierten dauerhaft zurückgestellte ältere Dateien den
Kopf der Warteschlange.

Aufträge sind idempotent: Eine Datei, die nicht mehr wartet oder in Bearbeitung ist, wird übersprungen.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest
from django.utils import timezone
from mandari_dokumente import MEMORY_LIMIT_REASON, METHOD_NONE, OcrMemoryLimitError, extract_text
from mandari_oparl.robots import RETRY_UNREACHABLE_SECONDS

from ..models import OParlFile

if TYPE_CHECKING:
    from mandari_dokumente import ExtractionResult

logger = logging.getLogger(__name__)

RUNNER_WORKER: Final = "worker"
#: Importpfad des Auftrags (``insight_core.background_tasks.file_extract_text``), für den Rückstau
TASK_PATH: Final = "insight_core.background_tasks.file_extract_text"
TASK_QUEUE: Final = "ocr"
#: Zurückgestellte Kommunen (Kennung → frühestens wieder ab, Unix-Zeit), gemeinsam für alle Prozesse
DEFERRED_CACHE_KEY: Final = "texterkennung:zurueckgestellt"

#: Ergebniscodes des Auftrags (Protokoll und Tests)
ERLEDIGT: Final = "erledigt"
OHNE_TEXT: Final = "ohne_text"
GESCHEITERT: Final = "gescheitert"
UEBERSPRUNGEN: Final = "uebersprungen"
ZURUECKGESTELLT: Final = "zurueckgestellt"
NICHT_ZU_TUN: Final = "nicht_zu_tun"


def runner_is_worker() -> bool:
    return str(getattr(settings, "TEXT_EXTRACTION_RUNNER", "ingestor")).strip().lower() == RUNNER_WORKER


def _stale_after() -> timedelta:
    return timedelta(minutes=int(getattr(settings, "TEXT_EXTRACTION_STALE_MINUTES", 60)))


def _max_attempts() -> int:
    return int(getattr(settings, "TEXT_EXTRACTION_MAX_ATTEMPTS", 3))


def _max_bytes() -> int:
    return int(getattr(settings, "TEXT_EXTRACTION_MAX_SIZE_MB", 50)) * 1024 * 1024


def queued_file_ids() -> list[str]:
    """
    Dateien, deren Auftrag eingereiht ist, aber noch nicht läuft (``wartend``, auch nach einem Abbruch wieder
    eingereiht). Sie warten auf einen Platz in der Warteschlange ``ocr``, nicht auf einen toten Worker.
    """
    from apps.events.models import Task, TaskStatus

    ids: list[str] = []
    for args in Task.objects.filter(queue=TASK_QUEUE, task_path=TASK_PATH, status=TaskStatus.WARTEND).values_list(
        "args", flat=True
    ):
        werte = args.get("args") if isinstance(args, dict) else None
        if not werte:
            continue
        try:
            ids.append(str(uuid.UUID(str(werte[0]))))
        except ValueError:
            continue
    return ids


def deferred_bodies(now: float | None = None) -> list[str]:
    """Kommunen, aus denen der Zeitplan gerade nichts beansprucht (Abruf eben zurückgestellt)."""
    jetzt = time.time() if now is None else now
    try:
        stand = cache.get(DEFERRED_CACHE_KEY) or {}
    except Exception:  # noqa: BLE001 - ohne Zwischenspeicher gilt keine Kommune als zurückgestellt
        logger.warning("Zurückgestellte Kommunen der Texterkennung nicht lesbar")
        return []
    return [kennung for kennung, bis in stand.items() if bis > jetzt]


def _defer_body(body_id: Any) -> None:
    """Kommune ``RETRY_UNREACHABLE_SECONDS`` lang nicht beanspruchen (robots.txt nicht erreichbar, Drossel belegt)."""
    jetzt = time.time()
    try:
        stand = {k: v for k, v in (cache.get(DEFERRED_CACHE_KEY) or {}).items() if v > jetzt}
        stand[str(body_id)] = jetzt + RETRY_UNREACHABLE_SECONDS
        cache.set(DEFERRED_CACHE_KEY, stand, RETRY_UNREACHABLE_SECONDS)
    except Exception:  # noqa: BLE001 - ohne Zwischenspeicher kommt die Datei beim nächsten Lauf wieder dran
        logger.warning("Kommune %s für die Texterkennung nicht zurückstellbar", body_id)


def release_stale(now: datetime | None = None) -> tuple[int, int]:
    """
    Abgebrochene Bearbeitungen auflösen (wie ``release_stale_extractions`` im Ingestor): länger als
    ``TEXT_EXTRACTION_STALE_MINUTES`` in ``processing`` → zurück nach ``pending``; nach
    ``TEXT_EXTRACTION_MAX_ATTEMPTS`` begonnenen, nie beendeten Versuchen ``failed`` („Speichergrenze“), mit dem
    Zeitpunkt der Aufgabe in ``text_extracted_at``. Dateien mit wartendem Auftrag bleiben unberührt.
    Rückgabe: (zurückgestellt, aufgegeben).
    """
    now = now or timezone.now()
    cutoff = now - _stale_after()
    stale = Q(text_extraction_status="processing") & (
        Q(text_extraction_started_at__lt=cutoff) | Q(text_extraction_started_at__isnull=True, updated_at__lt=cutoff)
    )
    eingereiht = queued_file_ids()
    if eingereiht:
        # Eingereiht, aber noch nicht begonnen: kein Abbruch, sonst entstünde ein zweiter Auftrag je Datei
        stale &= ~Q(pk__in=eingereiht)
    maximum = _max_attempts()
    aufgegeben = OParlFile.objects.filter(stale, text_extraction_attempts__gte=maximum).update(
        text_extraction_status="failed",
        text_extraction_error=f"{MEMORY_LIMIT_REASON}: Bearbeitung {maximum}-mal abgebrochen (Worker beendet)",
        text_extraction_started_at=None,
        text_extracted_at=now,
        updated_at=now,
    )
    zurueck = OParlFile.objects.filter(stale).update(
        text_extraction_status="pending", text_extraction_started_at=None, updated_at=now
    )
    if zurueck:
        logger.warning("Texterkennung: %d abgebrochene Dateien zurückgestellt", zurueck)
    if aufgegeben:
        logger.error(
            "Texterkennung: %d Dateien nach %d Abbrüchen aufgegeben (%s)", aufgegeben, maximum, MEMORY_LIMIT_REASON
        )
    return zurueck, aufgegeben


def _claimable() -> Q:
    from ..models import OParlBody
    from .file_cache import backoff_failures, sources_without_downloads

    bedingung = (
        Q(text_extraction_status="pending", deleted=False, source_missing_since__isnull=True)
        & (Q(download_url__isnull=False) | Q(access_url__isnull=False))
        & (Q(size__isnull=True) | Q(size__lte=_max_bytes()))
    )
    # Nie aus Quellen in Schonung (mehrfach in Folge nicht erreicht); der Auftrag stellte ihre Dateien nur
    # zurück, und sie blockierten als älteste den Kopf der Warteschlange
    ausgeschlossen = Q(source__consecutive_failures__gte=backoff_failures())
    gesperrt = sources_without_downloads()
    if gesperrt:
        ausgeschlossen |= Q(source_id__in=gesperrt)
    zurueckgestellt = deferred_bodies()
    if zurueckgestellt:
        ausgeschlossen |= Q(pk__in=zurueckgestellt)
    # Unterabfrage statt Verknüpfung: FOR UPDATE verträgt keine äußere Verknüpfung (Kommune ist nullbar)
    return bedingung & ~Q(body_id__in=OParlBody.objects.filter(ausgeschlossen).values("pk"))


def waiting_tasks() -> int:
    """Eingereihte und laufende Aufträge ``file.extract_text`` (Rückstau der Warteschlange ``ocr``)."""
    from apps.events.models import Task, TaskStatus

    return Task.objects.filter(
        queue=TASK_QUEUE, task_path=TASK_PATH, status__in=[TaskStatus.WARTEND, TaskStatus.LAEUFT]
    ).count()


def plan(now: datetime | None = None) -> int:
    """
    Zeitplan ``texterkennung_einplanen``: zurückstellen, beanspruchen, Aufträge einreihen (nur mit
    ``TEXT_EXTRACTION_RUNNER=worker``). Beanspruchen und Einreihen in einer Transaktion: Der Auftrag existiert
    genau dann, wenn die Datei beansprucht ist. Rückgabe: Zahl der eingereihten Aufträge.
    """
    if not runner_is_worker():
        return 0
    from apps.events.tasks_backend import journal_backend

    from ..background_tasks import file_extract_text
    from .document_extraction import purge_leftover_temp_files

    now = now or timezone.now()
    release_stale(now)
    # Reste beendeter Prozesse (Speicherwächter, Zeitgrenze des Runners) in diesem Container
    purge_leftover_temp_files()
    frei = int(getattr(settings, "TEXT_EXTRACTION_QUEUE_DEPTH", 20)) - waiting_tasks()
    if frei <= 0:
        return 0
    backend = journal_backend()
    with transaction.atomic():
        ids = list(
            OParlFile.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(_claimable())
            .order_by("created_at")
            .values_list("id", flat=True)[:frei]
        )
        if not ids:
            return 0
        OParlFile.objects.filter(id__in=ids).update(
            text_extraction_status="processing", text_extraction_started_at=now, updated_at=now
        )
        for file_id in ids:
            # Immer ins Journal (Warteschlange ocr), auch wenn die Anwendung Aufträge sonst sofort ausführt
            backend.enqueue(file_extract_text, [str(file_id)], {})
    logger.info("Texterkennung: %d Aufträge eingereiht", len(ids))
    return len(ids)


def _mark_started(file_id: str) -> OParlFile | None:
    """Versuch zählen und Beginn festhalten; ``None``, wenn die Datei nicht mehr zu bearbeiten ist."""
    now = timezone.now()
    gezaehlt = OParlFile.objects.filter(
        pk=file_id, text_extraction_status__in=["pending", "processing"], deleted=False
    ).update(
        text_extraction_status="processing",
        text_extraction_attempts=F("text_extraction_attempts") + 1,
        text_extraction_started_at=now,
        updated_at=now,
    )
    if not gezaehlt:
        return None
    return OParlFile.objects.select_related("body", "body__source").filter(pk=file_id).first()


def _finish(file_id: str, status: str, error: str | None = None) -> None:
    """Bearbeitung ohne Text beendet: Zähler und Beginn zurück (wie ``update_file_text`` im Ingestor)."""
    werte: dict[str, object] = {
        "text_extraction_status": status,
        "text_extraction_attempts": 0,
        "text_extraction_started_at": None,
        "updated_at": timezone.now(),
    }
    if error is not None:
        werte["text_extraction_error"] = error.replace("\x00", "")[:500]
    OParlFile.objects.filter(pk=file_id).update(**werte)


def _defer(file: OParlFile) -> str:
    """
    Quelle gerade nicht abrufbar: Datei ohne Abbruch zurück nach ``pending`` und Kommune ruhen lassen. Der eben
    gezählte Versuch wird zurückgenommen; frühere Abbrüche bleiben (eine verdächtige Datei bleibt verdächtig).
    """
    OParlFile.objects.filter(pk=file.pk).update(
        text_extraction_status="pending",
        text_extraction_attempts=Greatest(F("text_extraction_attempts") - 1, Value(0)),
        text_extraction_started_at=None,
        updated_at=timezone.now(),
    )
    if file.body_id is not None:
        _defer_body(file.body_id)
    return ZURUECKGESTELLT


def _store_result(file: OParlFile, result: ExtractionResult, sha256: str | None) -> None:
    """
    Ergebnis über ``save()`` speichern: der Suchindex folgt wie bei ``extract_texts`` (Signal), und zwar erst nach
    dem Commit (``index_file`` mit ``transaction.on_commit``).

    Mit Text meldet ``ris.file.text_extracted`` das Ergebnis in derselben Transaktion wie im Ingestor
    (``hub.ris.text_extraction``, Issue #821). Scheitert nur das Ereignis, gilt das Ergebnis trotzdem
    (eigener Sicherungspunkt): Eine wiederholte Erkennung kostet mehr als ein fehlendes Ereignis, das der
    Vollbau des Schattenindex nachholt. Protokolliert wird nur die Kennung der Datei.
    """
    from hub.ris.text_extraction import report_text_extracted

    file.text_extraction_status = "completed"
    file.text_extraction_method = result.method or METHOD_NONE
    file.text_extraction_error = "; ".join(result.notes)[:500] or None
    file.text_extraction_attempts = 0
    file.text_extraction_started_at = None
    file.text_extracted_at = timezone.now()
    felder = [
        "text_extraction_status",
        "text_extraction_method",
        "text_extraction_error",
        "text_extraction_attempts",
        "text_extraction_started_at",
        "text_extracted_at",
        "updated_at",
    ]
    if result.page_count is not None:
        file.page_count = result.page_count
        felder.append("page_count")
    if sha256:
        file.sha256_hash = sha256
        felder.append("sha256_hash")
    if result.text:
        file.text_content = result.text
        felder.append("text_content")
    with transaction.atomic():
        file.save(update_fields=felder)
        if result.text:
            try:
                with transaction.atomic():
                    report_text_extracted(
                        file.pk, file.body_id, method=file.text_extraction_method, characters=len(result.text)
                    )
            except Exception:
                logger.exception("Texterkennung der Datei %s ohne Ereignis im Journal", file.pk)


def extract_file(file_id: str) -> str:
    """Auftrag ``file.extract_text`` für eine Datei; Rückgabe: Ergebniscode (siehe oben)."""
    from apps.common.db_connections import release_idle_thread_connections

    from . import file_cache, robots
    from .document_extraction import (
        DocumentDownloadError,
        DocumentTooLargeError,
        DownloadedFile,
        RobotsBlockedError,
        RobotsUnreachableError,
        SourceBusyError,
        extraction_config,
    )

    file = _mark_started(file_id)
    if file is None:
        return NICHT_ZU_TUN
    url = file.download_url or file.access_url
    if not url:
        _finish(file_id, "skipped", "No download URL")
        return UEBERSPRUNGEN
    if file_cache.downloads_disabled(file.body) or file_cache.source_paused(file.body):
        # Quelle gesperrt bzw. in Schonung: später erneut, kein Abbruch
        return _defer(file)
    mime = file.mime_type or ""
    if mime.startswith(("image/", "video/", "audio/")):
        _finish(file_id, "skipped", f"Unsupported MIME type: {mime}"[:500])
        return UEBERSPRUNGEN

    downloaded: DownloadedFile | None = None
    try:
        # Dokumentablage: vorhandene Kopie nutzen, sonst einmal für Ablage und Text laden (Regel der Ablage,
        # hub.ris.abruf). Bis Teil B der Dokumentkette (Issue #919) lädt der Auftrag nicht abzulegende Dateien selbst.
        local = file_cache.local_file(file)
        if local is None and file_cache.stores_file(file):
            if file_cache.fetch_and_cache(file) == "storage_error":
                # Ablage bzw. Objektspeicher gestört: kein Abruf bei der Quelle, später erneut
                return _defer(file)
            file.refresh_from_db(fields=["local_path", "sha256_hash", "local_status", "blob"])
            local = file_cache.local_file(file)
        if local is not None:
            source, sha256 = local, file.sha256_hash
        else:
            downloaded = file_cache.download_to_file(
                url,
                max_bytes=_max_bytes(),
                extra_headers=file_cache.download_headers(file.body),
                sync_config=robots.sync_config_of(file),
            )
            source, sha256 = downloaded.path, downloaded.sha256
        # Die Kopie der Ablage darf größer sein (FILE_CACHE_MAX_MB) als die Grenze der Texterkennung
        if source.stat().st_size > _max_bytes():
            _finish(file_id, "skipped", f"File too large: > {_max_bytes()} bytes")
            return UEBERSPRUNGEN
        # Die Erkennung dauert; die Datenbankverbindung geht solange an den Pool zurück
        release_idle_thread_connections()
        # Öffentliche RIS-Datei: externe Texterkennung zulässig (nur mit Endpunkt aus KI_ERLAUBTE_HOSTS)
        result = extract_text(source, mime, file.file_name or file.name or "", extraction_config(allow_external=True))
        _store_result(file, result, sha256)
    except RobotsBlockedError as exc:
        _finish(file_id, "skipped", exc.reason)
        return UEBERSPRUNGEN
    except (RobotsUnreachableError, SourceBusyError):
        return _defer(file)
    except DocumentTooLargeError:
        _finish(file_id, "skipped", f"File too large: > {_max_bytes()} bytes")
        return UEBERSPRUNGEN
    except DocumentDownloadError:
        _finish(file_id, "failed", "Download fehlgeschlagen")
        return GESCHEITERT
    except OcrMemoryLimitError as exc:
        logger.warning("Texterkennung an der Speichergrenze für Datei %s", file_id)
        # Fester Text der Bibliothek („Speichergrenze: …“), keine Inhalte
        _finish(file_id, "failed", str(exc))
        return GESCHEITERT
    except Exception as exc:
        # Auch Ablage, Nachladen und Speichern: Die Datei endet sauber statt in "processing" zu bleiben, wo
        # jede Wiederholung des Auftrags als Abbruch zählte und sie zuletzt mit falschem Grund aufgegeben würde
        logger.exception("Texterkennung für Datei %s fehlgeschlagen", file_id)
        _finish(file_id, "failed", f"Extraction failed ({type(exc).__name__})")
        return GESCHEITERT
    finally:
        if downloaded is not None:
            downloaded.discard()
    return ERLEDIGT if result.text else OHNE_TEXT
