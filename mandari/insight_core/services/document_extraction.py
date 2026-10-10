# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokument-Extraktion in der Anwendung: Texterkennung und die Regeln vor jedem Abruf.

robots.txt und Drossel je Host vor einem Abruf (``prepare_fetch``) und die Fehlerklassen stehen hier; RIS-Dateien
lädt nur ``hub.ris.abruf`` (Issue #919, ein Weg zur Quelle). Die Texterkennung selbst
ist die gemeinsame Bibliothek ``mandari_dokumente`` (shared/, Issue #530) – dieselbe Implementierung wie im
OCR-Worker des Ingestors: pypdf, optional Mistral, sonst Tesseract Seite für Seite mit Speicher- und
Zeitgrenzen (Issue #817). Die frühere eigene Umsetzung (alle Seiten auf einmal, eigene Mistral-Anbindung) ist
entfallen. Grenzen und Mistral-Zugang kommen aus den Einstellungen (``OCR_*``, ``MISTRAL_*``).

Externe Texterkennung (Mistral) nur auf ausdrücklichen Wunsch (``allow_external=True``) und nur für öffentliche
RIS-Dateien; Standard ist die Erkennung im eigenen Betrieb (pypdf, Tesseract), etwa für den Work-Import und
Anlagen in Session (Issue #950). Auch dann wirkt Mistral nur mit Basis-URL aus ``KI_ERLAUBTE_HOSTS``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

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

from apps.common.ki_anbieter import erlaubte_hosts

logger = logging.getLogger(__name__)

PDF_MIME_TYPES = {
    "application/pdf",
    "application/x-pdf",
}

#: Präfixe eigener temporärer Dateien (``texterkennung-*``, auch der Bibliothek) und Seitenverzeichnisse der
#: Texterkennung (``ocr-seite-*``). Sie enthalten Dokumentinhalte, auch nichtöffentliche Anlagen.
TEMP_FILE_PREFIX: Final = "texterkennung-"
TEMP_DIR_PREFIX: Final = "ocr-seite-"
#: Ältere Reste gelten als liegengeblieben (Prozess beendet, etwa vom Speicherwächter oder an der Zeitgrenze
#: des Runners): weit über der längsten Bearbeitung (Auftrag höchstens 30 min, Anfragen Sekunden)
TEMP_MAX_AGE_SECONDS: Final = 2 * 3600
#: Je Prozess höchstens so oft nach Resten sehen (beim ersten Abruf bzw. der ersten Erkennung, dann stündlich)
TEMP_PURGE_EVERY_SECONDS: Final = 3600
_purge_state: dict[str, float] = {}


def purge_leftover_temp_files(
    max_age: float = TEMP_MAX_AGE_SECONDS, now: float | None = None, directory: Path | None = None
) -> int:
    """
    Liegengebliebene temporäre Dateien der Texterkennung löschen. Endet ein Prozess mitten in der Arbeit
    (Speicherwächter, Zeitgrenze), räumt kein ``finally`` mehr auf. Rückgabe: Zahl der gelöschten Reste.
    """
    jetzt = time.time() if now is None else now
    root = directory or Path(tempfile.gettempdir())
    removed = 0
    for path in [*root.glob(f"{TEMP_FILE_PREFIX}*"), *root.glob(f"{TEMP_DIR_PREFIX}*")]:
        try:
            if jetzt - path.stat().st_mtime <= max_age:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Texterkennung: %d liegengebliebene temporäre Dateien gelöscht", removed)
    return removed


def purge_now_and_then() -> None:
    """``purge_leftover_temp_files`` höchstens einmal je ``TEMP_PURGE_EVERY_SECONDS`` und Prozess."""
    jetzt = time.monotonic()
    zuletzt = _purge_state.get("at")
    if zuletzt is not None and jetzt - zuletzt < TEMP_PURGE_EVERY_SECONDS:
        return
    _purge_state["at"] = jetzt
    purge_leftover_temp_files()


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


def extraction_config(ocr_max_pages: int | None = None, *, allow_external: bool = False) -> ExtractionConfig:
    """
    Grenzen der Texterkennung und Mistral-Zugang aus den Einstellungen; ``ocr_max_pages`` begrenzt enger.

    Standard (``allow_external=False``): nur Erkennung im eigenen Betrieb (pypdf, Tesseract), nie ein externer
    Dienst – für alles außer öffentlichen RIS-Dateien, etwa nichtöffentliche Sitzungsunterlagen (Issue #873),
    den Work-Import und Anlagen in Session (Issue #950). ``True`` übergeben nur die Aufrufer für öffentliche
    RIS-Dateien; Mistral wirkt auch dann nur mit Basis-URL aus ``KI_ERLAUBTE_HOSTS``.
    """
    max_pages = int(getattr(settings, "OCR_MAX_PAGES", 100))
    if ocr_max_pages is not None:
        max_pages = max(0, min(max_pages, ocr_max_pages))
    if not allow_external:
        return ExtractionConfig(ocr=_ocr_limits(max_pages), mistral=MistralConfig())
    return ExtractionConfig(
        ocr=_ocr_limits(max_pages),
        mistral=MistralConfig(
            api_key=str(getattr(settings, "MISTRAL_API_KEY", "") or ""),
            url=str(getattr(settings, "MISTRAL_BASE_URL", "") or ""),
            erlaubte_hosts=erlaubte_hosts(),
            model=str(getattr(settings, "MISTRAL_OCR_MODEL", "pixtral-12b-2409")),
            requests_per_minute=int(getattr(settings, "MISTRAL_OCR_RATE_LIMIT", 60)),
        ),
    )


def _ocr_limits(max_pages: int) -> OcrLimits:
    return OcrLimits(
        dpi=int(getattr(settings, "OCR_DPI", 200)),
        max_pixels=int(float(getattr(settings, "OCR_MAX_MEGAPIXELS", 8)) * 1_000_000),
        memory_limit_mb=int(getattr(settings, "OCR_MEMORY_LIMIT_MB", 1024)),
        page_timeout=float(getattr(settings, "OCR_PAGE_TIMEOUT", 120)),
        file_budget=float(getattr(settings, "OCR_FILE_BUDGET_SECONDS", 1200)),
        max_pages=max_pages,
    )


def prepare_fetch(
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

    Nur für Abrufe, deren Antwort ohnehin in den Speicher gehört (Web-Anfragen, KI); RIS-Dateien lädt der eine Weg
    zur Quelle gestreamt (``hub.ris.abruf``, Issue #919).
    """
    agent, headers = prepare_fetch(url, extra_headers, sync_config, max_wait)
    from .safe_fetch import guarded_client

    try:
        # Nur öffentliche Ziele, auch nach Weiterleitungen (Adressen stammen aus der Quelle)
        with guarded_client(timeout=timeout) as client:
            response = client.get(url, headers={**headers, "User-Agent": agent}, follow_redirects=True)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DocumentDownloadError(f"Download fehlgeschlagen: {url}") from exc
    return response


def extract_text_from_file(
    data: bytes,
    mime_type: str | None = None,
    file_name: str = "",
    ocr_max_pages: int | None = None,
    *,
    allow_external: bool = False,
) -> tuple[str, bool, int | None, str]:
    """
    Text aus Binärdaten mit der gemeinsamen Texterkennung.

    Die Daten (oft hochgeladen oder von einer Quelle geladen) landen zuerst in einer eigenen temporären Datei;
    die Bibliothek und ihre Unterprozesse sehen nur deren Pfad, nie Werte von außen. Bleibt sie liegen, weil
    der Prozess endet, löscht sie ``purge_leftover_temp_files`` nach ``TEMP_MAX_AGE_SECONDS``. ``ocr_max_pages``
    begrenzt die erkannten Seiten (etwa beim Import im laufenden Seitenaufruf). Scheitert die Erkennung an der
    Speichergrenze, ist das Ergebnis leer (Methode ``none``); Aufrufer brechen deshalb nie ab.
    Externe Dienste (Mistral) nur mit ``allow_external=True`` (öffentliche RIS-Dateien), siehe ``extraction_config``.

    Returns:
        Tuple mit (text, ocr_performed, page_count, extraction_method)
    """
    purge_now_and_then()
    handle, name = tempfile.mkstemp(suffix=".bin", prefix=TEMP_FILE_PREFIX)
    path = Path(name)
    try:
        with os.fdopen(handle, "wb") as target:
            target.write(data)
        result = extract_text(
            path, mime_type, file_name, extraction_config(ocr_max_pages, allow_external=allow_external)
        )
    except OcrMemoryLimitError:
        logger.warning("Texterkennung an der Speichergrenze für %s", file_name or "Datei")
        return "", False, None, METHOD_NONE
    finally:
        path.unlink(missing_ok=True)
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
    allow_external: bool = False,
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
        allow_external: externe Texterkennung zulassen (nur öffentliche RIS-Dateien, siehe ``extraction_config``)

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
        allow_external=allow_external,
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
