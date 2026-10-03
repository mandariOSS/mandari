# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Löschabgleich der Dokumente mit den Quellen (Issue #787).

Was eine Kommune aus ihrem Ratsinformationssystem entfernt oder ersetzt, verschwindet auch bei uns:
zuerst öffentlich gesperrt, nach einer Frist gelöscht.

* **Gesperrt** ist ein Dokument, sobald die Quelle es als gelöscht meldet (OParl ``deleted``, vom
  Ingestor als ``deleted`` markiert) oder seine Download-Adresse ``404``/``410`` liefert
  (``source_missing_since``). Gesperrt heißt: keine Bytes in der Vorschau, kein Text auf der
  Vorgangsseite und in der OParl-Ausgabe, nicht im Suchindex, keine KI-Zusammenfassung mit seinem Inhalt.
* **Geändert:** Meldet die Quelle eine Änderung (``oparl_modified``) nach unserer Kopie bzw. nach der
  Texterkennung, laden wir die Datei neu und vergleichen den SHA-256. Anderer Inhalt ersetzt Kopie
  und Text (der Ingestor erkennt den Text neu), gleicher Inhalt ändert nichts.
* **Stichproben:** Gedrosselte HEAD-Anfragen auf die Download-Adressen finden Löschungen und
  Änderungen, die die Quelle nicht meldet. Ein ``404``/``410`` bestätigt ein GET, bevor gesperrt wird;
  eine abweichende Größe oder eine HTML-Seite statt der Datei führt zum Abgleich per Hash.
* **Erneut prüfen:** Ein wegen ``404``/``410`` gesperrtes Dokument prüfen wir nach 1, 7 und 25 Tagen
  erneut und unmittelbar vor dem Löschen noch einmal per GET. Liefert die Quelle es wieder, heben wir
  die Sperre auf, statt zu löschen. Eine vorübergehende ``404`` (Wartung, Umstellung) kostet so nur Zeit.
* **Bremse:** Liefern in einem Lauf mehr als ``FILE_RECONCILE_MAX_MISSING`` Dokumente einer Quelle neu
  ``404``/``410``, sperren wir keines davon und lassen die Quelle für den Lauf in Ruhe; der Lauf meldet
  das (``gebremst``). Eine kaputte Quelle sperrt so keine Dokumente in Massen.
* **Ruhe je Host:** Antwortet ein Host mit ``429``/``503`` oder fünfmal in Folge mit einem Fehler,
  fragen wir ihn in diesem Lauf nicht mehr an (auch kein GET hinterher).
* **Löschen:** Nach ``FILE_PURGE_AFTER_DAYS`` (Standard 30) Tagen Sperre entfernen wir die lokale
  Kopie und den extrahierten Text. Der Datensatz bleibt als Tombstone (Name, Adresse, Fingerabdruck).
  Lässt sich die Quelle vor dem Löschen nicht befragen (Fehler, robots.txt, Schonung), warten wir bis
  zu ``FILE_PURGE_CONFIRM_GRACE_DAYS`` (Standard 7) Tage und löschen danach ohne Rückfrage.

Abgefragt werden nur Quellen, die nicht in Schonung sind und Dateiabrufe zulassen; die robots.txt der
Quelle ist verbindlich (``services/file_robots.py``, Ausnahme nur mit Vermerk).
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.db.models import F, Q, QuerySet
from django.utils import timezone

from . import file_cache, file_robots

logger = logging.getLogger(__name__)

#: Ergebnisse eines Abgleichs
UNCHANGED = "unchanged"
CHANGED = "changed"
MISSING = "missing"
PRESENT = "present"
ERROR = "error"
ROBOTS = "robots"
SKIPPED = "skipped"
#: Host antwortet mit 429/503: in diesem Lauf nicht weiter anfragen
THROTTLED = "throttled"
#: Neu fehlende Dokumente einer Quelle über der Schwelle: nicht gesperrt (Bremse)
BRAKED = "gebremst"

#: Nach so vielen Fehlern in Folge lässt ein Lauf den Host in Ruhe
MAX_ERRORS_PER_HOST = 5
#: Ein wegen 404/410 gesperrtes Dokument nach so vielen Tagen erneut prüfen
RECHECK_DAYS = (1, 7, 25)
#: Statuscodes, mit denen ein Host bremst
_THROTTLE_STATUS = (429, 503)
#: Inhaltstypen einer Hinweis- oder Fehlerseite (weiche 404)
_HTML_TYPES = ("text/html", "application/xhtml+xml")


def purge_after_days() -> int:
    return int(getattr(settings, "FILE_PURGE_AFTER_DAYS", 30))


def purge_confirm_grace_days() -> int:
    """So viele Tage über die Frist hinaus wartet das Löschen auf eine Antwort der Quelle."""
    return int(getattr(settings, "FILE_PURGE_CONFIRM_GRACE_DAYS", 7))


def max_missing_per_run() -> int:
    """Höchstens so viele neu fehlende Dokumente je Quelle und Lauf werden gesperrt (sonst Bremse)."""
    return int(getattr(settings, "FILE_RECONCILE_MAX_MISSING", 10))


def blocked_q(prefix: str = "") -> Q:
    """Bedingung für gesperrte Dokumente (``prefix`` z. B. ``"files__"``)."""
    return Q(**{f"{prefix}deleted": True}) | Q(**{f"{prefix}source_missing_since__isnull": False})


def is_blocked(file_obj: Any) -> bool:
    """In der Quelle gelöscht oder dort nicht mehr abrufbar?"""
    return bool(getattr(file_obj, "deleted", False) or getattr(file_obj, "source_missing_since", None))


# =============================================================================
# Sperren, Entsperren, Ersetzen
# =============================================================================


def _forget_summary(file_obj: Any) -> None:
    from ..models import OParlPaper

    if file_obj.paper_id:
        OParlPaper.objects.filter(pk=file_obj.paper_id, summary__isnull=False).update(summary=None)


def _drop_from_index(file_obj: Any) -> None:
    from ..signals import remove_file_from_index

    remove_file_from_index(file_obj)


def mark_missing(file_obj: Any, now: datetime) -> bool:
    """Download-Adresse liefert 404/410: sperren (einmalig) und aus Suche und Zusammenfassung nehmen."""
    from ..models import OParlFile

    marked = OParlFile.objects.filter(pk=file_obj.pk, source_missing_since__isnull=True).update(
        source_missing_since=now, source_checked_at=now
    )
    if not marked:
        OParlFile.objects.filter(pk=file_obj.pk).update(source_checked_at=now)
    file_obj.source_missing_since = file_obj.source_missing_since or now
    file_obj.source_checked_at = now
    if marked:
        _drop_from_index(file_obj)
        _forget_summary(file_obj)
        logger.info("Dokument %s: Quelle liefert es nicht mehr, gesperrt", file_obj.pk)
    return bool(marked)


def mark_present(file_obj: Any, now: datetime) -> None:
    """Quelle liefert das Dokument: Prüfzeitpunkt setzen, eine Sperre wegen 404 aufheben."""
    from ..models import OParlFile

    was_missing = bool(file_obj.source_missing_since)
    file_obj.source_checked_at = now
    file_obj.source_missing_since = None
    fields = ["source_checked_at", "source_missing_since"]
    if was_missing and file_obj.content_purged_at:
        # Kopie und Text waren schon gelöscht: neu erkennen lassen, die Kopie holt cache_files nach
        file_obj.content_purged_at = None
        file_obj.text_extraction_status = "pending"
        fields += ["content_purged_at", "text_extraction_status"]
    if was_missing:
        # Speichern über das Modell: die Signale nehmen das Dokument wieder in die Suche auf
        file_obj.save(update_fields=fields)
        logger.info("Dokument %s: Quelle liefert es wieder, Sperre aufgehoben", file_obj.pk)
    else:
        OParlFile.objects.filter(pk=file_obj.pk).update(source_checked_at=now)


def _reset_text(file_obj: Any) -> list[str]:
    file_obj.text_content = None
    file_obj.text_extraction_status = "pending"
    file_obj.text_extraction_method = None
    file_obj.text_extraction_error = None
    file_obj.text_extracted_at = None
    file_obj.page_count = None
    return [
        "text_content",
        "text_extraction_status",
        "text_extraction_method",
        "text_extraction_error",
        "text_extracted_at",
        "page_count",
    ]


def replace_content(file_obj: Any, source: IO[bytes], sha256: str, content_type: str, now: datetime) -> None:
    """Die Quelle liefert einen anderen Inhalt: Kopie ersetzen, Text verwerfen (wird neu erkannt)."""
    old_path = file_obj.local_path if file_obj.local_status == "ok" else None
    if old_path:
        if file_cache.caches_body(file_obj.body) and file_cache.has_room_for(0):
            new_path = file_cache.store_stream(file_obj, source, content_type=content_type)
            if Path(old_path) != new_path:
                Path(old_path).unlink(missing_ok=True)
        else:
            # Neue Fassung lässt sich nicht ablegen: die alte darf trotzdem nicht bleiben
            Path(old_path).unlink(missing_ok=True)
            file_obj.local_path = None
            file_obj.local_size = None
            file_obj.local_status = "none"
            file_obj.save(update_fields=["local_path", "local_size", "local_status"])
    file_obj.sha256_hash = sha256
    file_obj.source_checked_at = now
    file_obj.source_missing_since = None
    fields = ["sha256_hash", "source_checked_at", "source_missing_since", *_reset_text(file_obj)]
    # Über das Modell speichern: das Signal nimmt den alten Text aus dem Suchindex
    file_obj.save(update_fields=fields)
    _forget_summary(file_obj)
    logger.info("Dokument %s: Inhalt in der Quelle geändert, Kopie und Text ersetzt", file_obj.pk)


# =============================================================================
# Ein Lauf: Drossel, Ruhe je Host, Bremse je Quelle
# =============================================================================


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _source_key(file_obj: Any) -> Any:
    body = file_obj.body
    if body is None:
        return None
    return body.source_id or body.pk


class _Pacer:
    """Mindestabstand zwischen zwei Anfragen an denselben Host."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = _host(url)
        last = self._last.get(host)
        if last is not None and self.interval > 0:
            pause = self.interval - (time.monotonic() - last)
            if pause > 0:
                time.sleep(pause)
        self._last[host] = time.monotonic()


class Run:
    """
    Zustand eines Laufs über alle Schritte: Mindestabstand je Host, Ruhe je Host (429/503, Fehler in
    Folge) und die Bremse je Quelle (neu fehlende Dokumente werden erst am Ende gesperrt).
    """

    def __init__(self, interval: float = 2.0, max_missing: int | None = None) -> None:
        self.pacer = _Pacer(interval)
        self.max_missing = max_missing_per_run() if max_missing is None else max_missing
        self._errors: Counter[str] = Counter()
        self.resting: set[str] = set()
        self._pending: dict[Any, list[tuple[Any, datetime]]] = defaultdict(list)
        #: Neu fehlende Dokumente je Quelle über alle Schritte des Laufs
        self._missing: Counter[Any] = Counter()
        self.braked: set[Any] = set()

    def may_fetch(self, file_obj: Any, url: str) -> bool:
        return _host(url) not in self.resting and _source_key(file_obj) not in self.braked

    def record(self, url: str, result: str) -> None:
        host = _host(url)
        if result == THROTTLED:
            if host not in self.resting:
                logger.info("Löschabgleich: %s bremst (429/503), Host ruht für diesen Lauf", host)
            self.resting.add(host)
        elif result == ERROR:
            self._errors[host] += 1
            if self._errors[host] >= MAX_ERRORS_PER_HOST:
                self.resting.add(host)
        else:
            self._errors[host] = 0

    def defer_missing(self, file_obj: Any, now: datetime) -> None:
        """Neu fehlendes Dokument vormerken; über der Schwelle greift die Bremse für die Quelle."""
        key = _source_key(file_obj)
        self._pending[key].append((file_obj, now))
        self._missing[key] += 1
        if self._missing[key] > self.max_missing and key not in self.braked:
            self.braked.add(key)
            logger.warning(
                "Löschabgleich: Quelle %s liefert in diesem Lauf mehr als %d Dokumente nicht mehr – "
                "nichts gesperrt, Quelle ruht für diesen Lauf (bitte prüfen)",
                key,
                self.max_missing,
            )

    def commit_missing(self, results: Counter[str]) -> None:
        """Vorgemerkte Dokumente sperren, außer bei gebremsten Quellen (dort zählen sie als ``gebremst``)."""
        for key, entries in self._pending.items():
            if key in self.braked:
                results[MISSING] -= len(entries)
                results[BRAKED] += len(entries)
                continue
            for file_obj, now in entries:
                mark_missing(file_obj, now)
        self._pending.clear()
        if results[MISSING] <= 0:
            del results[MISSING]


# =============================================================================
# Abrufe
# =============================================================================


def _url(file_obj: Any) -> str | None:
    return file_obj.download_url or file_obj.access_url or None


def _source_usable(file_obj: Any) -> bool:
    body = file_obj.body
    return not (file_cache.source_paused(body) or file_cache.downloads_disabled(body))


def _robots_allows(file_obj: Any, url: str, client: httpx.Client) -> bool:
    source = getattr(file_obj.body, "source", None) if file_obj.body is not None else None
    return file_robots.file_fetch_status(source, url, client) == file_robots.ALLOWED


class _TooLargeError(Exception):
    """Datei größer als ``FILE_CACHE_MAX_MB``."""


def _download(
    client: httpx.Client, url: str, target: IO[bytes], headers: dict[str, str]
) -> tuple[int, int, str, str, bytes]:
    """GET nach ``target``: (Status, Größe, SHA-256, Content-Type, Anfang der Datei)."""
    with client.stream("GET", url, headers=headers) as response:
        if response.status_code != 200:
            return response.status_code, 0, "", "", b""
        digest = hashlib.sha256()
        size = 0
        head = b""
        for chunk in response.iter_bytes(1024 * 1024):
            size += len(chunk)
            if size > file_cache.max_bytes():
                raise _TooLargeError
            digest.update(chunk)
            target.write(chunk)
            if len(head) < 512:
                head += chunk[: 512 - len(head)]
        return 200, size, digest.hexdigest(), response.headers.get("content-type", ""), head


def verify(file_obj: Any, client: httpx.Client, *, now: datetime | None = None, run: Run | None = None) -> str:
    """
    Datei neu laden und per SHA-256 mit unserem Stand vergleichen.

    Mit ``run`` wird ein neu fehlendes Dokument nur vorgemerkt und erst am Ende des Laufs gesperrt
    (Bremse je Quelle, ``Run.commit_missing``).
    """
    now = now or timezone.now()
    url = _url(file_obj)
    if not url or not _source_usable(file_obj):
        return SKIPPED
    if not _robots_allows(file_obj, url, client):
        return ROBOTS
    with tempfile.TemporaryFile() as tmp:
        try:
            status, size, sha256, content_type, head = _download(
                client, url, tmp, file_cache.download_headers(file_obj.body)
            )
        except _TooLargeError:
            return ERROR
        except httpx.HTTPError as exc:
            logger.info("Abgleich %s: %s", file_obj.pk, type(exc).__name__)
            return ERROR
        if status in (404, 410):
            if run is not None and not file_obj.source_missing_since:
                run.defer_missing(file_obj, now)
            else:
                mark_missing(file_obj, now)
            return MISSING
        if status in _THROTTLE_STATUS:
            return THROTTLED
        if status != 200 or not size:
            return ERROR
        if file_cache.looks_like_html(head) and "html" not in (file_obj.mime_type or "").lower():
            # Hinweis- oder Wartungsseite statt der Datei: nichts daraus schließen
            return ERROR
        if file_obj.sha256_hash == sha256:
            mark_present(file_obj, now)
            return UNCHANGED
        if (
            not file_obj.sha256_hash
            and file_obj.local_status != "ok"
            and file_obj.text_extraction_status != "completed"
        ):
            # Nichts von uns, das veraltet sein könnte: nur den Fingerabdruck merken
            file_obj.sha256_hash = sha256
            file_obj.save(update_fields=["sha256_hash"])
            mark_present(file_obj, now)
            return UNCHANGED
        tmp.seek(0)
        replace_content(file_obj, tmp, sha256, content_type.split(";")[0].strip(), now)
        return CHANGED


def head_check(file_obj: Any, client: httpx.Client, *, now: datetime | None = None, run: Run | None = None) -> str:
    """HEAD-Stichprobe: gelöscht (404/410, per GET bestätigt), vorhanden oder geändert (Größe, HTML)."""
    now = now or timezone.now()
    url = _url(file_obj)
    if not url or not _source_usable(file_obj):
        return SKIPPED
    if not _robots_allows(file_obj, url, client):
        return ROBOTS
    headers = file_cache.download_headers(file_obj.body)
    try:
        response = client.head(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.info("Stichprobe %s: %s", file_obj.pk, type(exc).__name__)
        return ERROR
    if response.status_code in _THROTTLE_STATUS:
        # Der Host bremst: kein GET hinterher, der Lauf lässt ihn in Ruhe
        return THROTTLED
    if response.status_code >= 400:
        # HEAD sagt bei manchen Servern wenig (405, 404 nur für HEAD): GET entscheidet
        return verify(file_obj, client, now=now, run=run)
    if response.status_code != 200:
        return ERROR
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type in _HTML_TYPES and "html" not in (file_obj.mime_type or "").lower():
        # Weiche 404: Hinweisseite mit 200 statt der Datei. Nie als „vorhanden“ werten, GET entscheidet
        return verify(file_obj, client, now=now, run=run)
    declared = response.headers.get("content-length")
    known = file_obj.local_size or file_obj.size
    if declared and declared.isdigit() and known and int(declared) != int(known):
        return verify(file_obj, client, now=now, run=run)
    mark_present(file_obj, now)
    return PRESENT


# =============================================================================
# Auswahl
# =============================================================================


def _base_queryset(body: Any = None) -> QuerySet[Any]:
    from ..models import OParlFile

    qs = (
        OParlFile.objects.filter(deleted=False, content_purged_at__isnull=True)
        .filter(Q(local_status="ok") | Q(text_extraction_status="completed"))
        .filter(Q(download_url__isnull=False) | Q(access_url__isnull=False))
        .exclude(body__source__consecutive_failures__gte=file_cache.backoff_failures())
        .exclude(body__source_id__in=file_cache.sources_without_downloads())
        .select_related("body", "body__source")
        .defer("text_content", "raw_json", "body__raw_json")
    )
    if body is not None:
        qs = qs.filter(body=body)
    return qs


def changed_queryset(body: Any = None) -> QuerySet[Any]:
    """Dokumente, die die Quelle nach unserer Kopie bzw. Texterkennung geändert hat."""
    stale_copy = Q(local_status="ok") & (Q(local_cached_at__isnull=True) | Q(local_cached_at__lt=F("oparl_modified")))
    stale_text = Q(text_extraction_status="completed") & (
        Q(text_extracted_at__isnull=True) | Q(text_extracted_at__lt=F("oparl_modified"))
    )
    return (
        _base_queryset(body)
        .filter(oparl_modified__isnull=False)
        .filter(Q(source_checked_at__isnull=True) | Q(source_checked_at__lt=F("oparl_modified")))
        .filter(stale_copy | stale_text)
        .order_by("-oparl_modified")
    )


def sample_queryset(body: Any) -> QuerySet[Any]:
    """Stichprobe einer Kommune: am längsten nicht geprüfte zuerst."""
    from ..models import OParlFile

    hosted = Q(content_purged_at__isnull=True) & (Q(local_status="ok") | Q(text_extraction_status="completed"))
    # Auch wegen 404 gesperrte Dokumente prüfen: liefert die Quelle sie wieder, wird entsperrt
    missing = Q(source_missing_since__isnull=False)
    return (
        OParlFile.objects.filter(body=body, deleted=False)
        .filter(hosted | missing)
        .filter(Q(download_url__isnull=False) | Q(access_url__isnull=False))
        .select_related("body", "body__source")
        .defer("text_content", "raw_json", "body__raw_json")
        .order_by(F("source_checked_at").asc(nulls_first=True), "pk")
    )


def recheck_queryset(body: Any, now: datetime | None = None) -> QuerySet[Any]:
    """
    Gesperrte Dokumente einer Kommune, deren erneute Prüfung fällig ist (nach 1, 7 und 25 Tagen Sperre).

    Fällig ist eine Stufe, wenn sie erreicht ist und seit Beginn der Sperre plus Stufe nicht geprüft wurde.
    """
    from ..models import OParlFile

    now = now or timezone.now()
    due = Q()
    for days in RECHECK_DAYS:
        stage = timedelta(days=days)
        due |= Q(source_missing_since__lte=now - stage) & (
            Q(source_checked_at__isnull=True) | Q(source_checked_at__lt=F("source_missing_since") + stage)
        )
    return (
        OParlFile.objects.filter(
            body=body, deleted=False, content_purged_at__isnull=True, source_missing_since__isnull=False
        )
        .filter(due)
        .filter(Q(download_url__isnull=False) | Q(access_url__isnull=False))
        .select_related("body", "body__source")
        .defer("text_content", "raw_json", "body__raw_json")
        .order_by("source_missing_since", "pk")
    )


# =============================================================================
# Läufe
# =============================================================================


def fetch_client() -> httpx.Client:
    from .safe_fetch import guarded_client

    timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
    return guarded_client(headers={"User-Agent": file_cache.USER_AGENT}, timeout=timeout, follow_redirects=True)


def _check_each(
    files: Iterable[Any], check: Any, client: httpx.Client, run: Run, results: Counter[str], limit: int | None = None
) -> None:
    """``check`` (``verify`` oder ``head_check``) für jede Datei, gedrosselt und mit Ruhe je Host."""
    seen: set[Any] = set()
    for file_obj in files:
        if limit is not None and len(seen) >= limit:
            break
        if file_obj.pk in seen:
            continue
        seen.add(file_obj.pk)
        url = _url(file_obj)
        if url and not run.may_fetch(file_obj, url):
            results[SKIPPED] += 1
            continue
        if url:
            run.pacer.wait(url)
        result = check(file_obj, client, run=run)
        if url:
            run.record(url, result)
        results[result] += 1


def check_changed(
    body: Any = None, *, limit: int = 200, interval: float = 2.0, client: Any = None, run: Run | None = None
) -> Counter[str]:
    """Von der Quelle geänderte Dokumente neu laden und vergleichen (höchstens ``limit``)."""
    results: Counter[str] = Counter()
    run = run or Run(interval)
    own = client is None
    client = client or fetch_client()
    try:
        _check_each(changed_queryset(body)[:limit].iterator(chunk_size=100), verify, client, run, results)
        run.commit_missing(results)
    finally:
        if own:
            client.close()
    return results


def sample_heads(
    body: Any = None,
    *,
    per_source: int = 50,
    interval: float = 2.0,
    client: Any = None,
    run: Run | None = None,
    now: datetime | None = None,
) -> Counter[str]:
    """
    HEAD-Stichproben je Kommune (höchstens ``per_source`` je Kommune und Lauf). Fällige erneute Prüfungen
    gesperrter Dokumente kommen zuerst. Ohne ``body`` nur gelistete Kommunen: Ausgeblendete (Piloten)
    zeigen nichts öffentlich und bekommen keine zusätzlichen Abrufe.
    """
    from itertools import chain

    from ..models import OParlBody

    results: Counter[str] = Counter()
    if body is not None:
        bodies = [body]
    else:
        bodies = list(OParlBody.objects.filter(source__isnull=False, is_listed=True).order_by("pk"))
    run = run or Run(interval)
    own = client is None
    client = client or fetch_client()
    try:
        for current in bodies:
            if file_cache.source_paused(current) or file_cache.downloads_disabled(current):
                continue
            candidates = chain(recheck_queryset(current, now)[:per_source], sample_queryset(current)[:per_source])
            _check_each(candidates, head_check, client, run, results, limit=per_source)
        run.commit_missing(results)
    finally:
        if own:
            client.close()
    return results


def restore_reappeared() -> int:
    """Nach dem Löschen wieder freigegebene Dokumente (Quelle hebt die Löschung auf): Text neu erkennen."""
    from ..models import OParlFile

    return OParlFile.objects.filter(
        content_purged_at__isnull=False, deleted=False, source_missing_since__isnull=True
    ).update(content_purged_at=None, text_extraction_status="pending")


def _purge(file_obj: Any, now: datetime, results: Counter[str]) -> None:
    """Kopie und Text eines Dokuments löschen; der Datensatz bleibt als Tombstone."""
    from ..models import OParlFile

    if file_obj.local_path:
        try:
            Path(file_obj.local_path).unlink(missing_ok=True)
            results["copies"] += 1
        except OSError:
            logger.warning("Dokument %s: lokale Kopie ließ sich nicht löschen", file_obj.pk)
            return
    OParlFile.objects.filter(pk=file_obj.pk).update(
        text_content=None,
        page_count=None,
        text_extraction_status="skipped",
        text_extraction_error="Von der Quelle entfernt, Kopie und Text gelöscht",
        local_path=None,
        local_size=None,
        local_status="none",
        content_purged_at=now,
    )
    _forget_summary(file_obj)
    results["purged"] += 1


def purge_expired(
    *,
    days: int | None = None,
    now: datetime | None = None,
    body: Any = None,
    client: Any = None,
    interval: float = 2.0,
    confirm_limit: int = 200,
    run: Run | None = None,
) -> Counter[str]:
    """
    Kopie und Text von Dokumenten löschen, die länger als ``days`` Tage gesperrt sind.

    * In der Quelle gelöscht (``deleted``): nach Frist ab ``deleted_at``. Fehlt der Zeitpunkt
      (Altbestand), beginnt die Frist jetzt.
    * Nicht mehr abrufbar (404/410): unmittelbar vorher noch ein GET (gedrosselt, robots.txt). Liefert
      die Quelle das Dokument, wird entsperrt statt gelöscht. Lässt sie sich nicht befragen, warten wir
      bis zu ``FILE_PURGE_CONFIRM_GRACE_DAYS`` Tage, danach wird ohne Rückfrage gelöscht.
    """
    from ..models import OParlFile

    now = now or timezone.now()
    span = timedelta(days=purge_after_days() if days is None else days)
    cutoff = now - span
    unconfirmed_cutoff = cutoff - timedelta(days=purge_confirm_grace_days())
    results: Counter[str] = Counter()
    scope = OParlFile.objects.filter(content_purged_at__isnull=True)
    if body is not None:
        scope = scope.filter(body=body)

    undated = scope.filter(deleted=True, deleted_at__isnull=True).update(deleted_at=now)
    if undated:
        results["frist_beginnt"] = undated
    deleted = scope.filter(deleted=True, deleted_at__lte=cutoff).defer("text_content", "raw_json")
    for file_obj in deleted.iterator(chunk_size=200):
        _purge(file_obj, now, results)

    missing = list(
        scope.filter(deleted=False, source_missing_since__lte=cutoff)
        .select_related("body", "body__source")
        .defer("text_content", "raw_json", "body__raw_json")
        .order_by("source_missing_since", "pk")[:confirm_limit]
    )
    if not missing:
        return results
    run = run or Run(interval)
    own = client is None
    client = client or fetch_client()
    try:
        for file_obj in missing:
            url = _url(file_obj)
            if url and run.may_fetch(file_obj, url):
                run.pacer.wait(url)
                result = verify(file_obj, client, now=now)
                run.record(url, result)
            else:
                result = SKIPPED
            if result == MISSING:
                _purge(file_obj, now, results)
            elif result in (UNCHANGED, CHANGED):
                # Die Quelle liefert das Dokument wieder: Sperre aufgehoben, nichts gelöscht
                results["entsperrt"] += 1
            elif file_obj.source_missing_since and file_obj.source_missing_since <= unconfirmed_cutoff:
                # Auch nach der Nachfrist nicht zu befragen: ohne Rückfrage löschen
                _purge(file_obj, now, results)
                results["ohne_rueckfrage"] += 1
            else:
                results["zurueckgestellt"] += 1
    finally:
        if own:
            client.close()
    return results
