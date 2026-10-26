"""
Text Extraction Pipeline for the Ingestor.

Lädt Dateien und erkennt ihren Text mit der gemeinsamen Texterkennung ``mandari_dokumente`` (shared/,
Issue #530): pypdf, optional Mistral, sonst Tesseract Seite für Seite mit Speicher- und Zeitgrenzen. Dieselbe
Implementierung nutzt der Auftrag ``file.extract_text`` der Anwendung; welcher Weg arbeitet, entscheidet
``TEXT_EXTRACTION_RUNNER`` (``ingestor`` = dieser Worker, Standard; ``worker`` = Aufträge der Anwendung).

Async-capable: PDF downloads via httpx, sync extraction via asyncio.to_thread().

Downloads laufen gestreamt in eine temporäre Datei, die beim Schreiben gehasht wird; die Größengrenze
greift schon während des Downloads, nie liegt eine ganze Datei im Arbeitsspeicher (Issue #788). Ist die
Dokumentablage eingerichtet (``OPARL_FILES_ROOT``), legt der Ingestor die Datei danach gleich unter
ihrem SHA-256 ab – ein Abruf je Datei für Text und Ablage.

Abbrüche (Issue #817): Jede Datei zählt ihre begonnenen Bearbeitungen. Stirbt der Worker mitten in einer
Datei, stellt die nächste Runde sie nach ``TEXT_EXTRACTION_STALE_MINUTES`` zurück; Dateien mit einem
Abbruch laufen danach einzeln und zuletzt (je Kommune), nach ``TEXT_EXTRACTION_MAX_ATTEMPTS`` Abbrüchen
gelten sie als gescheitert („Speichergrenze“) statt den Worker immer wieder zu beenden.

Beansprucht wird in kleinen Portionen (``CLAIM_PER_SLOT`` Dateien je Platz der Parallelität): Das
Beanspruchen startet die Zeitgrenze, eine beanspruchte Datei darf also nicht hinter einem ganzen Stapel
warten, sonst gälte sie als abgebrochen, obwohl der Worker lebt.

Quelle nicht erreichbar (Issue #1121): Verbindungs- und Protokollfehler, Zeitüberschreitungen sowie 408, 429 und
5xx beim Download stellen die ganze Quelle zurück (15 min, danach doppelt so lange je weiterem Fehlschlag in
Folge, höchstens ``MAX_DEFER_SECONDS``; ``Retry-After`` gilt mindestens). Die Datei bleibt ``pending``, statt mit
allen anderen Dateien der Quelle als gescheitert zu enden, solange die Quelle uns abweist. Erst nach
``MAX_TRANSIENT_FAILURES`` solchen Fehlschlägen derselben Datei in Folge gilt sie als gescheitert; je
Zurückstellung zählt nur der Fehlschlag, der sie auslöst, und der begonnene Versuch zählt nicht als Abbruch.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from mandari_dokumente import (
    MEMORY_LIMIT_REASON,
    ExtractionConfig,
    MistralConfig,
    OcrLimits,
    OcrMemoryLimitError,
    erlaubte_hosts_aus_umgebung,
    extract_text,
)
from mandari_oparl.abgleich import looks_like_html
from mandari_oparl.robots import KIND_FILES, RETRY_UNREACHABLE_SECONDS

from src.client import redirects
from src.client.host_pacing import host_pacer
from src.client.robots import robots_gate
from src.client.source_options import SourceFetchOptions
from src.config import settings

logger = logging.getLogger(__name__)

#: So lange gelten Einstellungen einer Quelle (Download-Header, robots-Ausnahme) im Zwischenspeicher je Body.
#: Der Extraktions-Worker lebt lange; eine neue Ausnahme oder ein neuer Header wirkt spätestens danach.
SOURCE_OPTIONS_TTL_SECONDS = 300.0

PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}
TEXT_MIME_TYPES = {"text/plain", "text/html"}
SUPPORTED_MIME_TYPES = PDF_MIME_TYPES | TEXT_MIME_TYPES
#: Wert von ``TEXT_EXTRACTION_RUNNER``, mit dem die Aufträge der Anwendung den Text erkennen (nicht dieser Worker)
RUNNER_WORKER = "worker"
#: Abgebrochene Bearbeitungen höchstens einmal in diesem Abstand auflösen (eine Abfrage über alle Kommunen),
#: je Speicher: Sync und Scraper legen je Kommune einen eigenen Extraktor an
RELEASE_STALE_EVERY_SECONDS = 60.0
#: Je Platz der Parallelität höchstens so viele Dateien auf einmal beanspruchen (Issue #817). Eine
#: beanspruchte Datei wartet so höchstens auf eine andere Datei ihres Platzes (``OCR_FILE_BUDGET_SECONDS``
#: plus Abruf), deutlich unter ``TEXT_EXTRACTION_STALE_MINUTES``, statt auf einen ganzen Stapel.
CLAIM_PER_SLOT = 2
#: Antworten, mit denen die Quelle gerade nicht liefert (Überlast, Wartung, Drossel): Quelle zurückstellen (#1121)
TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
#: Längster Abstand, um den ein Downloadfehler die Quelle zurückstellt (der Abstand verdoppelt sich je Fehlschlag)
MAX_DEFER_SECONDS = 6 * 60 * 60
#: Nach so vielen vorübergehenden Fehlschlägen derselben Datei in Folge gilt sie doch als gescheitert
MAX_TRANSIENT_FAILURES = 3


class FileTooLargeError(Exception):
    """Die Datei überschreitet ``TEXT_EXTRACTION_MAX_SIZE_MB`` (Abbruch während des Downloads)."""


def is_transient(error: BaseException) -> bool:
    """Liegt der Downloadfehler an der Quelle (nicht erreichbar, überlastet) statt an der Datei?"""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in TRANSIENT_STATUS_CODES
    return isinstance(error, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError)


def retry_after_seconds(error: BaseException) -> float:
    """``Retry-After`` in Sekunden aus einer Fehlerantwort; 0 ohne Angabe (HTTP-Datum zählt nicht)."""
    if not isinstance(error, httpx.HTTPStatusError):
        return 0.0
    value = (error.response.headers.get("retry-after") or "").strip()
    return float(value) if value.isascii() and value.isdigit() else 0.0


def _error_text(error: BaseException) -> str:
    """Lesbarer Fehler; Zeitüberschreitungen von httpx haben oft keinen Text."""
    return str(error) or type(error).__name__


@dataclass
class SourceBackoff:
    """
    Zurückgestellte Quellen je Body und vorübergehende Fehlschläge je Datei (Issue #1121), geteilt von allen
    Extraktoren an einem Speicher: Sync und Scraper legen je Kommune einen eigenen Extraktor an.
    """

    #: Je Body: bis zu diesem Zeitpunkt (Uhr des Extraktors) nichts beanspruchen
    until: dict[Any, float] = field(default_factory=dict)
    #: Je Body: Zurückstellungen nach Downloadfehlern in Folge (bestimmt den Abstand)
    streak: dict[Any, int] = field(default_factory=dict)
    #: Je Datei: vorübergehende Fehlschläge in Folge, höchstens einer je Zurückstellung der Quelle
    file_failures: dict[Any, int] = field(default_factory=dict)

    def hold(self, body_id: Any, until: float) -> None:
        """Quelle mindestens bis ``until`` zurückstellen (robots.txt nicht erreichbar); kürzt keine längere Frist."""
        if body_id is not None:
            self.until[body_id] = max(self.until.get(body_id, until), until)

    def deferred(self, body_id: Any, now: float) -> bool:
        """Ist die Quelle zurückgestellt? Abgelaufene Fristen verfallen."""
        until = self.until.get(body_id)
        if until is None:
            return False
        if now < until:
            return True
        del self.until[body_id]
        return False

    def defer(self, body_id: Any, now: float, minimum: float = 0.0) -> float:
        """
        Quelle nach einem Downloadfehler zurückstellen. Rückgabe: Abstand in Sekunden; 0, wenn sie schon
        zurückgestellt ist (parallele Fehlschläge derselben Runde verlängern den Abstand nicht).
        """
        if body_id is None or self.deferred(body_id, now):
            return 0.0
        streak = self.streak.get(body_id, 0) + 1
        self.streak[body_id] = streak
        seconds = min(max(RETRY_UNREACHABLE_SECONDS * 2 ** min(streak - 1, 10), minimum), MAX_DEFER_SECONDS)
        self.until[body_id] = now + seconds
        return float(seconds)

    def file_failed(self, file_id: Any) -> int:
        """Vorübergehenden Fehlschlag einer Datei zählen; Rückgabe: Fehlschläge in Folge."""
        failures = self.file_failures.get(file_id, 0) + 1
        self.file_failures[file_id] = failures
        return failures

    def succeeded(self, body_id: Any, file_id: Any) -> None:
        """Download gelungen: Die Quelle liefert wieder, Abstand und Zähler der Datei beginnen von vorn."""
        self.streak.pop(body_id, None)
        self.file_failures.pop(file_id, None)


def _backoff_of(storage: Any) -> SourceBackoff:
    """Zustand am Speicher (``extraction_backoff``); der erste Extraktor an einem Speicher legt ihn an."""
    backoff = getattr(storage, "extraction_backoff", None)
    if not isinstance(backoff, SourceBackoff):
        backoff = SourceBackoff()
        storage.extraction_backoff = backoff
    return backoff


@dataclass
class DownloadedFile:
    """Gestreamt geladene Datei in einer temporären Datei."""

    path: Path
    size: int
    sha256: str
    head: bytes

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


def blob_root() -> Path | None:
    """Wurzel der Ablage nach SHA-256 oder ``None``, wenn der Ingestor nichts ablegt."""
    root = (settings.oparl_files_root or "").strip()
    if not root or (settings.file_store_layout or "sha256").strip().lower() != "sha256":
        return None
    path = Path(root)
    return path / "sha256" if path.is_dir() else None


class TextExtractor:
    """
    Async text extraction pipeline for OParl files.

    Downloads files and extracts text content, then updates the database
    with the results.
    """

    def __init__(self, storage, clock: Callable[[], float] = time.monotonic) -> None:
        """
        Args:
            storage: DatabaseStorage instance for querying/updating files.
            clock: Uhr für Zwischenspeicher und Zurückstellen (Tests)
        """
        self.storage = storage
        self.max_size_bytes = settings.text_extraction_max_size_mb * 1024 * 1024
        self.concurrency = settings.text_extraction_concurrency
        self.timeout = settings.text_extraction_timeout
        self.batch_size = settings.text_extraction_batch_size
        self._clock = clock
        # Je Body: (Wert, geladen um); gültig SOURCE_OPTIONS_TTL_SECONDS
        self._header_cache: dict[Any, tuple[dict[str, str], float]] = {}
        self._options_cache: dict[Any, tuple[SourceFetchOptions, float]] = {}
        # Zurückgestellte Bodies (robots.txt oder Quelle nicht erreichbar), geteilt mit den anderen Extraktoren
        # am selben Speicher (Issue #1121)
        self._backoff = _backoff_of(storage)

    async def release_stale(self) -> tuple[int, int]:
        """
        Abgebrochene Bearbeitungen (Worker beendet) zurückstellen bzw. nach zu vielen Abbrüchen aufgeben,
        höchstens alle ``RELEASE_STALE_EVERY_SECONDS`` je Speicher (``stale_extractions_released_at``), auch
        wenn Sync und Scraper je Kommune einen eigenen Extraktor anlegen. Rückgabe: (zurückgestellt, aufgegeben).
        """
        release = getattr(self.storage, "release_stale_extractions", None)
        if release is None:
            return 0, 0
        now = self._clock()
        last = getattr(self.storage, "stale_extractions_released_at", None)
        if last is not None and now - last < RELEASE_STALE_EVERY_SECONDS:
            return 0, 0
        self.storage.stale_extractions_released_at = now
        try:
            zurueck, aufgegeben = await release(
                stale_after=timedelta(minutes=settings.text_extraction_stale_minutes),
                max_attempts=settings.text_extraction_max_attempts,
            )
        except Exception as e:  # noqa: BLE001 - die Extraktion läuft auch ohne Auflösen weiter
            logger.warning("Abgebrochene Textextraktionen nicht auflösbar: %s", e)
            return 0, 0
        if zurueck:
            logger.warning("Textextraktion: %d abgebrochene Dateien zurückgestellt (Worker beendet)", zurueck)
        if aufgegeben:
            logger.error(
                "Textextraktion: %d Dateien nach %d Abbrüchen aufgegeben (%s)",
                aufgegeben,
                settings.text_extraction_max_attempts,
                MEMORY_LIMIT_REASON,
            )
        return zurueck, aufgegeben

    async def extract_pending_files(self, body_id: UUID) -> int:
        """
        Download and extract text from all pending files for a body.

        Args:
            body_id: The body UUID to process files for.

        Returns:
            Number of files successfully extracted.
        """
        # Die Aufträge der Anwendung erkennen den Text (Issue #530): kein Doppelbetrieb, hier nichts beanspruchen
        if runs_elsewhere():
            logger.debug("Texterkennung über Aufträge der Anwendung (TEXT_EXTRACTION_RUNNER=worker)")
            return 0
        # Abgebrochene Bearbeitungen aller Kommunen auflösen (höchstens einmal je Minute, Issue #817)
        await self.release_stale()
        # Quelle liefert Dokumente nur hinter einer Zugangsprüfung für Menschen (sync_config["file_downloads"]):
        # nichts beanspruchen, die Dateien bleiben "pending" und werden nachgeholt, sobald der Schalter fällt
        if not await self._file_downloads_enabled(body_id):
            logger.debug("Dateiabruf für Body %s abgeschaltet (sync_config der Quelle)", body_id)
            return 0
        # Die robots.txt der Quelle war eben nicht erreichbar: Dateien bleiben "pending", bis der neue Versuch
        # fällig ist, statt sie in jeder Runde zu beanspruchen und wieder zurückzustellen
        if self._deferred(body_id):
            logger.debug("Body %s zurückgestellt (robots.txt nicht erreichbar)", body_id)
            return 0

        # Höchstens batch_size Dateien je Aufruf, beansprucht in kleinen Portionen direkt vor der Bearbeitung:
        # Das Beanspruchen startet die Zeitgrenze für abgebrochene Bearbeitungen (Issue #817)
        claimed = 0
        extracted = 0
        portion = max(1, self.concurrency) * CLAIM_PER_SLOT
        # 1. Dateien ohne Abbruch, parallel
        while claimed < self.batch_size and not self._deferred(body_id):
            requested = min(portion, self.batch_size - claimed)
            files = await self.storage.get_pending_files(
                body_id=body_id,
                batch_size=requested,
                max_size_bytes=self.max_size_bytes,
                retried=False,
            )
            if not files:
                break
            claimed += len(files)
            extracted += await self._process_claimed(files)
            if len(files) < requested:
                break  # keine weiteren wartenden Dateien ohne Abbruch
        # 2. Dateien, deren Bearbeitung schon einmal abbrach: zuletzt, einzeln beansprucht und allein
        # bearbeitet. Stirbt der Worker wieder, zählt der Abbruch nur bei der Datei, die ihn auslöst.
        while claimed < self.batch_size and not self._deferred(body_id):
            files = await self.storage.get_pending_files(
                body_id=body_id, batch_size=1, max_size_bytes=self.max_size_bytes, retried=True
            )
            if not files:
                break
            claimed += len(files)
            extracted += await self._process_claimed(files)

        if claimed:
            logger.info("Extracted text from %d/%d files", extracted, claimed)
        else:
            logger.debug("No pending files for body %s", body_id)
        return extracted

    def _deferred(self, body_id: Any) -> bool:
        """Ist die Kommune zurückgestellt (robots.txt oder Quelle eben nicht erreichbar)?"""
        return self._backoff.deferred(body_id, self._clock())

    async def _process_claimed(self, files: list[Any]) -> int:
        """
        Beanspruchte Dateien bearbeiten: ohne Abbruch parallel (``TEXT_EXTRACTION_CONCURRENCY``), Dateien mit
        einem Abbruch danach einzeln. Rückgabe: Zahl der Dateien mit Text.
        """
        semaphore = asyncio.Semaphore(self.concurrency)
        extracted = 0

        async def process_one(file_row: Any) -> None:
            nonlocal extracted
            async with semaphore:
                if await self._process_file(file_row):
                    extracted += 1

        suspects = [f for f in files if (getattr(f, "text_extraction_attempts", 0) or 0) > 0]
        regular = [f for f in files if (getattr(f, "text_extraction_attempts", 0) or 0) <= 0]
        await asyncio.gather(
            *(process_one(f) for f in regular),
            return_exceptions=True,
        )
        for file_row in suspects:
            try:
                if await self._process_file(file_row):
                    extracted += 1
            except Exception as e:  # noqa: BLE001 - wie gather(return_exceptions=True)
                logger.warning("Textextraktion für Datei %s fehlgeschlagen: %s", file_row.id, e)
        return extracted

    async def _process_file(self, file_row) -> bool:
        """Download a single file and extract its text."""
        file_id = file_row.id
        download_url = file_row.download_url or file_row.access_url
        mime_type = file_row.mime_type or ""
        file_name = file_row.file_name or ""

        if not download_url:
            await self.storage.update_file_text(
                file_id=file_id,
                status="skipped",
                error="No download URL",
            )
            return False

        # Check MIME type support
        # Allow unknown MIME types (try anyway), but skip known unsupported
        if mime_type and mime_type not in SUPPORTED_MIME_TYPES and mime_type.startswith(("image/", "video/", "audio/")):
            await self.storage.update_file_text(
                file_id=file_id,
                status="skipped",
                error=f"Unsupported MIME type: {mime_type}",
            )
            return False

        # Quelle eben zurückgestellt (etwa nach dem Downloadfehler einer Datei derselben Portion, Issue #1121):
        # nicht anfragen, noch nicht begonnen, die Datei bleibt "pending"
        if file_row.body_id is not None and self._deferred(file_row.body_id):
            await self.storage.update_file_text(file_id=file_id, status="pending", reset_attempts=False)
            return False

        # robots.txt (RFC 9309) gilt auch für Dateien; viele Systeme sperren nur Dokumente (Disallow: /*.pdf$).
        # Gesperrte Dateien werden übersprungen; nach einer Freigabe holt sie robots_override (Django) zurück.
        # Ist die robots.txt nicht erreichbar, bleibt die Datei "pending" und kommt später wieder dran.
        # Geprüft wird mit dem User-Agent, mit dem die Datei auch geladen wird (Download-Header der Quelle).
        headers = await self._download_headers(file_row.body_id)
        options = await self._fetch_options(file_row.body_id)
        interval = settings.request_interval if options.request_interval is None else options.request_interval

        async def pace(url: str) -> None:
            await host_pacer.wait(url, interval)

        decision = await robots_gate.decide(
            None,
            download_url,
            user_agent=_user_agent_of(headers),
            kind=KIND_FILES,
            override=options.robots_override,
            pace=pace,
        )
        if decision.unreachable:
            logger.info("Datei %s zurückgestellt: %s", file_id, decision.reason)
            self._backoff.hold(file_row.body_id, self._clock() + RETRY_UNREACHABLE_SECONDS)
            # Noch nicht begonnen: Abbruchzähler bleiben, eine verdächtige Datei läuft weiter einzeln und zuletzt
            await self.storage.update_file_text(file_id=file_id, status="pending", reset_attempts=False)
            return False
        if not decision.allowed:
            logger.info("Datei %s nicht abgerufen: %s", file_id, decision.reason)
            await self.storage.update_file_text(file_id=file_id, status="skipped", error=decision.reason)
            return False

        # Ab hier kann die Bearbeitung den Worker beenden (Speicher): Versuch zählen (Issue #817)
        if not await self._mark_started(file_id):
            return False

        try:
            # Drossel je Host: Dateien zählen wie jede andere Anfrage an die Quelle (mit Grenze je Host)
            async with host_pacer.limit(download_url, interval) as slot:
                # Weiterleitungen: Ziel erst nach seiner robots.txt (gleiche Kennung und Ausnahme) und im Takt
                # abrufen; der Platz in der Grenze je Host zieht mit zum Ziel-Host (#1027)
                before_redirect = redirects.check_and_pace(
                    None,
                    user_agent=_user_agent_of(headers),
                    kind=KIND_FILES,
                    override=options.robots_override,
                    pace=pace,
                    slot=slot,
                )
                await pace(download_url)
                downloaded = await self._download_to_file(download_url, headers, before_redirect)
        except redirects.RedirectRefusedError as refused:
            if refused.decision.unreachable:
                logger.info("Datei %s zurückgestellt (Weiterleitung): %s", file_id, refused.decision.reason)
                self._backoff.hold(file_row.body_id, self._clock() + RETRY_UNREACHABLE_SECONDS)
                # Begonnen, aber ohne Befund zur Datei: der Versuch zählt nicht, frühere Abbrüche bleiben
                await self.storage.update_file_text(file_id=file_id, status="pending", undo_attempt=True)
                return False
            logger.info("Datei %s nicht abgerufen (Weiterleitung): %s", file_id, refused.decision.reason)
            await self.storage.update_file_text(file_id=file_id, status="skipped", error=refused.decision.reason)
            return False
        except FileTooLargeError:
            await self.storage.update_file_text(
                file_id=file_id,
                status="skipped",
                error=f"File too large: > {self.max_size_bytes} bytes",
            )
            return False
        except Exception as e:
            if is_transient(e) and await self._defer_after_transient(file_row, download_url, e):
                return False
            logger.warning("Download failed for %s: %s", download_url, _error_text(e))
            await self.storage.update_file_text(
                file_id=file_id,
                status="failed",
                error=f"Download failed: {_error_text(e)}",
            )
            return False
        self._backoff.succeeded(file_row.body_id, file_id)

        try:
            return await self._extract_and_store(file_row, downloaded, mime_type, file_name)
        finally:
            downloaded.discard()

    async def _defer_after_transient(self, file_row: Any, url: str, error: BaseException) -> bool:
        """
        Vorübergehender Downloadfehler (Issue #1121): Quelle zurückstellen, Datei wieder ``pending``. ``False``
        beim ``MAX_TRANSIENT_FAILURES``-ten Fehlschlag derselben Datei in Folge: Sie gilt dann als gescheitert
        (eine kaputte Datei am Anfang der Warteschlange hält die Quelle sonst auf Dauer an), und die Quelle wird
        dafür nicht zurückgestellt.

        Je Zurückstellung zählt nur der Fehlschlag, der sie auslöst. Parallel bearbeitete Dateien derselben Runde
        scheitern dann an der Quelle, nicht an sich selbst. So enden bei einem langen Ausfall nur einzelne Dateien
        am Anfang der Warteschlange als gescheitert, nicht alle, die in den Runden gerade an der Reihe sind.
        """
        now = self._clock()
        counts = not self._backoff.deferred(file_row.body_id, now)
        if counts and self._backoff.file_failed(file_row.id) >= MAX_TRANSIENT_FAILURES:
            self._backoff.file_failures.pop(file_row.id, None)
            return False
        seconds = self._backoff.defer(file_row.body_id, now, retry_after_seconds(error))
        if seconds:
            logger.warning(
                "Quelle von Body %s für %d min zurückgestellt, Download von %s gescheitert: %s",
                file_row.body_id,
                round(seconds / 60),
                url,
                _error_text(error),
            )
        else:
            logger.info("Datei %s zurückgestellt, Download gescheitert: %s", file_row.id, _error_text(error))
        # Begonnen, aber ohne Befund zur Datei: der Versuch zählt nicht, frühere Abbrüche bleiben (Issue #817)
        await self.storage.update_file_text(file_id=file_row.id, status="pending", undo_attempt=True)
        return True

    async def _extract_and_store(
        self, file_row: Any, downloaded: DownloadedFile, mime_type: str, file_name: str
    ) -> bool:
        """Text aus der geladenen Datei erkennen, Ergebnis speichern und die Datei in der Ablage ablegen."""
        file_id = file_row.id
        sha256_hash = downloaded.sha256

        # Detect MIME type from content if not set
        if not mime_type and downloaded.head[:5] == b"%PDF-":
            mime_type = "application/pdf"

        # Extract text
        try:
            text, page_count, method = await asyncio.to_thread(
                self._extract_text, downloaded.path, mime_type, file_name
            )
        except OcrMemoryLimitError as e:
            logger.warning("Texterkennung an der Speichergrenze für %s: %s", file_name or file_id, e)
            await self.storage.update_file_text(
                file_id=file_id,
                status="failed",
                error=str(e),
                sha256_hash=sha256_hash,
            )
            await self._store(file_row, downloaded, mime_type)
            return False
        except Exception as e:
            logger.warning("Extraction failed for %s: %s", file_name or file_id, e)
            await self.storage.update_file_text(
                file_id=file_id,
                status="failed",
                error=f"Extraction failed: {e}",
                sha256_hash=sha256_hash,
            )
            await self._store(file_row, downloaded, mime_type)
            return False
        await self._store(file_row, downloaded, mime_type)

        if text.strip():
            await self.storage.update_file_text(
                file_id=file_id,
                text_content=text.strip(),
                method=method,
                status="completed",
                page_count=page_count,
                sha256_hash=sha256_hash,
            )
            return True
        await self.storage.update_file_text(
            file_id=file_id,
            method=method or "none",
            status="completed",
            page_count=page_count,
            sha256_hash=sha256_hash,
        )
        return False

    async def _mark_started(self, file_id: Any) -> bool:
        """Begonnene Bearbeitung zählen; ``False``, wenn die Datei nicht mehr zu bearbeiten ist."""
        mark = getattr(self.storage, "mark_extraction_started", None)
        if mark is None:
            return True
        try:
            attempts = await mark(file_id)
        except Exception as e:  # noqa: BLE001 - ohne Zähler läuft die Bearbeitung wie bisher
            logger.warning("Bearbeitungsbeginn für Datei %s nicht gespeichert: %s", file_id, e)
            return True
        if attempts is None:
            logger.info("Datei %s nicht mehr in Bearbeitung, übersprungen", file_id)
            return False
        if attempts > 1:
            logger.warning("Datei %s: Versuch %d nach Abbruch, läuft einzeln", file_id, attempts)
        return True

    async def _file_downloads_enabled(self, body_id: Any) -> bool:
        """Schalter ``sync_config["file_downloads"]`` der Quelle; im Zweifel (Fehler, alter Storage) an."""
        lookup = getattr(self.storage, "file_downloads_enabled_for_body", None)
        if lookup is None or body_id is None:
            return True
        try:
            return bool(await lookup(body_id))
        except Exception as e:  # noqa: BLE001 - der Schalter ist optional, Extraktion läuft wie bisher weiter
            logger.warning("Dateiabruf-Schalter für Body %s nicht ladbar: %s", body_id, e)
            return True

    async def _download_headers(self, body_id: Any) -> dict[str, str]:
        """
        Zusätzliche Download-Header je Quelle (``sync_config["download_headers"]``, Issue #116):
        manche RIS liefern Anlagen nur mit Referer oder Sitzungs-Cookie aus. Ergebnis je Body
        ``SOURCE_OPTIONS_TTL_SECONDS`` zwischengespeichert; ohne Body oder Konfiguration leer.
        """
        if body_id is None:
            return {}
        cached = self._header_cache.get(body_id)
        now = self._clock()
        if cached is not None and now - cached[1] < SOURCE_OPTIONS_TTL_SECONDS:
            return cached[0]
        headers: dict[str, str]
        try:
            headers = await self.storage.get_download_headers_for_body(body_id)
        except Exception as e:  # noqa: BLE001 - Header sind optional, Download läuft ohne weiter
            logger.warning("Download-Header für Body %s nicht ladbar: %s", body_id, e)
            headers = {}
        self._header_cache[body_id] = (headers, now)
        return headers

    async def _fetch_options(self, body_id: Any) -> SourceFetchOptions:
        """
        Abrufoptionen der Quelle eines Bodies (Abstand, robots-Ausnahme), je Body ``SOURCE_OPTIONS_TTL_SECONDS``
        zwischengespeichert: Eine neu gesetzte Ausnahme wirkt im laufenden Extraktions-Worker spätestens danach,
        ohne Neustart.
        """
        if body_id is None:
            return SourceFetchOptions()
        cached = self._options_cache.get(body_id)
        now = self._clock()
        if cached is not None and now - cached[1] < SOURCE_OPTIONS_TTL_SECONDS:
            return cached[0]
        lookup = getattr(self.storage, "get_fetch_options_for_body", None)
        options: SourceFetchOptions
        try:
            options = await lookup(body_id) if lookup is not None else SourceFetchOptions()
        except Exception as e:  # noqa: BLE001 - ohne lesbare Optionen gelten die Standards
            logger.warning("Abrufoptionen für Body %s nicht ladbar: %s", body_id, e)
            options = SourceFetchOptions()
        self._options_cache[body_id] = (options, now)
        return options

    async def _download_to_file(
        self,
        url: str,
        extra_headers: dict[str, str] | None = None,
        before_redirect: Callable[[str], Awaitable[None]] | None = None,
    ) -> DownloadedFile:
        """
        Datei gestreamt in eine temporäre Datei laden und dabei hashen.

        Bricht ab, sobald die Größengrenze überschritten ist (auch bei falscher ``Content-Length``). Mit
        eingerichteter Ablage liegt die temporäre Datei im selben Dateisystem wie die Ablage.

        Weiterleitungen folgen wir selbst (:mod:`src.client.redirects`): vorher ``before_redirect`` mit dem Ziel
        (robots.txt und Takt der Quelle; ohne Angabe robots.txt ohne Ausnahme und Standardtakt).
        ``redirects.RedirectRefusedError``, wenn die robots.txt des Ziels den Abruf nicht erlaubt.
        """
        if before_redirect is None:
            interval = settings.request_interval

            async def pace(target: str) -> None:
                await host_pacer.wait(target, interval)

            before_redirect = redirects.check_and_pace(
                None, user_agent=_user_agent_of(extra_headers), kind=KIND_FILES, override=None, pace=pace
            )

        headers = {
            **{key: value for key, value in (extra_headers or {}).items() if key.lower() != "user-agent"},
            "User-Agent": _user_agent_of(extra_headers),
        }
        root = blob_root()
        directory: Path | None = None
        if root is not None:
            directory = root / "tmp"
            directory.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(suffix=".part", dir=directory)
        path = Path(name)
        digest = hashlib.sha256()
        size = 0
        head = b""
        try:
            with os.fdopen(handle, "wb") as target:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    request = client.build_request("GET", url, headers=headers)
                    response = await redirects.send(client, request, before_redirect=before_redirect, stream=True)
                    try:
                        response.raise_for_status()
                        declared = response.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > self.max_size_bytes:
                            raise FileTooLargeError
                        async for chunk in response.aiter_bytes(256 * 1024):
                            size += len(chunk)
                            if size > self.max_size_bytes:
                                raise FileTooLargeError
                            digest.update(chunk)
                            target.write(chunk)
                            if len(head) < 512:
                                head += chunk[: 512 - len(head)]
                    finally:
                        await response.aclose()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return DownloadedFile(path=path, size=size, sha256=digest.hexdigest(), head=head)

    async def _store(self, file_row: Any, downloaded: DownloadedFile, mime_type: str) -> bool:
        """
        Geladene Datei in der Ablage nach SHA-256 ablegen (nur gelistete Kommunen, genug Platz).

        Hinweis- oder Prüfseiten statt der Datei (HTML bei erwarteter PDF) und leere Antworten werden nicht
        abgelegt. Fehler beim Ablegen verhindern die Texterkennung nie.
        """
        root = blob_root()
        attach = getattr(self.storage, "attach_file_blob", None)
        if root is None or attach is None or not downloaded.size or file_row.body_id is None:
            return False
        if looks_like_html(downloaded.head) and "html" not in (mime_type or "").lower():
            return False
        try:
            free = shutil.disk_usage(root.parent).free
            if free - downloaded.size < settings.file_cache_min_free_gb * 1024**3:
                logger.info("Ablage: zu wenig freier Speicher, Datei %s nicht abgelegt", file_row.id)
                return False
            if not await self.storage.body_stores_files(file_row.body_id):
                return False
            target = root / downloaded.sha256[:2] / downloaded.sha256
            if downloaded.path.parent != root / "tmp":
                return False
            stored = bool(await attach(file_row.id, downloaded.sha256, downloaded.size, downloaded.path, target))
        except Exception as e:  # noqa: BLE001 - Ablage ist optional, der Text zählt
            logger.warning("Datei %s nicht abgelegt: %s", file_row.id, e)
            return False
        return stored

    @staticmethod
    def _extract_text(source: Path, mime_type: str, file_name: str) -> tuple[str, int | None, str]:
        """
        Text der geladenen Datei mit der gemeinsamen Texterkennung (läuft in einem Thread).

        ``OcrMemoryLimitError`` geht an den Aufrufer (Datei gilt als gescheitert, Grund „Speichergrenze“).

        Returns:
            (text, page_count, extraction_method)
        """
        result = extract_text(source, mime_type, file_name, extraction_config())
        return result.text, result.page_count, result.method


def runs_elsewhere() -> bool:
    """``TEXT_EXTRACTION_RUNNER=worker``: Die Anwendung erkennt den Text in Aufträgen, dieser Worker ruht."""
    return (settings.text_extraction_runner or "").strip().lower() == RUNNER_WORKER


def extraction_config() -> ExtractionConfig:
    """
    Grenzen der Texterkennung und Mistral-Zugang aus den Einstellungen (``OCR_*``, ``MISTRAL_*``).

    Mistral wirkt nur mit Basis-URL, deren Host in ``KI_ERLAUBTE_HOSTS`` steht (``MistralConfig.enabled``). Der
    Ingestor erkennt nur öffentliche RIS-Dateien.
    """
    return ExtractionConfig(
        ocr=OcrLimits(
            dpi=settings.ocr_dpi,
            max_pixels=int(settings.ocr_max_megapixels * 1_000_000),
            memory_limit_mb=settings.ocr_memory_limit_mb,
            page_timeout=settings.ocr_page_timeout,
            file_budget=settings.ocr_file_budget_seconds,
            max_pages=settings.ocr_max_pages,
        ),
        mistral=MistralConfig(
            api_key=settings.mistral_api_key,
            url=settings.mistral_base_url,
            erlaubte_hosts=erlaubte_hosts_aus_umgebung(settings.ki_erlaubte_hosts),
            model=settings.mistral_ocr_model,
            timeout=settings.text_extraction_timeout,
            requests_per_minute=settings.mistral_ocr_rate_limit,
        ),
    )


def _user_agent_of(headers: dict[str, str] | None) -> str:
    """User-Agent für Datei-Abrufe: aus den Download-Headern der Quelle (gleich welche Schreibweise), sonst Standard."""
    for key, value in (headers or {}).items():
        if key.lower() == "user-agent" and str(value).strip():
            return str(value).strip()
    return settings.user_agent
