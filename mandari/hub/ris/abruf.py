# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abruf der RIS-Dateien bei der Quelle: der eine Weg zur Quelle (Issue #919, ``docs/adr/20261007-dokumentkette.md``).

Datei-Downloads liegen in genau diesem Modul. Dokument-Cache (``cache_files``, Admin-Aktion), Vorschau
(Live-Abruf mit Write-Through) und Löschabgleich (``verify``, ``replace_content``) nutzen es; der Vertrag
``abruf-ein-weg`` in ``pyproject.toml`` (import-linter) erlaubt den Import nur aus ``hub.ris``,
``insight_core.services.file_cache``, ``insight_core.services.file_reconcile`` und ``insight_core.views.files``.
Der Schutz vor nicht öffentlichen Zielen (``safe_fetch``), die robots.txt und die Drossel je Host gelten
unverändert.

**Zustände des Abrufs** (``OParlFile.local_status``, ADR Abschnitt 4):

- ``fetching``: beansprucht. Beansprucht wird atomar beim Start eines Abrufs (``SELECT … FOR UPDATE SKIP
  LOCKED``) und nur aus ``none``, aus fälligem ``retry`` bzw. ``error`` oder aus einer verfallenen
  Beanspruchung; ``fetch_next_at`` hält dann das Ende der Beanspruchung (``DOCUMENT_FETCH_STALE_MINUTES``).
  Zeitpläne wählen nur aus und beanspruchen nicht. Endet ein Abruf ohne Ergebnis (Platte, Ablage, Takt,
  Quelle in Schonung), stellt er den Zustand davor wieder her und zählt keinen Versuch. Liegen gebliebene
  Beanspruchungen gibt ``release_stale`` zu Beginn jedes Laufs von ``cache_files`` frei.
- ``retry``: Zeitüberschreitung, Verbindungsfehler, 429, 5xx, leere Antwort, robots.txt nicht erreichbar, sowie
  404/410 in den ersten sieben Tagen nach unserer Erfassung (``created_at``). Wiederholt nach 15 min, 1 h,
  6 h, 24 h und 72 h (``fetch_attempts``, ``fetch_next_at``), danach ``error`` bzw. bei 404/410 ``missing``.
- ``missing``: 404/410 danach; der Löschabgleich übernimmt (er prüft auch nie abgelegte Dateien).
- ``refused``: robots.txt verbietet, oder die Quelle liefert eine HTML-Seite statt der Datei (Bot-Schutz).
  Zurück auf ``none`` nur nach Freigabe (``robots_override`` bzw. ``dokumentkette freigeben``). Bewusst nicht
  „gesperrt“ im Sinn des Löschabgleichs (#787).
- ``too_large``: größer als ``FILE_CACHE_MAX_MB``.
- ``error``: sonstige Fehler (etwa 403) und erschöpfte Wiederholungen; ein neuer Versuch nach einer Woche.

Der Fehlercode steht in ``fetch_error`` (Kennzahl ``mandari_files_fetch_errors_total`` je Quelle und Code), der
lesbare Grund wie bisher in ``local_error``. Ein Abruffehler ändert ``text_extraction_status`` nie.

**Was nie abgerufen wird** (ADR Abschnitt 3): gelöschte (alle Gründe), gesperrte (``source_missing_since``) und
nach #787 geleerte Dateien (``content_purged_at``) – geprüft beim Auswählen und beim Beanspruchen. Der
Löschabgleich ruft gesperrte Dateien dagegen ausdrücklich ab (``stream``), sonst gäbe es kein Entsperren.

**Abruf erlaubt** heißt: Die Quelle ist aktiv, ``sync_config["file_downloads"]`` ist nicht ``false``, sie ist
nicht in Schonung (``file_cache.source_paused``, geprüft vor jedem Versuch) und die robots.txt erlaubt es.

**Ablage** (ADR Abschnitt 5, ``stores_file``): gelistete Kommunen wie bisher; mit
``TEXT_EXTRACTION_RUNNER=worker`` alle Quellen mit erlaubtem Abruf, aber nur Dateien ab dem Stichtag
``sync_config["document_since"]`` (Altbestand nur mit ``sync_config["document_backfill"] = true``). Ein leerer
Stichtag gilt nur für Quellen mit gelisteter Kommune (die bisher abgelegten); ``ausstehend`` heißt: erster
vollständiger Sync noch nicht beendet, nichts abrufen. ``stichtage_setzen`` trägt die Stichtage nach (bei
``TEXT_EXTRACTION_RUNNER=worker`` zu Beginn jedes Laufs von ``cache_files``, von Hand mit ``dokumentkette
umschalten``). Ein neuer Inhalt wird im selben Abruf in den Objektspeicher geladen; ``FILE_CACHE_MIN_FREE_GB``
gilt immer.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any, Final

import httpx
from django.conf import settings
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Q, QuerySet, Value, When
from django.utils import timezone
from mandari_oparl.abgleich import GONE_STATUS, looks_like_html
from prometheus_client import Counter as PromCounter

from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import file_cache, file_store, host_pacing, robots, safe_fetch
from insight_core.services.document_extraction import (
    TEMP_FILE_PREFIX,
    DocumentDownloadError,
    DocumentTooLargeError,
    DownloadedFile,
    prepare_fetch,
    purge_now_and_then,
)
from insight_core.services.text_extraction_job import runner_is_worker

logger = logging.getLogger(__name__)

# --- Zustände (``OParlFile.local_status``) ---------------------------------------------------------------
NONE: Final = "none"
FETCHING: Final = "fetching"
OK: Final = "ok"
RETRY: Final = "retry"
MISSING: Final = "missing"
REFUSED: Final = "refused"
ERROR: Final = "error"
TOO_LARGE: Final = "too_large"

# --- Ergebnisse ohne eigenen Zustand ----------------------------------------------------------------------
#: Nichts zu tun: schon abgelegt, schon beansprucht oder nicht fällig
SKIPPED: Final = "skipped"
#: Quelle in Schonung bzw. Abruf nicht erlaubt: kein Versuch, kein Zustand
PAUSED: Final = "paused"
#: Gelöscht, gesperrt oder nach #787 geleert: wird nie abgerufen
EXCLUDED: Final = "excluded"
#: Platte unter ``FILE_CACHE_MIN_FREE_GB``: Zustand wiederhergestellt, kein Versuch gezählt
DISK_FULL: Final = "disk_full"
#: Ablage oder Objektspeicher gestört: kein Quellabruf, Zustand wiederhergestellt
STORAGE_ERROR: Final = "storage_error"
#: Takt der Drossel je Host belegt (nur mit Höchstwartezeit): später erneut, kein Versuch gezählt
BUSY: Final = "busy"

# --- Fehlercodes (``OParlFile.fetch_error``) -------------------------------------------------------------
CODE_TIMEOUT: Final = "timeout"
CODE_CONNECTION: Final = "verbindung"
CODE_THROTTLED: Final = "http_429"
CODE_SERVER: Final = "http_5xx"
CODE_EMPTY: Final = "leer"
CODE_ROBOTS_UNREACHABLE: Final = "robots_unerreichbar"
CODE_NOT_FOUND: Final = "nicht_gefunden"
CODE_HTML: Final = "html"
CODE_ROBOTS: Final = "robots"
CODE_TOO_LARGE: Final = "zu_gross"
CODE_CLIENT: Final = "http_4xx"
CODE_BLOCKED_DESTINATION: Final = "ziel_gesperrt"
CODE_NO_URL: Final = "keine_adresse"
#: Vorübergehende Fehler: Wiederholung mit wachsendem Abstand
RETRY_CODES: Final = frozenset(
    {CODE_TIMEOUT, CODE_CONNECTION, CODE_THROTTLED, CODE_SERVER, CODE_EMPTY, CODE_ROBOTS_UNREACHABLE}
)
#: Die Quelle lässt uns nicht laden: erst nach Freigabe erneut
REFUSED_CODES: Final = frozenset({CODE_HTML, CODE_ROBOTS})

#: Abstände der Wiederholungen; danach ``error`` (bzw. bei 404/410 ``missing``)
BACKOFF: Final = (
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
    timedelta(hours=72),
)
#: Ein Fehler (``error``) wird nach dieser Frist erneut versucht
ERROR_RETRY: Final = timedelta(days=7)
#: Die Quelle veröffentlicht das Objekt oft vor der Datei: 404/410 in dieser Frist nach unserer Erfassung → ``retry``
NOT_FOUND_GRACE: Final = timedelta(days=7)

#: Fehlertext einer HTML-Seite statt der Datei (gleich dem bisherigen, ältere Images erkennen ihn wieder)
HTML_TEXT: Final = "Quelle liefert eine HTML-Seite statt der Datei"
#: Fehlertext einer Sperre durch die robots.txt, wenn die Prüfung keinen Grund nennt
ROBOTS_TEXT: Final = f"{robots.SKIP_ERROR_PREFIX}: Abruf der Datei gesperrt"

#: Schlüssel in ``OParlSource.sync_config``
DOCUMENT_SINCE_KEY: Final = "document_since"
DOCUMENT_BACKFILL_KEY: Final = "document_backfill"
#: Stichtag einer neuen Quelle bis zum Ende ihres ersten vollständigen Syncs
SINCE_PENDING: Final = "ausstehend"

FETCH_ERRORS = PromCounter(
    "mandari_files_fetch_errors_total",
    "Fehlgeschlagene Abrufe von RIS-Dateien je Quelle und Fehlercode",
    ["source", "code"],
)


# =============================================================================
# Einstellungen
# =============================================================================


def claim_duration() -> timedelta:
    """So lange gilt eine Beanspruchung; danach gilt sie als liegen geblieben (Absturz)."""
    return timedelta(minutes=int(getattr(settings, "DOCUMENT_FETCH_STALE_MINUTES", 30)))


def max_queued() -> int:
    """Höchstens so viele Abrufe je Quelle und Lauf; der Rest kommt im nächsten Lauf."""
    return max(1, int(getattr(settings, "DOCUMENT_FETCH_MAX_QUEUED", 200)))


# =============================================================================
# Abruf erlaubt, Ablage und Stichtag
# =============================================================================


def source_fetch_allowed(source: Any) -> bool:
    """Quelle aktiv und Dateiabruf nicht abgeschaltet (``sync_config["file_downloads"]``)?"""
    if source is None:
        return True
    return bool(getattr(source, "is_active", True)) and not file_cache.downloads_disabled_in(source.sync_config)


def fetch_allowed(body: Any) -> bool:
    """Abruf für die Kommune erlaubt (ohne Schonung und robots.txt, die vor jedem Versuch gelten)?"""
    return source_fetch_allowed(getattr(body, "source", None) if body is not None else None)


@dataclass(frozen=True)
class Stichtag:
    """Stichtag einer Quelle: leer, ``ausstehend`` oder ein Zeitpunkt; dazu die Freigabe des Altbestands."""

    since: datetime | None = None
    pending: bool = False
    backfill: bool = False


def _parse_since(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
    return parsed


def stichtag_of(source: Any) -> Stichtag:
    config = getattr(source, "sync_config", None) if source is not None else None
    if not isinstance(config, dict):
        return Stichtag()
    backfill = config.get(DOCUMENT_BACKFILL_KEY) is True
    value = config.get(DOCUMENT_SINCE_KEY)
    if value in (None, ""):
        return Stichtag(backfill=backfill)
    if value == SINCE_PENDING:
        return Stichtag(pending=True, backfill=backfill)
    since = _parse_since(value)
    if since is None:
        # Unlesbarer Stichtag: lieber nichts abrufen als den ganzen Altbestand
        logger.warning("Quelle %s: document_since unlesbar, Dateien werden nicht abgelegt", getattr(source, "pk", "?"))
        return Stichtag(pending=True, backfill=backfill)
    return Stichtag(since=since, backfill=backfill)


def stores_file(file_obj: Any) -> bool:
    """Wird diese Datei abgelegt (Dokument-Cache, Write-Through der Vorschau, Löschabgleich)?"""
    body = getattr(file_obj, "body", None)
    if body is None:
        return False
    source = getattr(body, "source", None)
    if source is None:
        return bool(body.is_listed)
    if not source_fetch_allowed(source):
        return False
    if not body.is_listed and not runner_is_worker():
        return False
    stichtag = stichtag_of(source)
    if stichtag.pending:
        return False
    if stichtag.since is None:
        # Leerer Stichtag: nur die bisher abgelegten (gelisteten) Kommunen
        return bool(body.is_listed)
    if stichtag.backfill:
        return True
    created = getattr(file_obj, "created_at", None) or timezone.now()
    return bool(created >= stichtag.since)


def stores_body(body: Any) -> bool:
    """Legt die Kommune überhaupt Dateien ab (ohne Blick auf Stichtag und Altbestand einzelner Dateien)?"""
    if body is None:
        return False
    source = getattr(body, "source", None)
    if source is None:
        return bool(body.is_listed)
    if not source_fetch_allowed(source):
        return False
    stichtag = stichtag_of(source)
    if stichtag.pending:
        return False
    if body.is_listed:
        return True
    return runner_is_worker() and stichtag.since is not None


def storable_q() -> Q:
    """Bedingung für Dateien, die abgelegt werden (wie ``stores_file``, für Abfragen)."""
    worker = runner_is_worker()
    bedingung = Q(body__isnull=False, body__source__isnull=True, body__is_listed=True)
    for source in OParlSource.objects.only("pk", "is_active", "sync_config"):
        if not source_fetch_allowed(source):
            continue
        stichtag = stichtag_of(source)
        if stichtag.pending:
            continue
        teil = Q(body__source_id=source.pk)
        if stichtag.since is None or not worker:
            teil &= Q(body__is_listed=True)
        if stichtag.since is not None and not stichtag.backfill:
            teil &= Q(created_at__gte=stichtag.since)
        bedingung |= teil
    return bedingung


def stichtage_setzen(*, now: datetime | None = None, ausfuehren: bool = True) -> dict[str, str]:
    """
    Stichtage nachtragen (idempotent); Rückgabe: Quelle → neuer Wert.

    - Quelle ohne Stichtag und ohne gelistete Kommune: der Zeitpunkt jetzt (Umschalten auf die Ablage für alle),
      solange ihr erster vollständiger Sync noch fehlt ``ausstehend``.
    - ``ausstehend`` mit beendetem vollständigem Sync (``last_successful_full_sync``): dessen Zeitpunkt.

    Quellen mit gelisteter Kommune behalten den leeren Stichtag (bisher abgelegt). Der Ingestor setzt den
    Stichtag nie.
    """
    now = now or timezone.now()
    gelistet = set(OParlBody.objects.filter(is_listed=True, source__isnull=False).values_list("source_id", flat=True))
    geaendert: dict[str, str] = {}
    for source in OParlSource.objects.only("pk", "sync_config", "last_successful_full_sync").order_by("pk"):
        config = source.sync_config if isinstance(source.sync_config, dict) else {}
        value = config.get(DOCUMENT_SINCE_KEY)
        if value in (None, ""):
            if source.pk in gelistet:
                continue
            neu = now.isoformat() if source.last_successful_full_sync else SINCE_PENDING
        elif value == SINCE_PENDING and source.last_successful_full_sync:
            neu = source.last_successful_full_sync.isoformat()
        else:
            continue
        geaendert[str(source.pk)] = neu
        if ausfuehren:
            _set_since(source.pk, value, neu)
    return geaendert


def _set_since(source_id: Any, expected: Any, value: str) -> None:
    """Stichtag schreiben, nur wenn er noch ``expected`` ist (Zeile gesperrt, übrige Einstellungen bleiben)."""
    with transaction.atomic():
        source = OParlSource.objects.select_for_update().filter(pk=source_id).first()
        if source is None:
            return
        config = dict(source.sync_config) if isinstance(source.sync_config, dict) else {}
        if config.get(DOCUMENT_SINCE_KEY) != expected:
            return
        config[DOCUMENT_SINCE_KEY] = value
        source.sync_config = config
        source.save(update_fields=["sync_config"])


# =============================================================================
# Auswahl, Beanspruchung, Zustände
# =============================================================================


def not_excluded_q(prefix: str = "") -> Q:
    """Nicht gelöscht, nicht gesperrt (#787), Inhalt nicht nach #787 gelöscht."""
    return Q(
        **{
            f"{prefix}deleted": False,
            f"{prefix}source_missing_since__isnull": True,
            f"{prefix}content_purged_at__isnull": True,
        }
    )


def is_excluded(file_obj: Any) -> bool:
    return bool(
        getattr(file_obj, "deleted", False)
        or getattr(file_obj, "source_missing_since", None)
        or getattr(file_obj, "content_purged_at", None)
    )


def due_q(now: datetime, *, include_errors: bool = False, force: bool = False) -> Q:
    """
    Zustände, aus denen ein Abruf beanspruchen darf: ``none``, fälliges ``retry``/``error``, verfallene
    Beanspruchung. ``include_errors``: auch nicht fällige ``error`` (``cache_files --retry-errors``); ``force``: jeder
    Zustand außer ``ok`` und einem laufenden Abruf (Admin-Aktion von Hand).
    """
    bedingung = (
        Q(local_status=NONE)
        | Q(local_status__in=[RETRY, ERROR], fetch_next_at__lte=now)
        | Q(local_status=FETCHING, fetch_next_at__lt=now)
    )
    if include_errors:
        bedingung |= Q(local_status=ERROR)
    if force:
        bedingung |= Q(local_status__in=[RETRY, ERROR, MISSING, REFUSED, TOO_LARGE])
    return bedingung


def is_being_fetched(file_obj: Any, now: datetime | None = None) -> bool:
    """Läuft gerade ein Abruf (Beanspruchung nicht verfallen)? Vorschau und Löschabgleich rufen dann nicht ab."""
    if getattr(file_obj, "local_status", None) != FETCHING:
        return False
    until = getattr(file_obj, "fetch_next_at", None)
    return until is None or until > (now or timezone.now())


@dataclass(frozen=True)
class Claim:
    """Beanspruchte Datei und ihr Zustand davor (zum Wiederherstellen)."""

    file_id: Any
    status: str
    next_at: datetime | None
    attempts: int


def claim(
    file_id: Any, *, now: datetime | None = None, include_errors: bool = False, force: bool = False
) -> Claim | None:
    """Datei atomar beanspruchen (``fetching``); ``None``, wenn sie schon beansprucht, erledigt oder ausgeschlossen ist."""
    now = now or timezone.now()
    with transaction.atomic():
        row = (
            OParlFile.objects.select_for_update(skip_locked=True)
            .filter(pk=file_id)
            .filter(not_excluded_q())
            .filter(due_q(now, include_errors=include_errors, force=force))
            .values("local_status", "fetch_next_at", "fetch_attempts")
            .first()
        )
        if row is None:
            return None
        OParlFile.objects.filter(pk=file_id).update(local_status=FETCHING, fetch_next_at=now + claim_duration())
    status = row["local_status"]
    if status == FETCHING:
        # Verfallene Beanspruchung: der Zustand davor ist unbekannt
        status = RETRY if row["fetch_attempts"] else NONE
    return Claim(file_id, status, row["fetch_next_at"], int(row["fetch_attempts"] or 0))


def restore(beanspruchung: Claim) -> None:
    """Abruf ohne Ergebnis beendet: Zustand vor der Beanspruchung wiederherstellen (nur, wenn noch beansprucht)."""
    OParlFile.objects.filter(pk=beanspruchung.file_id, local_status=FETCHING).update(
        local_status=beanspruchung.status, fetch_next_at=beanspruchung.next_at
    )


def release_stale(now: datetime | None = None) -> int:
    """Liegen gebliebene Beanspruchungen freigeben (Absturz eines Abrufs). Rückgabe: Anzahl."""
    now = now or timezone.now()
    verfallen = Q(local_status=FETCHING) & (Q(fetch_next_at__lt=now) | Q(fetch_next_at__isnull=True))
    wiederholen = OParlFile.objects.filter(verfallen, fetch_attempts__gt=0).update(
        local_status=RETRY, fetch_next_at=now
    )
    neu = OParlFile.objects.filter(verfallen).update(local_status=NONE, fetch_next_at=None)
    if wiederholen or neu:
        logger.warning("Abruf: %d liegen gebliebene Beanspruchungen freigegeben", wiederholen + neu)
    return wiederholen + neu


def failure_state(code: str, attempts: int, created_at: datetime | None, now: datetime) -> tuple[str, datetime | None]:
    """Zustand und nächster Versuch nach dem ``attempts``-ten Fehlschlag in Folge (Tabelle in ADR Abschnitt 4)."""
    if code == CODE_NOT_FOUND:
        young = created_at is not None and created_at >= now - NOT_FOUND_GRACE
        if young and attempts <= len(BACKOFF):
            return RETRY, now + BACKOFF[attempts - 1]
        return MISSING, None
    if code in RETRY_CODES:
        if attempts <= len(BACKOFF):
            return RETRY, now + BACKOFF[attempts - 1]
        return ERROR, now + ERROR_RETRY
    if code in REFUSED_CODES:
        return REFUSED, None
    if code == CODE_TOO_LARGE:
        return TOO_LARGE, None
    return ERROR, now + ERROR_RETRY


def _source_label(file_obj: Any) -> str:
    body = getattr(file_obj, "body", None)
    source_id = getattr(body, "source_id", None) if body is not None else None
    return str(source_id) if source_id else "-"


def _record_failure(file_obj: Any, condition: Q, attempts: int, code: str, message: str, now: datetime) -> str | None:
    state, next_at = failure_state(code, attempts, getattr(file_obj, "created_at", None), now)
    werte = {
        "local_status": state,
        "local_error": message.replace("\x00", "")[:500],
        "fetch_error": code,
        "fetch_attempts": attempts,
        "fetch_next_at": next_at,
    }
    if not OParlFile.objects.filter(condition, pk=file_obj.pk).update(**werte):
        return None
    for name, wert in werte.items():
        setattr(file_obj, name, wert)
    FETCH_ERRORS.labels(source=_source_label(file_obj), code=code).inc()
    if state == ERROR and code in RETRY_CODES:
        logger.info(
            "Datei %s: Abruf nach %d Versuchen aufgegeben (%s), neuer Versuch in einer Woche",
            file_obj.pk,
            attempts,
            code,
        )
    return state


def fail(file_obj: Any, beanspruchung: Claim, code: str, message: str, now: datetime | None = None) -> str:
    """Fehlschlag eines beanspruchten Abrufs vermerken; Rückgabe: neuer Zustand."""
    now = now or timezone.now()
    state = _record_failure(file_obj, Q(local_status=FETCHING), beanspruchung.attempts + 1, code, message, now)
    return state or SKIPPED


def record_live_failure(file_obj: Any, code: str, message: str, now: datetime | None = None) -> str | None:
    """
    Fehlschlag eines Live-Abrufs der Vorschau vermerken – mit denselben Zuständen, ohne Beanspruchung.

    Nur wenn ein Abruf jetzt beanspruchen dürfte (``none`` bzw. fälliges ``retry``/``error``): Viele Aufrufe einer
    Vorschau zählen so nicht als viele Fehlschläge, und ein laufender Abruf wird nicht überschrieben.
    """
    now = now or timezone.now()
    condition = not_excluded_q() & (Q(local_status=NONE) | Q(local_status__in=[RETRY, ERROR], fetch_next_at__lte=now))
    attempts = int(getattr(file_obj, "fetch_attempts", 0) or 0) + 1
    return _record_failure(file_obj, condition, attempts, code, message, now)


def mark_present(file_obj: Any) -> None:
    """Die Kopie liegt (wieder) vor: ``ok`` und Zähler des Abrufs zurück."""
    jetzt = timezone.now()
    OParlFile.objects.filter(pk=file_obj.pk).update(
        local_status=OK, local_error="", local_cached_at=jetzt, fetch_attempts=0, fetch_next_at=None, fetch_error=""
    )
    file_obj.local_status = OK
    file_obj.local_error = ""
    file_obj.local_cached_at = jetzt
    file_obj.fetch_attempts = 0
    file_obj.fetch_next_at = None
    file_obj.fetch_error = ""


def mark_content_missing(file_obj: Any) -> bool:
    """
    Bestätigt fehlende Kopie (weder lokal noch im Objektspeicher): ``ok`` → ``none``, damit der Abruf sie holt.

    Nur nach bestätigtem Fehlen (``file_store.local_copy`` mit Zustand „fehlt“), nie bei einer Störung.
    """
    if not OParlFile.objects.filter(pk=file_obj.pk, local_status=OK).update(local_status=NONE, fetch_next_at=None):
        return False
    file_obj.local_status = NONE
    logger.info("Datei %s: Kopie weder lokal noch im Objektspeicher, wird neu abgerufen", file_obj.pk)
    return True


def reopen_missing(file_obj: Any) -> bool:
    """Die Quelle liefert eine als ``missing`` vermerkte Datei wieder: zurück auf ``none`` (Löschabgleich)."""
    if not OParlFile.objects.filter(pk=file_obj.pk, local_status=MISSING).update(
        local_status=NONE, local_error="", fetch_attempts=0, fetch_next_at=None, fetch_error=""
    ):
        return False
    file_obj.local_status = NONE
    return True


# =============================================================================
# Download (der eine Weg zur Quelle)
# =============================================================================


class TooLargeError(Exception):
    """Die Datei überschreitet die Größengrenze (``FILE_CACHE_MAX_MB``)."""


@dataclass
class Antwort:
    """Ergebnis eines GET: Status, bei 200 Größe, SHA-256, Inhaltstyp und Anfang der Datei."""

    status: int
    size: int = 0
    sha256: str = ""
    content_type: str = ""
    head: bytes = b""


def http_timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


def fetch_client() -> httpx.Client:
    """Client für Abrufe bei Quellen: nur öffentliche Ziele, auch nach Weiterleitungen."""
    return safe_fetch.guarded_client(
        headers={"User-Agent": file_cache.USER_AGENT}, timeout=http_timeout(), follow_redirects=True
    )


def stream(
    client: httpx.Client,
    url: str,
    target: IO[bytes],
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
) -> Antwort:
    """
    GET ``url`` gestreamt nach ``target`` (nie die ganze Datei im Speicher), dabei hashen.

    Bei einem Status außer 200 wird nichts geschrieben. ``TooLargeError`` über ``max_bytes`` (Standard
    ``FILE_CACHE_MAX_MB``), Netzfehler als ``httpx.HTTPError``.
    """
    grenze = file_cache.max_bytes() if max_bytes is None else max_bytes
    with client.stream("GET", url, headers=headers or {}) as response:
        if response.status_code != 200:
            return Antwort(response.status_code)
        if int(response.headers.get("content-length") or 0) > grenze:
            raise TooLargeError
        digest = hashlib.sha256()
        size = 0
        head = b""
        for chunk in response.iter_bytes(1024 * 1024):
            size += len(chunk)
            if size > grenze:
                raise TooLargeError
            digest.update(chunk)
            target.write(chunk)
            if len(head) < 512:
                head += chunk[: 512 - len(head)]
        return Antwort(200, size, digest.hexdigest(), response.headers.get("content-type", ""), head)


def head(client: httpx.Client, url: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """HEAD-Anfrage (Stichprobe des Löschabgleichs)."""
    return client.head(url, headers=headers or {})


def download_live(
    file_obj: Any, url: str, target: IO[bytes], *, total_seconds: float, read_timeout: float
) -> safe_fetch.Download:
    """
    Live-Abruf der Vorschau: kurze Zeitgrenzen, Größengrenze, Download-Header und User-Agent der Quelle.

    Wirft wie ``safe_fetch.download_to`` (``httpx.HTTPStatusError``, ``httpx.RequestError``, ``TooLargeError``,
    ``DeadlineExceededError``). robots.txt und Drossel prüft die Vorschau vorher selbst (mit Höchstwartezeit).
    """
    return safe_fetch.download_to(
        target,
        url,
        max_bytes=file_cache.max_bytes(),
        total_seconds=total_seconds,
        timeout=httpx.Timeout(connect=5.0, read=read_timeout, write=5.0, pool=5.0),
        headers=file_cache.download_headers(file_obj.body),
        user_agent=robots.user_agent_for(file_obj),
    )


def download_to_file(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 120.0,
    extra_headers: dict[str, str] | None = None,
    sync_config: Any = None,
) -> DownloadedFile:
    """
    Datei nach robots.txt und Drossel je Host gestreamt in eine temporäre Datei laden und dabei hashen (bisher
    ``document_extraction.download_to_file``).

    Nie liegt die ganze Datei im Speicher; die Größengrenze greift während des Downloads
    (``DocumentTooLargeError``). Fehler der Quelle: ``DocumentDownloadError``; robots.txt und Drossel wie
    ``document_extraction.prepare_fetch``.
    """
    agent, headers = prepare_fetch(url, extra_headers, sync_config, None)
    purge_now_and_then()
    handle, name = tempfile.mkstemp(suffix=".part", prefix=TEMP_FILE_PREFIX)
    path = Path(name)
    try:
        with os.fdopen(handle, "wb") as target:
            result = safe_fetch.download_to(
                target,
                url,
                max_bytes=max_bytes,
                total_seconds=timeout,
                timeout=httpx.Timeout(timeout),
                headers=headers,
                user_agent=agent,
            )
    except safe_fetch.TooLargeError as exc:
        path.unlink(missing_ok=True)
        raise DocumentTooLargeError(f"Datei größer als {max_bytes // 1024 // 1024} MB") from exc
    except (httpx.HTTPError, safe_fetch.DeadlineExceededError) as exc:
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


def code_for_status(status: int) -> str:
    """Fehlercode einer HTTP-Antwort außer 200."""
    if status in GONE_STATUS:
        return CODE_NOT_FOUND
    if status == 429:
        return CODE_THROTTLED
    if status >= 500:
        return CODE_SERVER
    return CODE_CLIENT


def code_for_exception(exc: BaseException) -> str:
    """Fehlercode eines Netzfehlers."""
    if isinstance(exc, safe_fetch.BlockedDestinationError):
        return CODE_BLOCKED_DESTINATION
    if isinstance(exc, httpx.TimeoutException | safe_fetch.DeadlineExceededError):
        return CODE_TIMEOUT
    return CODE_CONNECTION


# =============================================================================
# Ablegen
# =============================================================================


def ablegen(file_obj: Any, source: IO[bytes], *, content_type: str | None = None, upload: bool = True) -> Path:
    """
    Inhalt ablegen (``ok``, Zähler zurück) und sofort in den Objektspeicher laden; scheitert das, holt es der
    Zeitplan nach. ``upload=False`` nur in Web-Anfragen (Vorschau): Der Besucher wartet nicht auf den Upload, und
    ``evict_local`` verdrängt ohnehin nur Inhalte, die schon im Objektspeicher liegen.
    """
    path = file_cache.store_stream(file_obj, source, content_type=content_type)
    sha256 = getattr(file_obj, "blob_id", None)
    if upload and sha256:
        file_store.upload_now(sha256)
    return path


# =============================================================================
# Ein Abruf
# =============================================================================


def abrufen(
    file_obj: Any,
    *,
    client: httpx.Client | None = None,
    include_errors: bool = False,
    force: bool = False,
    max_wait: float | None = None,
    now: datetime | None = None,
) -> str:
    """
    Datei bei der Quelle abrufen und ablegen. Rückgabe: neuer Zustand (``ok``, ``retry``, ``missing``,
    ``refused``, ``too_large``, ``error``) oder ein Ergebnis ohne Zustand (``skipped``, ``paused``, ``excluded``,
    ``disk_full``, ``storage_error``, ``busy``).

    ``include_errors``: auch nicht fällige ``error`` (``cache_files --retry-errors``); ``force``: jeder Zustand außer
    ``ok`` (Admin-Aktion von Hand). ``max_wait``: so lange höchstens auf die Drossel je Host warten (sonst ``busy``,
    Zustand wiederhergestellt).
    """
    now = now or timezone.now()
    if is_excluded(file_obj):
        return EXCLUDED
    body = getattr(file_obj, "body", None)
    if not fetch_allowed(body) or file_cache.source_paused(body):
        # Die Quelle wartet als Ganzes, nicht jede Datei einzeln: kein Versuch, kein Zustand
        return PAUSED
    if file_obj.local_status == OK:
        kopie = file_store.local_copy(file_obj)
        if kopie.present:
            return SKIPPED
        if kopie.disturbed:
            # Eine Störung der Ablage löst nie einen Abruf bei der Quelle aus
            return STORAGE_ERROR
        mark_content_missing(file_obj)
    beanspruchung = claim(file_obj.pk, now=now, include_errors=include_errors, force=force)
    if beanspruchung is None:
        return SKIPPED
    file_obj.local_status = FETCHING
    try:
        return _abrufen_beansprucht(file_obj, beanspruchung, client, max_wait, now)
    except BaseException:
        restore(beanspruchung)
        raise


def _abrufen_beansprucht(
    file_obj: Any, beanspruchung: Claim, client: httpx.Client | None, max_wait: float | None, now: datetime
) -> str:
    url = file_obj.download_url or file_obj.access_url
    if not url:
        return fail(file_obj, beanspruchung, CODE_NO_URL, "Keine Download-URL", now)
    if file_cache.local_file(file_obj) is not None:
        mark_present(file_obj)
        return SKIPPED
    sync_config = robots.sync_config_of(file_obj)
    try:
        decision = robots.check(
            url, robots.KIND_FILES, sync_config=sync_config, agent=robots.user_agent_for(file_obj), max_wait=max_wait
        )
    except host_pacing.PacingBusyError:
        restore(beanspruchung)
        return BUSY
    if decision.unreachable:
        return fail(
            file_obj, beanspruchung, CODE_ROBOTS_UNREACHABLE, decision.reason or "robots.txt nicht erreichbar", now
        )
    if not decision.allowed:
        return fail(file_obj, beanspruchung, CODE_ROBOTS, decision.reason or ROBOTS_TEXT, now)
    if not file_cache.has_room_for(0):
        restore(beanspruchung)
        return DISK_FULL
    # Drossel je Host über alle Prozesse (Ingestor, Vorschau, andere Quellen auf dem Host)
    if not host_pacing.wait(url, sync_config=sync_config, max_wait=max_wait):
        restore(beanspruchung)
        return BUSY

    own_client = client is None
    aktiv = fetch_client() if client is None else client
    # Gestreamt in eine temporäre Datei, nie die ganze Datei im Speicher (#788)
    buffer = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024)  # noqa: SIM115 - unten geschlossen
    try:
        try:
            antwort = stream(aktiv, url, buffer, headers=file_cache.download_headers(file_obj.body))
        except TooLargeError:
            return fail(file_obj, beanspruchung, CODE_TOO_LARGE, f"> {file_cache.max_bytes() // 1024 // 1024} MB", now)
        except httpx.HTTPError as exc:
            return fail(file_obj, beanspruchung, code_for_exception(exc), type(exc).__name__, now)
        if antwort.status != 200:
            return fail(file_obj, beanspruchung, code_for_status(antwort.status), f"HTTP {antwort.status}", now)
        if not antwort.size:
            return fail(file_obj, beanspruchung, CODE_EMPTY, "Leere Antwort", now)
        if looks_like_html(antwort.head) and "html" not in (file_obj.mime_type or "").lower():
            return fail(file_obj, beanspruchung, CODE_HTML, HTML_TEXT, now)
        if not file_cache.has_room_for(antwort.size):
            restore(beanspruchung)
            return DISK_FULL
        buffer.seek(0)
        try:
            ablegen(file_obj, buffer, content_type=antwort.content_type)
        except OSError:
            logger.warning("Datei %s: Ablage gestört, Abruf später erneut", file_obj.pk, exc_info=True)
            restore(beanspruchung)
            return STORAGE_ERROR
        return OK
    finally:
        buffer.close()
        if own_client:
            aktiv.close()


# =============================================================================
# Lauf des Dokument-Caches (Zeitplan ``cache_files``)
# =============================================================================


def pending_queryset(body: Any = None, *, retry_errors: bool = False, now: datetime | None = None) -> QuerySet[Any]:
    """
    Dateien, die ein Lauf abrufen soll: abzulegen, nicht ausgeschlossen, fällig und nicht aus Quellen in Schonung.
    Fällige Wiederholungen zuerst, dann die neuesten Dateien (neue Dateien vor dem Altbestand).
    """
    now = now or timezone.now()
    # text_content/raw_json sind riesig (extrahierter Volltext) — nie mitladen
    qs = (
        OParlFile.objects.filter(not_excluded_q())
        .filter(due_q(now, include_errors=retry_errors))
        .filter(storable_q())
        .exclude(body__source__consecutive_failures__gte=file_cache.backoff_failures())
        .select_related("body", "body__source")
        .defer("text_content", "raw_json", "body__raw_json")
    )
    if file_store.uses_blobs() and getattr(settings, "INGESTOR_STORES_FILES", False) and not runner_is_worker():
        # Dateien, deren Text der Ingestor noch erkennt, legt er beim selben Abruf selbst ab (ein Abruf je
        # Datei, #788). Maßgeblich ist die letzte Änderung des Datensatzes; hängt die Erkennung länger als einen
        # Tag, holt der Cache sie. Mit TEXT_EXTRACTION_RUNNER=worker ruht der Ingestor (ohne Wirkung).
        recent = now - timedelta(days=1)
        qs = qs.exclude(text_extraction_status__in=["pending", "processing"], updated_at__gte=recent)
    if body is not None:
        qs = qs.filter(body=body)
    vorrang = Case(When(local_status=NONE, then=Value(1)), default=Value(0), output_field=IntegerField())
    return qs.annotate(abruf_vorrang=vorrang).order_by("abruf_vorrang", "-file_date", "-oparl_created", "-created_at")


def nachladen(body: Any = None, *, limit: int = 500, retry_errors: bool = False, sleep: float = 0.05) -> Counter[str]:
    """
    Fehlende Kopien nachladen (``cache_files``): liegen gebliebene Beanspruchungen freigeben, Stichtage nachtragen
    (nur mit ``TEXT_EXTRACTION_RUNNER=worker``), dann höchstens ``limit`` Abrufe, je Quelle höchstens
    ``DOCUMENT_FETCH_MAX_QUEUED``. Jeder Abruf beansprucht selbst; der Lauf wählt nur aus.
    """
    results: Counter[str] = Counter()
    release_stale()
    if runner_is_worker():
        stichtage_setzen()
    if not file_cache.has_room_for(0):
        results[DISK_FULL] += 1
        return results
    grenze = max_queued()
    je_quelle: Counter[Any] = Counter()
    erledigt = 0
    with fetch_client() as client:
        for file_obj in _kandidaten(body, retry_errors, limit):
            if erledigt >= limit:
                break
            schluessel = file_obj.body.source_id if file_obj.body is not None else None
            if je_quelle[schluessel] >= grenze:
                continue
            je_quelle[schluessel] += 1
            status = abrufen(file_obj, client=client, include_errors=retry_errors)
            results[status] += 1
            erledigt += 1
            if status == DISK_FULL:
                break
            if sleep:
                time.sleep(sleep)
    return results


def _kandidaten(body: Any, retry_errors: bool, limit: int) -> Iterable[Any]:
    # Ein Fenster über dem Limit, damit die Grenze je Quelle andere Quellen nachrücken lässt
    return pending_queryset(body, retry_errors=retry_errors)[: max(limit, 1) * 10].iterator(chunk_size=200)


# =============================================================================
# Freigeben und Zurücksetzen
# =============================================================================


def freigeben(source: Any, *, code: str | None = None) -> int:
    """Verweigerte Abrufe einer Quelle neu einreihen (``refused`` → ``none``), optional nur ein Fehlercode."""
    dateien = OParlFile.objects.filter(body__source=source, local_status=REFUSED)
    if code:
        dateien = dateien.filter(fetch_error=code)
    return dateien.update(local_status=NONE, local_error="", fetch_attempts=0, fetch_next_at=None, fetch_error="")


def zuruecksetzen() -> dict[str, int]:
    """
    Zustände für ein älteres Image zurücksetzen (Rückfall ohne Rückbau der Migration; idempotent): ``retry`` und
    ``fetching`` → ``none``, ``refused`` → ``error`` mit dem Fehlertext, an dem ein älteres Image die Sperre
    erkennt (robots.txt-Präfix bzw. „HTML statt Datei“). Spalten und übrige Inhalte bleiben.
    """
    prefix = robots.SKIP_ERROR_PREFIX
    verweigert = OParlFile.objects.filter(local_status=REFUSED)
    robots_mit_text = verweigert.filter(fetch_error=CODE_ROBOTS, local_error__startswith=prefix).update(
        local_status=ERROR
    )
    robots_ohne_text = verweigert.filter(fetch_error=CODE_ROBOTS).update(local_status=ERROR, local_error=ROBOTS_TEXT)
    html = verweigert.filter(fetch_error=CODE_HTML).update(local_status=ERROR, local_error=HTML_TEXT)
    sonstige = verweigert.update(local_status=ERROR)
    offen = OParlFile.objects.filter(local_status__in=[RETRY, FETCHING]).update(local_status=NONE, fetch_next_at=None)
    return {
        "verweigert_robots": robots_mit_text + robots_ohne_text,
        "verweigert_html": html,
        "verweigert_sonstige": sonstige,
        "wiederholung_oder_laufend": offen,
    }


# =============================================================================
# Kennzahlen und Prüfung
# =============================================================================


def retry_overdue_q(now: datetime) -> Q:
    """Fällige Wiederholungen, die länger als ``DOCUMENT_FETCH_RETRY_ALERT_HOURS`` liegen."""
    stunden = float(getattr(settings, "DOCUMENT_FETCH_RETRY_ALERT_HOURS", 6))
    return Q(local_status=RETRY, fetch_next_at__lt=now - timedelta(hours=stunden))


def counts(now: datetime | None = None) -> dict[str, int]:
    """Wartende Abrufe und fällige Wiederholungen (nur Dateien, die abgelegt werden), eine Abfrage."""
    now = now or timezone.now()
    zahlen = (
        OParlFile.objects.filter(not_excluded_q())
        .filter(storable_q())
        .filter(due_q(now))
        .aggregate(queued=Count("pk"), retry_due=Count("pk", filter=Q(local_status=RETRY)))
    )
    return {"queued": int(zahlen["queued"] or 0), "retry_due": int(zahlen["retry_due"] or 0)}


def overdue_retries(now: datetime | None = None) -> int:
    """Fällige Wiederholungen, die länger als die Schwelle liegen – ohne Quellen in Schonung und gesperrte Quellen."""
    now = now or timezone.now()
    return (
        OParlFile.objects.filter(not_excluded_q())
        .filter(retry_overdue_q(now))
        .filter(storable_q())
        .exclude(body__source__consecutive_failures__gte=file_cache.backoff_failures())
        .count()
    )
