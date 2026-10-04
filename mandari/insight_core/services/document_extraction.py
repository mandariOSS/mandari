# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokument-Extraktion in der Anwendung: Abruf und Texterkennung.

Der Abruf (robots.txt, Drossel je Host, nur öffentliche Ziele) gehört der Anwendung. Die Texterkennung selbst
ist die gemeinsame Bibliothek ``mandari_dokumente`` (shared/, Issue #530) – dieselbe Implementierung wie im
OCR-Worker des Ingestors: pypdf, optional Mistral, sonst Tesseract Seite für Seite mit Speicher- und
Zeitgrenzen (Issue #817). Die frühere eigene Umsetzung (alle Seiten auf einmal, eigene Mistral-Anbindung) ist
entfallen. Grenzen und Mistral-Zugang kommen aus den Einstellungen (``OCR_*``, ``MISTRAL_*``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from django.conf import settings
from mandari_dokumente import (
    METHOD_NONE,
    ExtractionConfig,
    MistralConfig,
    OcrLimits,
    OcrMemoryLimitError,
    extract_text,
)

logger = logging.getLogger(__name__)

PDF_MIME_TYPES = {
    "application/pdf",
    "application/x-pdf",
}


@dataclass(slots=True)
class ExtractedDocument:
    """Ergebnis einer Dokumenten-Extraktion."""

    binary: bytes
    text: str
    checksum: str
    mime_type: str
    original_name: str
    source_url: str
    ocr_performed: bool = False
    page_count: int | None = None
    extraction_method: str = "none"  # pypdf, mistral, tesseract, text, none


@dataclass(slots=True)
class DownloadedFile:
    """Gestreamt geladene Datei in einer temporären Datei (``discard`` löscht sie)."""

    path: Path
    size: int
    sha256: str
    content_type: str

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


class DocumentDownloadError(RuntimeError):
    """Wird geworfen, wenn ein Dokument nicht heruntergeladen werden kann."""


class DocumentTooLargeError(DocumentDownloadError):
    """Die Datei überschreitet ``TEXT_EXTRACTION_MAX_SIZE_MB`` (Abbruch während des Downloads)."""


class RobotsBlockedError(DocumentDownloadError):
    """Die robots.txt der Quelle sperrt das Dokument; ``reason`` beginnt mit ``robots.txt``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SourceBusyError(DocumentDownloadError):
    """
    Die Drossel je Host lässt innerhalb der Höchstwartezeit keinen Abruf zu (nur mit ``max_wait``, also in
    Web-Anfragen). Kein Fehler der Quelle: später erneut versuchen.
    """


class RobotsUnreachableError(DocumentDownloadError):
    """
    Die robots.txt der Quelle war nicht erreichbar (5xx, 408, 429, Netzfehler): Abruf zurückgestellt, keine
    Sperre. Aufrufer lassen die Datei in der Warteschlange und versuchen es später erneut.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def extraction_config(ocr_max_pages: int | None = None) -> ExtractionConfig:
    """Grenzen der Texterkennung und Mistral-Zugang aus den Einstellungen; ``ocr_max_pages`` begrenzt enger."""
    max_pages = int(getattr(settings, "OCR_MAX_PAGES", 100))
    if ocr_max_pages is not None:
        max_pages = max(0, min(max_pages, ocr_max_pages))
    return ExtractionConfig(
        ocr=OcrLimits(
            dpi=int(getattr(settings, "OCR_DPI", 200)),
            max_pixels=int(float(getattr(settings, "OCR_MAX_MEGAPIXELS", 8)) * 1_000_000),
            memory_limit_mb=int(getattr(settings, "OCR_MEMORY_LIMIT_MB", 1024)),
            page_timeout=float(getattr(settings, "OCR_PAGE_TIMEOUT", 120)),
            file_budget=float(getattr(settings, "OCR_FILE_BUDGET_SECONDS", 1200)),
            max_pages=max_pages,
        ),
        mistral=MistralConfig(
            api_key=str(getattr(settings, "MISTRAL_API_KEY", "") or ""),
            model=str(getattr(settings, "MISTRAL_OCR_MODEL", "pixtral-12b-2409")),
            requests_per_minute=int(getattr(settings, "MISTRAL_OCR_RATE_LIMIT", 60)),
        ),
    )


def _prepare_fetch(
    url: str,
    extra_headers: dict[str, str] | None,
    sync_config: Any,
    max_wait: float | None,
) -> tuple[str, dict[str, str]]:
    """
    robots.txt und Drossel je Host vor einem Abruf; Rückgabe: (User-Agent, Header ohne User-Agent).

    Geprüft wird mit dem User-Agent des Abrufs (``User-Agent`` in ``extra_headers``, sonst unser Standard).
    Gesperrt: ``RobotsBlockedError``, robots.txt nicht erreichbar: ``RobotsUnreachableError``, kein freier
    Zeitpunkt innerhalb von ``max_wait``: ``SourceBusyError`` – jeweils ohne Anfrage an die Quelle.
    """
    from . import host_pacing, robots

    agent = next(
        (v for k, v in (extra_headers or {}).items() if k.lower() == "user-agent" and v.strip()), robots.USER_AGENT
    )
    deadline = None if max_wait is None else time.monotonic() + max_wait
    try:
        decision = robots.check(url, robots.KIND_FILES, sync_config=sync_config, agent=agent, max_wait=max_wait)
    except host_pacing.PacingBusyError as exc:
        raise SourceBusyError(f"Drossel je Host: kein freier Zeitpunkt für {url}") from exc
    if decision.unreachable:
        raise RobotsUnreachableError(decision.reason)
    if not decision.allowed:
        raise RobotsBlockedError(decision.reason)
    # Drossel je Host über alle Prozesse; mit Höchstwartezeit gilt die verbleibende Zeit
    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    if not host_pacing.wait(url, sync_config=sync_config, max_wait=remaining):
        raise SourceBusyError(f"Drossel je Host: kein freier Zeitpunkt für {url}")
    return agent, {k: v for k, v in (extra_headers or {}).items() if k.lower() != "user-agent"}


def _http_get(
    url: str,
    timeout: float = 60.0,
    extra_headers: dict[str, str] | None = None,
    sync_config: Any = None,
    max_wait: float | None = None,
) -> httpx.Response:
    """
    HTTP-GET nach robots.txt und Drossel je Host (``extra_headers``: Download-Header je Quelle, Issue #116).

    Nur für Abrufe, deren Antwort ohnehin in den Speicher gehört (Web-Anfragen, KI); die Texterkennung der
    RIS-Dateien lädt gestreamt (``download_to_file``).
    """
    agent, headers = _prepare_fetch(url, extra_headers, sync_config, max_wait)
    from .safe_fetch import guarded_client

    try:
        # Nur öffentliche Ziele, auch nach Weiterleitungen (Adressen stammen aus der Quelle)
        with guarded_client(timeout=timeout) as client:
            response = client.get(url, headers={**headers, "User-Agent": agent}, follow_redirects=True)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DocumentDownloadError(f"Download fehlgeschlagen: {url}") from exc
    return response


def download_to_file(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 120.0,
    extra_headers: dict[str, str] | None = None,
    sync_config: Any = None,
) -> DownloadedFile:
    """
    Datei nach robots.txt und Drossel je Host gestreamt in eine temporäre Datei laden und dabei hashen.

    Nie liegt die ganze Datei im Speicher; die Größengrenze greift während des Downloads
    (``DocumentTooLargeError``). Fehler der Quelle: ``DocumentDownloadError``.
    """
    from .safe_fetch import DeadlineExceededError, TooLargeError, download_to

    agent, headers = _prepare_fetch(url, extra_headers, sync_config, None)
    handle, name = tempfile.mkstemp(suffix=".part", prefix="texterkennung-")
    path = Path(name)
    try:
        with os.fdopen(handle, "wb") as target:
            result = download_to(
                target,
                url,
                max_bytes=max_bytes,
                total_seconds=timeout,
                timeout=httpx.Timeout(timeout),
                headers=headers,
                user_agent=agent,
            )
    except TooLargeError as exc:
        path.unlink(missing_ok=True)
        raise DocumentTooLargeError(f"Datei größer als {max_bytes // 1024 // 1024} MB") from exc
    except (httpx.HTTPError, DeadlineExceededError) as exc:
        path.unlink(missing_ok=True)
        raise DocumentDownloadError(f"Download fehlgeschlagen ({type(exc).__name__})") from exc
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return DownloadedFile(path=path, size=result.size, sha256=digest.hexdigest(), content_type=result.content_type)


def extract_text_from_file(
    data: bytes | Path,
    mime_type: str | None = None,
    file_name: str = "",
    ocr_max_pages: int | None = None,
) -> tuple[str, bool, int | None, str]:
    """
    Text aus Binärdaten (oder einer Datei) mit der gemeinsamen Texterkennung.

    ``ocr_max_pages`` begrenzt die erkannten Seiten (etwa beim Import im laufenden Seitenaufruf). Scheitert die
    Erkennung an der Speichergrenze, ist das Ergebnis leer (Methode ``none``); Aufrufer brechen deshalb nie ab.

    Returns:
        Tuple mit (text, ocr_performed, page_count, extraction_method)
    """
    try:
        result = extract_text(data, mime_type, file_name, extraction_config(ocr_max_pages))
    except OcrMemoryLimitError:
        logger.warning("Texterkennung an der Speichergrenze für %s", file_name or "Datei")
        return "", False, None, METHOD_NONE
    return result.text, result.ocr_performed, result.page_count, result.method


def download_and_extract(
    *,
    url: str,
    mime_type: str | None = None,
    original_name: str = "",
    timeout: float = 60.0,
    extra_headers: dict[str, str] | None = None,
    sync_config: Any = None,
    max_wait: float | None = None,
) -> ExtractedDocument:
    """
    Lädt ein Dokument herunter und extrahiert Text.

    Args:
        url: Download-URL
        mime_type: MIME-Typ (optional, wird aus Response ermittelt)
        original_name: Originaler Dateiname
        timeout: HTTP-Timeout in Sekunden
        sync_config: ``sync_config`` der Quelle (Ausnahme von der robots.txt, Abstand der Drossel)
        max_wait: höchstens so lange auf die Drossel je Host warten (Web-Anfragen)

    Raises:
        RobotsBlockedError: die robots.txt sperrt das Dokument
        RobotsUnreachableError: die robots.txt ist nicht erreichbar (später erneut versuchen)
        SourceBusyError: kein freier Zeitpunkt innerhalb von ``max_wait`` (später erneut versuchen)
        DocumentDownloadError: Abruf fehlgeschlagen
    """
    response = _http_get(url, timeout=timeout, extra_headers=extra_headers, sync_config=sync_config, max_wait=max_wait)
    binary = response.content
    resolved_mime = mime_type or response.headers.get("Content-Type", "").split(";")[0]
    checksum = hashlib.sha256(binary).hexdigest()

    text, ocr_used, page_count, extraction_method = extract_text_from_file(
        binary,
        mime_type=resolved_mime,
        file_name=original_name or url.split("/")[-1],
    )

    return ExtractedDocument(
        binary=binary,
        text=text,
        checksum=checksum,
        mime_type=resolved_mime,
        original_name=original_name or url.split("/")[-1],
        source_url=url,
        ocr_performed=ocr_used,
        page_count=page_count,
        extraction_method=extraction_method,
    )
