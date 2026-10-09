# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Obergrenze für die Gesamtgröße des Dokument-Caches (Issue #961, docs/FILE_CACHE.md).

Ohne Grenze (``FILE_CACHE_MAX_TOTAL_GB=0``, Standard) wächst die Ablage, bis ``FILE_CACHE_MIN_FREE_GB`` greift.
Mit Grenze verdrängt ``enforce`` die am wenigsten gebrauchten Inhalte, sobald die Belegung die Grenze
überschreitet, bis sie ``FILE_CACHE_EVICT_TARGET_PERCENT`` (Standard 90 %) der Grenze erreicht.

**Rangfolge:** zuletzt über die Vorschau ausgeliefert (``OParlFile.local_accessed_at``, ``mark_used``), sonst das
Datum des Dokuments (Datum, sonst Anlage in der Quelle bzw. bei uns: dieselbe Einteilung wie im
Zugriffsprotokoll, nach dem jüngere Dokumente weit häufiger gelesen werden). Ein Inhalt zählt so jung wie die
jüngste Datei, die auf ihn verweist. Der Zeitpunkt der Zwischenspeicherung zählt bewusst nicht: Der Erstabgleich
legte die neuesten Dokumente zuerst ab, sie wären sonst zuerst verdrängt worden.

**Zwei Arten der Verdrängung:**

* Mit Objektspeicher (``OBJ_ENABLED``, Ablage nach SHA-256) wird nur die lokale Kopie gelöscht, und nur für Inhalte,
  die sicher im Objektspeicher liegen. Die Datenbank bleibt unverändert, die Dokumente bleiben „Lokal vorhanden“;
  die Vorschau holt den Inhalt bei Bedarf aus dem Objektspeicher (``file_store.local_copy``).
* Ohne Objektspeicher geben alle Dateien eines Inhalts ihre Referenz gemeinsam frei und gehen auf ``evicted``
  („Verdrängt“); danach verweist kein Dokument mehr auf den Inhalt, und er wird gelöscht. Die Vorschau holt ein
  verdrängtes Dokument bei Bedarf von der Quelle und legt es wieder ab; ``cache_files`` lädt verdrängte Dokumente
  nicht von selbst nach. Extrahierter Text, Status der Texterkennung und Fingerabdruck (``sha256_hash``) bleiben
  unberührt, es wird also nichts neu erkannt.

**Nie verdrängt** werden Inhalte, deren Text gerade erkannt wird (``pending``/``processing``: die Erkennung liest die
lokale Kopie), Kopien aus der letzten Stunde und – ohne Objektspeicher – Dokumente, die sich nicht neu abrufen
ließen: Quelle in Schonung, Dateiabruf abgeschaltet, synthetische Quelle (Domäne ``.invalid``, etwa die Demo) oder
keine Download-Adresse. Gesperrte Dokumente (Löschabgleich, #787) behandelt die Grenze wie alle anderen; ihre
Sperre, Frist und Löschung bleiben, wie sie sind.

**Belegung:** Inhalte unter ``<OPARL_FILES_ROOT>/sha256/<ab>/`` auf der Platte (ohne Teil-Downloads) plus Kopien im
alten Layout je Kommune (Größe aus der Datenbank). Gelöscht wird nur unterhalb von ``OPARL_FILES_ROOT`` und nie über
symbolische Verweise.

**Ablauf in Stapeln:** Der Durchgang über die Platte summiert nur Größen. Danach liefert die Datenbank die Inhalte in
Rangfolge über einen Cursor (nie alle zugleich im Speicher); verdrängt wird je Stapel in einer kurzen Transaktion,
gerade gesperrte Zeilen (eine Datei wird eben abgelegt) werden übersprungen statt erwartet. Ein Lauf hält eine
Sperre im gemeinsamen Cache, damit Zeitplan und Handlauf nicht doppelt verdrängen.
"""

from __future__ import annotations

import heapq
import logging
import os
import re
import stat
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Max, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.common.einmalig import Sperre

from . import file_cache, file_store, object_storage

logger = logging.getLogger(__name__)

#: Status verdrängter Dokumente (ohne Objektspeicher): die Vorschau holt sie bei Bedarf neu
EVICTED = "evicted"
#: Die Texterkennung liest die lokale Kopie: solche Inhalte bleiben
BUSY_TEXT = ("pending", "processing")
#: So junge Kopien bleiben (gerade abgelegt, womöglich noch in Gebrauch)
FRESH = timedelta(hours=1)
#: Die Nutzung eines Dokuments höchstens so oft vermerken
USE_INTERVAL = timedelta(hours=1)
#: Inhalte je Transaktion
BATCH = 200
#: Gemeinsame Sperre aller Läufe (Zeitplan, Handlauf); verfällt vor dem nächsten stündlichen Termin
LOCK_NAME = "dokumentcache-grenze"
LOCK_TTL = 3000

MODE_REMOTE = "objektspeicher"
MODE_RELEASE = "freigeben"

_KIND_BLOB = 0
_KIND_LEGACY = 1
_HEX2 = re.compile(r"[0-9a-f]{2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: Felder einer Datei für Rangfolge und Schutz
_FILE_FIELDS = (
    "pk",
    "blob_id",
    "body_id",
    "local_path",
    "text_extraction_status",
    "local_cached_at",
    "local_accessed_at",
    "file_date",
    "oparl_created",
    "created_at",
    "download_url",
    "access_url",
)


# =============================================================================
# Einstellungen
# =============================================================================


def limit_bytes() -> int:
    """Obergrenze in Bytes aus ``FILE_CACHE_MAX_TOTAL_GB``; 0 = unbegrenzt (bisheriges Verhalten)."""
    try:
        gb = float(getattr(settings, "FILE_CACHE_MAX_TOTAL_GB", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return round(gb * 1024**3) if gb > 0 else 0


def target_percent() -> int:
    """Zielwert der Verdrängung in Prozent der Grenze (50 bis 100, Standard 90)."""
    try:
        value = int(getattr(settings, "FILE_CACHE_EVICT_TARGET_PERCENT", 90) or 90)
    except (TypeError, ValueError):
        value = 90
    return min(max(value, 50), 100)


def target_bytes(limit: int) -> int:
    return limit * target_percent() // 100


def mode() -> str:
    """Mit Objektspeicher nur lokal verdrängen, sonst Referenzen freigeben."""
    return MODE_REMOTE if object_storage.enabled() and file_store.uses_blobs() else MODE_RELEASE


# =============================================================================
# Nutzung vermerken
# =============================================================================


def mark_used(file_obj: Any, now: datetime | None = None) -> None:
    """
    Auslieferung über die Vorschau vermerken (``local_accessed_at``), höchstens einmal je ``USE_INTERVAL``.

    Nur ein Zeitpunkt je Dokument, keine Angaben zum Abruf. Ein Fehler verhindert die Auslieferung nie.
    """
    from ..models import OParlFile

    now = now or timezone.now()
    last = getattr(file_obj, "local_accessed_at", None)
    if last is not None and now - last < USE_INTERVAL:
        return
    try:
        stale = Q(local_accessed_at__isnull=True) | Q(local_accessed_at__lt=now - USE_INTERVAL)
        OParlFile.objects.filter(stale, pk=file_obj.pk).update(local_accessed_at=now)
        file_obj.local_accessed_at = now
    except Exception:  # Vermerk ist Nebensache: die Auslieferung geht vor
        logger.warning("Nutzung von Dokument %s nicht vermerkt", getattr(file_obj, "pk", "?"), exc_info=True)


# =============================================================================
# Belegung
# =============================================================================


@dataclass(frozen=True)
class _Roots:
    cache: Path
    blobs: Path
    #: Liegt die Ablage nach SHA-256 tatsächlich unterhalb von OPARL_FILES_ROOT? Sonst wird dort nichts gelöscht.
    blobs_safe: bool


def _roots() -> _Roots | None:
    try:
        cache = file_cache.cache_root().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    try:
        blobs = file_store.blob_root().resolve(strict=True)
    except (OSError, RuntimeError):
        blobs = cache / "sha256"
    safe = blobs.is_relative_to(cache) and blobs != cache
    if not safe:
        logger.warning("Ablage nach SHA-256 liegt nicht unterhalb von OPARL_FILES_ROOT: dort wird nichts verdrängt")
    return _Roots(cache=cache, blobs=blobs, blobs_safe=safe)


def _blob_sizes(root: Path) -> Iterator[int]:
    """Größen der Inhalte unter ``sha256/<ab>/`` (reguläre Dateien, ohne Verweise und Teil-Downloads)."""
    if not root.is_dir():
        return
    with os.scandir(root) as prefixes:
        for prefix in prefixes:
            if not _HEX2.fullmatch(prefix.name) or not prefix.is_dir(follow_symlinks=False):
                continue
            with os.scandir(prefix.path) as entries:
                for entry in entries:
                    if not (_SHA256.fullmatch(entry.name) and entry.name.startswith(prefix.name)):
                        continue
                    try:
                        if entry.is_file(follow_symlinks=False):
                            yield entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue


def _legacy_files() -> Any:
    """Kopien im alten Layout je Kommune (ohne Referenz auf einen Inhalt)."""
    from ..models import OParlFile

    return (
        OParlFile.objects.filter(local_status="ok", blob__isnull=True)
        .exclude(local_path__isnull=True)
        .exclude(local_path="")
        # Pfade unter sha256/ gehören Inhalten anderer Dateien
        .exclude(local_path__startswith=str(file_store.blob_root()))
    )


def usage_bytes() -> int:
    """Belegung: Inhalte nach SHA-256 auf der Platte plus Kopien im alten Layout (Größe aus der Datenbank)."""
    disk = sum(_blob_sizes(file_store.blob_root()))
    legacy = _legacy_files().aggregate(s=Sum(Coalesce("local_size", "size")))["s"] or 0
    return int(disk) + int(legacy)


def room_bytes() -> int | None:
    """Platz bis zur Grenze in Bytes (negativ: darüber); ``None`` ohne Grenze."""
    limit = limit_bytes()
    if not limit:
        return None
    return limit - usage_bytes()


# =============================================================================
# Rangfolge und Schutz
# =============================================================================


def _score_expression() -> Greatest:
    document_date = Coalesce("file_date", "oparl_created", "created_at")
    # Beide Teile sind nie leer: Greatest verhält sich so in PostgreSQL und SQLite gleich
    return Greatest(Coalesce("local_accessed_at", document_date), document_date)


def score_of(row: dict[str, Any]) -> datetime:
    """Rang einer Datei in Python (wie ``_score_expression``): letzte Auslieferung, sonst Datum des Dokuments."""
    document_date: datetime = row["file_date"] or row["oparl_created"] or row["created_at"]
    used: datetime | None = row["local_accessed_at"]
    return max(used, document_date) if used is not None else document_date


def _synthetic(url: str | None) -> bool:
    """Synthetische Quelle (Domäne ``.invalid``, etwa die Demo): Die Kopie ist die einzige."""
    return (urlparse(url or "").hostname or "").endswith(".invalid")


def unrefetchable_bodies() -> list[Any]:
    """Kommunen, deren Dokumente sich gerade nicht neu abrufen ließen (Schonung, kein Dateiabruf, synthetisch)."""
    from ..models import OParlBody, OParlSource

    sources = set(file_cache.sources_without_downloads())
    sources.update(
        OParlSource.objects.filter(consecutive_failures__gte=file_cache.backoff_failures()).values_list("pk", flat=True)
    )
    sources.update(pk for pk, url in OParlSource.objects.values_list("pk", "url") if _synthetic(url))
    if not sources:
        return []
    return list(OParlBody.objects.filter(source_id__in=sources).values_list("pk", flat=True))


def _no_url_q() -> Q:
    return (Q(download_url__isnull=True) | Q(download_url="")) & (Q(access_url__isnull=True) | Q(access_url=""))


def _blocked_q(unrefetchable: list[Any]) -> Q:
    blocked = _no_url_q()
    if unrefetchable:
        blocked |= Q(body_id__in=unrefetchable)
    return blocked


def _flag(condition: Q) -> Case:
    return Case(When(condition, then=Value(1)), default=Value(0), output_field=IntegerField())


def _blob_rows(unrefetchable: list[Any]) -> Iterator[dict[str, Any]]:
    """Inhalte in Rangfolge (älteste Nutzung zuerst), je Inhalt über alle Dateien, die auf ihn verweisen."""
    from ..models import OParlFile

    rows = (
        OParlFile.objects.filter(blob__isnull=False)
        .values("blob_id")
        .annotate(
            score=Max(_score_expression()),
            busy=Max(_flag(Q(text_extraction_status__in=BUSY_TEXT))),
            blocked=Max(_flag(_blocked_q(unrefetchable))),
            cached=Max("local_cached_at"),
            remote=Max("blob__remote_at"),
        )
        .order_by("score", "blob_id")
    )
    for row in rows.iterator(chunk_size=1000):
        yield {
            "kind": _KIND_BLOB,
            "key": str(row["blob_id"]),
            "score": row["score"],
            "busy": bool(row["busy"]),
            "blocked": bool(row["blocked"]),
            "cached": row["cached"],
            "remote": row["remote"],
            "path": None,
        }


def _legacy_rows(unrefetchable: list[Any]) -> Iterator[dict[str, Any]]:
    """Kopien im alten Layout in Rangfolge."""
    rows = (
        _legacy_files()
        .annotate(
            score=_score_expression(),
            busy=_flag(Q(text_extraction_status__in=BUSY_TEXT)),
            blocked=_flag(_blocked_q(unrefetchable)),
        )
        .values("pk", "local_path", "score", "busy", "blocked", "local_cached_at")
        .order_by("score", "pk")
    )
    for row in rows.iterator(chunk_size=1000):
        yield {
            "kind": _KIND_LEGACY,
            "key": str(row["pk"]),
            "score": row["score"],
            "busy": bool(row["busy"]),
            "blocked": bool(row["blocked"]),
            "cached": row["local_cached_at"],
            "remote": None,
            "path": row["local_path"],
        }


def _ranking(unrefetchable: list[Any]) -> Iterable[dict[str, Any]]:
    return heapq.merge(
        _blob_rows(unrefetchable),
        _legacy_rows(unrefetchable),
        key=lambda row: (row["score"], row["kind"], row["key"]),
    )


def _protection(row: dict[str, Any], run_mode: str, now: datetime) -> str | None:
    """Grund, aus dem ein Inhalt bleibt, sonst ``None``."""
    if row["busy"]:
        return "texterkennung"
    cached = row["cached"]
    if cached is not None and cached > now - FRESH:
        return "frisch"
    if run_mode == MODE_REMOTE and row["kind"] == _KIND_BLOB:
        return None if row["remote"] is not None else "nicht_hochgeladen"
    if row["blocked"]:
        return "nicht_abrufbar"
    return None


# =============================================================================
# Pfade: nur unterhalb von OPARL_FILES_ROOT, nie über Verweise
# =============================================================================


def _blob_file(roots: _Roots, sha256: str) -> tuple[Path, int] | None:
    if not roots.blobs_safe or not _SHA256.fullmatch(sha256):
        return None
    prefix = roots.blobs / sha256[:2]
    path = prefix / sha256
    try:
        if not stat.S_ISDIR(os.lstat(prefix).st_mode):
            return None
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return path, info.st_size


def _legacy_file(roots: _Roots, local_path: str | None) -> tuple[Path, int] | None:
    if not local_path:
        return None
    path = Path(local_path)
    if not path.is_absolute():
        return None
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return None
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_relative_to(roots.cache) or real.is_relative_to(roots.blobs):
        return None
    return real, info.st_size


# =============================================================================
# Ein Lauf
# =============================================================================


@dataclass
class Result:
    """Ergebnis eines Laufs (auch ``dry_run``)."""

    mode: str
    limit: int = 0
    target: int = 0
    before: int = 0
    after: int = 0
    #: verdrängte Inhalte (bzw. Kopien im alten Layout)
    units: int = 0
    #: betroffene Dokumente
    files: int = 0
    freed: int = 0
    #: je Kommune: [Dokumente, Bytes]; ein Inhalt zählt mit seinen Bytes bei der ersten Kommune (nach Namen)
    per_body: dict[str, list[int]] = field(default_factory=dict)
    #: auf dem Weg übersprungene Inhalte je Grund (Bytes)
    protected: Counter[str] = field(default_factory=Counter)
    #: Inhalte, die sich beim Verdrängen geändert hatten oder gesperrt waren (Anzahl)
    skipped: Counter[str] = field(default_factory=Counter)
    dry_run: bool = False
    disabled: bool = False
    #: ein anderer Lauf hält die Sperre
    locked: bool = False

    @property
    def reached(self) -> bool:
        return self.after <= self.target


@dataclass
class _Run:
    result: Result
    now: datetime
    roots: _Roots
    unrefetchable: set[Any]
    lock: Sperre | None
    names: dict[Any, str] = field(default_factory=dict)


def enforce(
    *, max_bytes: int | None = None, dry_run: bool = False, now: datetime | None = None, batch: int = BATCH
) -> Result:
    """
    Belegung auf ``FILE_CACHE_EVICT_TARGET_PERCENT`` der Grenze bringen, sobald sie die Grenze überschreitet.

    ``max_bytes`` ersetzt ``FILE_CACHE_MAX_TOTAL_GB`` (Handlauf, ``prune_file_cache --max-gb``). Mit ``dry_run`` wird
    nur berechnet, was ginge.
    """
    limit = limit_bytes() if max_bytes is None else max(int(max_bytes), 0)
    result = Result(mode=mode(), limit=limit, target=target_bytes(limit), dry_run=dry_run)
    if not limit:
        result.disabled = True
        return result
    lock = None if dry_run else Sperre(LOCK_NAME, LOCK_TTL)
    if lock is not None and not lock.erwerben():
        result.locked = True
        return result
    try:
        _enforce(result, now or timezone.now(), max(1, batch), lock)
    finally:
        if lock is not None:
            lock.freigeben()
    return result


def _enforce(result: Result, now: datetime, batch: int, lock: Sperre | None) -> None:
    result.before = result.after = usage_bytes()
    if result.before <= result.limit:
        return
    roots = _roots()
    if roots is None:
        result.skipped["unsicher"] += 1
        return
    unrefetchable = unrefetchable_bodies()
    run = _Run(result=result, now=now, roots=roots, unrefetchable=set(unrefetchable), lock=lock)
    to_free = result.before - result.target
    pending: list[tuple[dict[str, Any], Path, int]] = []
    planned = 0
    for row in _ranking(unrefetchable):
        if result.freed + planned >= to_free:
            break
        found = _blob_file(roots, row["key"]) if row["kind"] == _KIND_BLOB else _legacy_file(roots, row["path"])
        if found is None:
            continue  # nicht (mehr) lokal oder nicht sicher löschbar
        path, size = found
        reason = _protection(row, result.mode, now)
        if reason:
            result.protected[reason] += size
            continue
        pending.append((row, path, size))
        planned += size
        if len(pending) >= batch:
            if not _apply(run, pending):
                return
            pending, planned = [], 0
    if pending:
        _apply(run, pending)
    result.after = result.before - result.freed


def _apply(run: _Run, items: list[tuple[dict[str, Any], Path, int]]) -> bool:
    """Einen Stapel verdrängen; ``False``, wenn der Lauf abbrechen muss (Löschen scheitert)."""
    blobs = [item for item in items if item[0]["kind"] == _KIND_BLOB]
    legacy = [item for item in items if item[0]["kind"] == _KIND_LEGACY]
    ok = True
    if run.result.dry_run:
        _count_planned(run, blobs, legacy)
    else:
        if blobs:
            ok = _evict_local(run, blobs) if run.result.mode == MODE_REMOTE else _release_blobs(run, blobs)
        if legacy and ok:
            ok = _release_legacy(run, legacy)
    if run.lock is not None:
        run.lock.verlaengern()
    run.result.after = run.result.before - run.result.freed
    return ok


def _body_name(run: _Run, body_id: Any) -> str:
    if not run.names:
        from ..models import OParlBody

        run.names = dict(OParlBody.objects.values_list("pk", "name"))
    return run.names.get(body_id) or "ohne Kommune"


def _count(run: _Run, bodies: list[Any], size: int) -> None:
    """Ein verdrängter Inhalt: Dokumente je Kommune, Bytes bei der ersten Kommune (nach Namen)."""
    names = sorted(_body_name(run, body_id) for body_id in bodies) or ["ohne Kommune"]
    for index, name in enumerate(names):
        entry = run.result.per_body.setdefault(name, [0, 0])
        entry[0] += 1 if bodies else 0
        if index == 0:
            entry[1] += size
    run.result.units += 1
    run.result.files += len(bodies)
    run.result.freed += size


def _blob_bodies(keys: list[str]) -> dict[str, list[Any]]:
    """Kommunen der Dateien, die auf diese Inhalte verweisen (je Datei ein Eintrag)."""
    from ..models import OParlFile

    bodies: dict[str, list[Any]] = {key: [] for key in keys}
    for blob_id, body_id in OParlFile.objects.filter(blob_id__in=keys).values_list("blob_id", "body_id"):
        bodies.setdefault(str(blob_id), []).append(body_id)
    return bodies


def _count_planned(
    run: _Run, blobs: list[tuple[dict[str, Any], Path, int]], legacy: list[tuple[dict[str, Any], Path, int]]
) -> None:
    """Probelauf: zählen, was verdrängt würde."""
    from ..models import OParlFile

    bodies = _blob_bodies([row["key"] for row, _, _ in blobs])
    for row, _, size in blobs:
        _count(run, bodies[row["key"]], size)
    legacy_bodies = {
        str(pk): body_id
        for pk, body_id in OParlFile.objects.filter(pk__in=[row["key"] for row, _, _ in legacy]).values_list(
            "pk", "body_id"
        )
    }
    for row, _, size in legacy:
        _count(run, [legacy_bodies[row["key"]]] if row["key"] in legacy_bodies else [], size)


def _evict_local(run: _Run, items: list[tuple[dict[str, Any], Path, int]]) -> bool:
    """Mit Objektspeicher: nur die lokale Kopie löschen, die Dokumente behalten ihre Referenz."""
    bodies = _blob_bodies([row["key"] for row, _, _ in items])
    for row, path, size in items:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("Lokale Kopie %s nicht gelöscht", row["key"][:12], exc_info=True)
            run.result.skipped["fehler"] += 1
            return False
        _count(run, bodies[row["key"]], size)
    return True


class _UnlinkError(Exception):
    """Eine Datei ließ sich nicht löschen: der Stapel wird zurückgerollt."""


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise _UnlinkError(str(path)) from exc


def _skip(run: _Run, reason: str, count: int = 1) -> None:
    if count > 0:
        run.result.skipped[reason] += count


def _still_evictable(run: _Run, rows: list[dict[str, Any]], score: datetime) -> str | None:
    """Prüfung unter Sperre: Hat sich seit der Planung etwas geändert? Grund, sonst ``None``."""
    for row in rows:
        if row["text_extraction_status"] in BUSY_TEXT:
            return "texterkennung"
        cached = row["local_cached_at"]
        if cached is not None and cached > run.now - FRESH:
            return "frisch"
        if row["body_id"] in run.unrefetchable or not (row["download_url"] or row["access_url"]):
            return "nicht_abrufbar"
        if score_of(row) > score:
            return "genutzt"
    return None


def _release_blobs(run: _Run, items: list[tuple[dict[str, Any], Path, int]]) -> bool:
    """Ohne Objektspeicher: Referenzen aller Dateien eines Inhalts freigeben (``evicted``), dann den Inhalt löschen."""
    from ..models import OParlFile, OParlFileBlob

    planned = {row["key"]: (row, path, size) for row, path, size in items}
    try:
        with transaction.atomic():
            locked = dict(
                OParlFileBlob.objects.select_for_update(skip_locked=True)
                .filter(pk__in=list(planned))
                .values_list("pk", "remote_at")
            )
            files: dict[str, list[dict[str, Any]]] = {key: [] for key in locked}
            for row in (
                OParlFile.objects.select_for_update(skip_locked=True)
                .filter(blob_id__in=list(locked))
                .values(*_FILE_FIELDS)
            ):
                files[str(row["blob_id"])].append(row)
            actual = dict(
                OParlFile.objects.filter(blob_id__in=list(locked))
                .values("blob_id")
                .annotate(n=Count("pk"))
                .values_list("blob_id", "n")
            )
            _skip(run, "gesperrt", len(planned) - len(locked))
            evict: list[str] = []
            for key in locked:
                rows = files[key]
                if not rows:
                    # Kein Verweis mehr: das Aufräumen verwaister Inhalte löscht ihn
                    _skip(run, "ohne_verweis")
                    continue
                if len(rows) != actual.get(key, 0):
                    # Eine Datei des Inhalts wird gerade geändert (gesperrte Zeile): beim nächsten Lauf
                    _skip(run, "gesperrt")
                    continue
                reason = _still_evictable(run, rows, planned[key][0]["score"])
                if reason:
                    _skip(run, reason)
                    continue
                evict.append(key)
            if not evict:
                return True
            OParlFile.objects.filter(pk__in=[row["pk"] for key in evict for row in files[key]]).update(
                blob=None, local_path=None, local_size=None, local_status=EVICTED, local_error=""
            )
            # Liegt der Inhalt (aus früherem Betrieb) im Objektspeicher, löscht ihn dort das Aufräumen
            remote = [key for key in evict if locked[key] is not None]
            if remote:
                OParlFileBlob.objects.filter(pk__in=remote).update(ref_count=0, orphaned_at=run.now)
            gone = [key for key in evict if locked[key] is None]
            if gone:
                OParlFileBlob.objects.filter(pk__in=gone).delete()
            # Löschen, solange die Sperre gilt: Wer den Inhalt gleich wieder ablegt, findet die Datei nicht mehr vor
            for key in evict:
                _unlink(planned[key][1])
    except _UnlinkError:
        logger.warning("Verdrängen abgebrochen: eine Kopie ließ sich nicht löschen", exc_info=True)
        run.result.skipped["fehler"] += 1
        return False
    for key in evict:
        _count(run, [row["body_id"] for row in files[key]], planned[key][2])
    return True


def _release_legacy(run: _Run, items: list[tuple[dict[str, Any], Path, int]]) -> bool:
    """Kopien im alten Layout: Datei löschen, Dokument auf ``evicted``."""
    from ..models import OParlFile

    planned = {row["key"]: (row, path, size) for row, path, size in items}
    done: list[dict[str, Any]] = []
    try:
        with transaction.atomic():
            rows = list(
                OParlFile.objects.select_for_update(skip_locked=True)
                .filter(pk__in=list(planned), local_status="ok", blob__isnull=True)
                .values(*_FILE_FIELDS)
            )
            _skip(run, "gesperrt", len(planned) - len(rows))
            for row in rows:
                key = str(row["pk"])
                reason = _still_evictable(run, [row], planned[key][0]["score"])
                if reason:
                    _skip(run, reason)
                    continue
                found = _legacy_file(run.roots, row["local_path"])
                if found is None or found[0] != planned[key][1]:
                    _skip(run, "unsicher")
                    continue
                done.append(row)
            if not done:
                return True
            OParlFile.objects.filter(pk__in=[row["pk"] for row in done]).update(
                local_path=None, local_size=None, local_status=EVICTED, local_error=""
            )
            for row in done:
                _unlink(planned[str(row["pk"])][1])
    except _UnlinkError:
        logger.warning("Verdrängen abgebrochen: eine Kopie ließ sich nicht löschen", exc_info=True)
        run.result.skipped["fehler"] += 1
        return False
    for row in done:
        _count(run, [row["body_id"]], planned[str(row["pk"])][2])
    return True
