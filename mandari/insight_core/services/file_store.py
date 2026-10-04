# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumentablage nach SHA-256 mit Referenzzählung (Issue #788).

Layout unter ``OPARL_FILES_ROOT``::

    sha256/<ab>/<sha256>      Inhalt, ohne Dateiendung (der Typ kommt nie aus dem Namen)
    sha256/tmp/<zufall>.part  Downloads, die gerade entstehen (gleiches Dateisystem: atomares Umbenennen)

Gleiche Dateien liegen nur einmal in der Ablage. Jede Datei (``OParlFile.blob``) ist eine Referenz auf
ihren Inhalt (``OParlFileBlob``). Fällt die letzte Referenz weg, wird der Inhalt verwaist markiert und
vom Aufräumen gelöscht, lokal und im Objektspeicher. Die Originale bleiben unverändert.

Ablegen, Ersetzen und Freigeben laufen in einer Transaktion mit gesperrter Zeile des Inhalts. Die Datei
wird verschoben, solange die Sperre gilt. So kann das Aufräumen nie einen Inhalt löschen, den ein
anderer Prozess (Anwendung oder Ingestor, der dasselbe Vorgehen per SQL nutzt) gerade wieder belegt.

``FILE_STORE_LAYOUT=kommune`` behält das bisherige Layout je Kommune (``<kommune>/<jahr>/<id>.<endung>``),
etwa wenn je Kommune ein eigener Speicher eingehängt ist. Dann gibt es weder Deduplizierung noch
Objektspeicher.

Mit eingeschaltetem Objektspeicher (``services/object_storage.py``) ist die lokale Ablage ein
Zwischenspeicher: Inhalte werden hochgeladen, lokal nach dem letzten Zugriff verdrängt
(``OBJ_CACHE_MAX_GB``) und bei Bedarf wieder geholt. Antwortet der Objektspeicher nicht, holt die
Dateivorschau das Dokument wie bisher von der Quelle. Den letzten Zugriff trägt die Zugriffszeit
(``atime``) der Datei; die Änderungszeit bleibt unberührt, denn aus ihr bildet der Webserver ``ETag``
und ``Last-Modified``.

Abgelegte Inhalte sind für alle lesbar (``0644``): Der Webserver liefert sie aus, und Anwendung und
Ingestor legen sie mit verschiedenen Prozessen ab.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import IO, Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, F, QuerySet, Sum
from django.utils import timezone

from . import file_cache, object_storage

logger = logging.getLogger(__name__)

LAYOUT_SHA256 = "sha256"
LAYOUT_KOMMUNE = "kommune"
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: Rechte abgelegter Inhalte: Webserver, Anwendung und Ingestor lesen sie (temporäre Dateien entstehen mit 0600)
FILE_MODE = 0o644
#: Den letzten Zugriff höchstens so oft vermerken (Nanosekunden)
_TOUCH_INTERVAL_NS = 3600 * 10**9


class TooLargeError(Exception):
    """Die Datei überschreitet die zulässige Größe."""


def layout() -> str:
    value = str(getattr(settings, "FILE_STORE_LAYOUT", LAYOUT_SHA256) or LAYOUT_SHA256).strip().lower()
    return LAYOUT_KOMMUNE if value == LAYOUT_KOMMUNE else LAYOUT_SHA256


def uses_blobs() -> bool:
    """Ablage nach SHA-256 (Standard)?"""
    return layout() == LAYOUT_SHA256


def blob_root() -> Path:
    return file_cache.cache_root() / "sha256"


def in_blob_root(path: str | Path | None) -> bool:
    """Liegt ``path`` in der Ablage nach SHA-256 (geteilte Inhalte, nie direkt löschen)?"""
    if not path:
        return False
    return Path(path).is_relative_to(blob_root())


def blob_path(sha256: str) -> Path:
    if not _SHA256.fullmatch(sha256 or ""):
        raise ValueError("Kein SHA-256")
    return blob_root() / sha256[:2] / sha256


def tmp_dir() -> Path:
    path = blob_root() / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Spool:
    """Temporäre Datei in der Ablage, die beim Schreiben hasht und die Größe begrenzt."""

    def __init__(self, max_bytes: int | None = None) -> None:
        self.max_bytes = max_bytes
        handle, name = tempfile.mkstemp(suffix=".part", dir=tmp_dir())
        self.path = Path(name)
        self._file: IO[bytes] | None = os.fdopen(handle, "wb")
        self._digest = hashlib.sha256()
        self.size = 0
        self.head = b""

    def write(self, chunk: bytes) -> None:
        if self._file is None:
            raise ValueError("Spool ist geschlossen")
        self.size += len(chunk)
        if self.max_bytes is not None and self.size > self.max_bytes:
            raise TooLargeError
        self._digest.update(chunk)
        self._file.write(chunk)
        if len(self.head) < 512:
            self.head += chunk[: 512 - len(self.head)]

    def copy_from(self, source: IO[bytes]) -> Spool:
        source.seek(0)
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            self.write(chunk)
        return self.close()

    def close(self) -> Spool:
        if self._file is not None:
            self._file.close()
            self._file = None
        return self

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def discard(self) -> None:
        self.close()
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> Spool:
        return self

    def __exit__(self, *exc: object) -> None:
        # Nicht übernommene Downloads (Fehler, zu groß) räumen sich selbst weg
        self.discard()


# =============================================================================
# Ablegen und Freigeben
# =============================================================================


def _lock_blob(sha256: str, size: int) -> Any:
    """Zeile des Inhalts sperren, sonst anlegen (in der laufenden Transaktion)."""
    from ..models import OParlFileBlob

    blob = OParlFileBlob.objects.select_for_update().filter(pk=sha256).first()
    if blob is not None:
        return blob
    try:
        with transaction.atomic():
            return OParlFileBlob.objects.create(sha256=sha256, size=size, ref_count=0)
    except IntegrityError:
        # Ein paralleler Prozess hat ihn eben angelegt
        return OParlFileBlob.objects.select_for_update().get(pk=sha256)


def _place(source: Path, target: Path) -> None:
    """``source`` als ``target`` ablegen (verschieben, über Dateisystemgrenzen kopieren), lesbar für alle."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(source, FILE_MODE)
    try:
        os.replace(source, target)
    except OSError:
        partial = target.with_name(target.name + ".part")
        shutil.copyfile(source, partial)
        os.replace(partial, target)
        source.unlink(missing_ok=True)


def _release_blob(sha256: str, count: int = 1) -> None:
    from ..models import OParlFileBlob

    OParlFileBlob.objects.filter(pk=sha256).update(ref_count=F("ref_count") - count)
    OParlFileBlob.objects.filter(pk=sha256, ref_count__lte=0, orphaned_at__isnull=True).update(
        orphaned_at=timezone.now()
    )


def _unlink_after_commit(path: str | None) -> None:
    if path:
        transaction.on_commit(lambda: Path(path).unlink(missing_ok=True))


def attach(file_obj: Any, source: Path, sha256: str, size: int, *, content_type: str | None = None) -> Path:
    """
    Inhalt aus ``source`` unter seinem SHA-256 ablegen und ``file_obj`` darauf verweisen lassen.

    Liegt der Inhalt schon in der Ablage, wird ``source`` verworfen. Eine bisherige Referenz der Datei
    wird freigegeben, eine Kopie im alten Layout nach dem Commit gelöscht.
    """
    from ..models import OParlFile, OParlFileBlob

    target = blob_path(sha256)
    with transaction.atomic():
        _lock_blob(sha256, size)
        if target.is_file():
            # Inhalt liegt schon in der Ablage: die neue Fassung erst nach dem Commit verwerfen
            _unlink_after_commit(str(source))
        else:
            _place(source, target)
        current = OParlFile.objects.select_for_update().filter(pk=file_obj.pk).values("blob_id", "local_path").first()
        old_blob = current["blob_id"] if current else None
        old_path = current["local_path"] if current else None
        if old_blob != sha256:
            OParlFileBlob.objects.filter(pk=sha256).update(ref_count=F("ref_count") + 1, orphaned_at=None)
            if old_blob:
                _release_blob(old_blob)
            elif old_path and Path(old_path) != target and not in_blob_root(old_path):
                # Kopie im bisherigen Layout; ein Pfad unter sha256/ ohne Referenz gehört anderen Dateien
                _unlink_after_commit(old_path)
        file_obj.blob_id = sha256
        file_obj.local_path = str(target)
        file_obj.local_size = size
        file_obj.size = size
        file_obj.sha256_hash = sha256
        file_obj.local_status = "ok"
        file_obj.local_error = ""
        file_obj.local_cached_at = timezone.now()
        fields = [
            "blob",
            "local_path",
            "local_size",
            "size",
            "sha256_hash",
            "local_status",
            "local_error",
            "local_cached_at",
        ]
        if not file_obj.mime_type and content_type:
            file_obj.mime_type = content_type.split(";")[0].strip()[:100]
            fields.append("mime_type")
        file_obj.save(update_fields=fields)
    return target


def store_spool(file_obj: Any, spool: Spool, *, content_type: str | None = None) -> Path:
    spool.close()
    return attach(file_obj, spool.path, spool.sha256, spool.size, content_type=content_type)


def release(file_obj: Any) -> None:
    """Die Datei verweist nicht mehr auf ihren Inhalt (Kopie gelöscht bzw. nicht mehr gebraucht)."""
    from ..models import OParlFile

    release_queryset(OParlFile.objects.filter(pk=file_obj.pk))
    file_obj.blob_id = None
    file_obj.local_path = None
    file_obj.local_size = None
    file_obj.local_status = "none"


def release_queryset(files: QuerySet[Any]) -> int:
    """Referenzen vieler Dateien freigeben (Löschen, Ausblenden); Kopien im alten Layout löschen."""
    legacy: list[str] = []
    with transaction.atomic():
        counts = list(files.filter(blob__isnull=False).values("blob_id").annotate(n=Count("pk")))
        legacy = list(
            files.filter(blob__isnull=True)
            .exclude(local_path__isnull=True)
            .exclude(local_path="")
            # Pfade unter sha256/ gehören anderen Dateien (fehlende Referenz): nie löschen
            .exclude(local_path__startswith=str(blob_root()))
            .values_list("local_path", flat=True)
        )
        changed = files.exclude(local_status="none", blob__isnull=True, local_path__isnull=True).update(
            blob=None, local_path=None, local_size=None, local_status="none"
        )
        for row in counts:
            _release_blob(row["blob_id"], row["n"])
    for path in legacy:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logger.warning("Kopie im alten Layout ließ sich nicht löschen")
    return changed


# =============================================================================
# Lesen (mit Objektspeicher als Rückhalt)
# =============================================================================


def _touch(path: Path) -> None:
    """
    Letzten Zugriff vermerken (Verdrängung im Zwischenspeicher nach Zugriffszeit), höchstens einmal je Stunde.

    Nur die Zugriffszeit: Aus der Änderungszeit bildet der Webserver ``ETag`` und ``Last-Modified``; ändert
    sie sich, greifen bedingte Anfragen nicht mehr, und Range-Anfragen mit ``If-Range`` bekommen die ganze Datei.
    """
    if not object_storage.enabled():
        return
    with contextlib.suppress(OSError):
        stat = path.stat()
        now = time.time_ns()
        if now - stat.st_atime_ns >= _TOUCH_INTERVAL_NS:
            os.utime(path, ns=(now, stat.st_mtime_ns))


def local_copy(file_obj: Any) -> Path | None:
    """
    Pfad der lokalen Kopie oder ``None``.

    Mit Objektspeicher: Fehlt der Inhalt lokal, liegt aber im Objektspeicher, wird er gestreamt in die
    Ablage geholt (Zeitgrenze, Hashprüfung). Scheitert das, ``None`` – die Vorschau holt dann von der Quelle.
    """
    from ..models import OParlFileBlob

    path = file_cache.local_file(file_obj)
    if path is not None:
        _touch(path)
        return path
    sha256 = getattr(file_obj, "blob_id", None)
    if not sha256 or not object_storage.enabled():
        return None
    if not OParlFileBlob.objects.filter(pk=sha256, remote_at__isnull=False).exists():
        return None
    return fetch_remote(sha256)


def fetch_remote(sha256: str) -> Path | None:
    """Inhalt aus dem Objektspeicher in die lokale Ablage holen (ohne Datenbankverbindung festzuhalten)."""
    from apps.common.db_connections import release_idle_thread_connections

    target = blob_path(sha256)
    release_idle_thread_connections()
    # Gesamtdauer begrenzt wie beim Abruf von der Quelle: ein langsamer Objektspeicher bindet keinen Thread
    deadline = time.monotonic() + float(getattr(settings, "OBJ_FETCH_TOTAL_SECONDS", 60))
    try:
        with Spool(max_bytes=file_cache.max_bytes()) as spool:
            object_storage.download(sha256, spool, deadline=deadline)
            spool.close()
            if spool.sha256 != sha256:
                logger.error("Inhalt %s aus dem Objektspeicher hat einen anderen Hash", sha256[:12])
                return None
            if not target.is_file():
                _place(spool.path, target)
        return target
    except Exception:  # Objektspeicher gestört: Rückfall auf die Quelle
        logger.warning("Inhalt %s nicht aus dem Objektspeicher ladbar", sha256[:12], exc_info=True)
        return None


# =============================================================================
# Pflege: Umstellen, Hochladen, Aufräumen, Referenzen
# =============================================================================


def migrate_legacy(*, limit: int = 1000) -> Counter[str]:
    """Kopien im alten Layout je Kommune in die Ablage nach SHA-256 verschieben (wiederaufnehmbar)."""
    from ..models import OParlFile

    results: Counter[str] = Counter()
    pending = (
        OParlFile.objects.filter(local_status="ok", blob__isnull=True)
        .exclude(local_path__isnull=True)
        .exclude(local_path="")
        .select_related("body")
        .defer("text_content", "raw_json", "body__raw_json")
        .order_by("pk")
    )
    for file_obj in pending[:limit].iterator(chunk_size=200):
        path = Path(file_obj.local_path or "")
        if not path.is_file():
            OParlFile.objects.filter(pk=file_obj.pk).update(local_status="none", local_path=None, local_size=None)
            results["missing"] += 1
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        size = path.stat().st_size
        # Die Kopie selbst wird verschoben (kein zweiter Platzbedarf); liegt der Inhalt schon in der
        # Ablage, löscht attach() die alte Kopie nach dem Commit
        attach(file_obj, path, digest.hexdigest(), size)
        results["moved"] += 1
    return results


def upload_pending(*, limit: int = 500) -> Counter[str]:
    """Inhalte ohne Kopie im Objektspeicher hochladen."""
    from ..models import OParlFileBlob

    results: Counter[str] = Counter()
    if not object_storage.enabled():
        return results
    for sha256 in OParlFileBlob.objects.filter(remote_at__isnull=True, ref_count__gt=0).values_list("pk", flat=True)[
        :limit
    ]:
        path = blob_path(sha256)
        if not path.is_file():
            results["missing"] += 1
            continue
        try:
            object_storage.upload(sha256, path)
        except Exception:
            logger.warning("Inhalt %s nicht hochgeladen", sha256[:12], exc_info=True)
            results["error"] += 1
            continue
        OParlFileBlob.objects.filter(pk=sha256).update(remote_at=timezone.now())
        results["uploaded"] += 1
    return results


def cleanup_orphans(*, min_age: timedelta = timedelta(minutes=10), limit: int = 5000) -> Counter[str]:
    """Inhalte ohne Referenz löschen (lokal und im Objektspeicher)."""
    from ..models import OParlFile, OParlFileBlob

    results: Counter[str] = Counter()
    cutoff = timezone.now() - min_age
    candidates = OParlFileBlob.objects.filter(ref_count__lte=0, orphaned_at__lte=cutoff).values_list("pk", flat=True)
    for sha256 in list(candidates[:limit]):
        with transaction.atomic():
            blob = (
                OParlFileBlob.objects.select_for_update()
                .filter(pk=sha256, ref_count__lte=0, orphaned_at__isnull=False)
                .first()
            )
            if blob is None:
                continue
            actual = OParlFile.objects.filter(blob_id=sha256).count()
            if actual:
                # Zählung war falsch: berichtigen statt löschen
                OParlFileBlob.objects.filter(pk=sha256).update(ref_count=actual, orphaned_at=None)
                results["repaired"] += 1
                continue
            if blob.remote_at is not None:
                if not object_storage.enabled():
                    results["remote_kept"] += 1
                    continue
                try:
                    object_storage.delete(sha256)
                except Exception:
                    logger.warning("Inhalt %s im Objektspeicher nicht gelöscht", sha256[:12], exc_info=True)
                    results["error"] += 1
                    continue
            blob_path(sha256).unlink(missing_ok=True)
            blob.delete()
            results["deleted"] += 1
    return results


def evict_local(*, max_bytes: int | None = None) -> Counter[str]:
    """
    Lokalen Zwischenspeicher auf ``OBJ_CACHE_MAX_GB`` begrenzen: am längsten nicht gelesene Inhalte zuerst
    (Zugriffszeit, siehe ``_touch``).

    Nur Inhalte, die sicher im Objektspeicher liegen, werden lokal gelöscht.
    """
    from ..models import OParlFileBlob

    results: Counter[str] = Counter()
    if not object_storage.enabled():
        return results
    limit = max_bytes if max_bytes is not None else int(getattr(settings, "OBJ_CACHE_MAX_GB", 60)) * 1024**3
    entries: list[tuple[float, int, str, Path]] = []
    root = blob_root()
    if not root.is_dir():
        return results
    for prefix in os.scandir(root):
        if not prefix.is_dir() or not re.fullmatch(r"[0-9a-f]{2}", prefix.name):
            continue
        for entry in os.scandir(prefix.path):
            if entry.is_file() and _SHA256.fullmatch(entry.name):
                stat = entry.stat()
                entries.append((stat.st_atime, stat.st_size, entry.name, Path(entry.path)))
    total = sum(size for _, size, _, _ in entries)
    results["bytes_before"] = total
    if total <= limit:
        return results
    entries.sort()
    for start in range(0, len(entries), 500):
        batch = entries[start : start + 500]
        remote = set(
            OParlFileBlob.objects.filter(pk__in=[sha for _, _, sha, _ in batch], remote_at__isnull=False).values_list(
                "pk", flat=True
            )
        )
        for _, size, sha256, path in batch:
            if total <= limit:
                results["bytes_after"] = total
                return results
            if sha256 not in remote:
                continue
            path.unlink(missing_ok=True)
            total -= size
            results["evicted"] += 1
    results["bytes_after"] = total
    return results


def repair_refcounts() -> Counter[str]:
    """Referenzzähler aus den tatsächlichen Verweisen neu berechnen (nach Abstürzen, altem Code)."""
    from ..models import OParlFileBlob

    results: Counter[str] = Counter()
    for sha256, stored, actual in (
        OParlFileBlob.objects.annotate(actual=Count("files")).values_list("pk", "ref_count", "actual").iterator()
    ):
        if stored == actual:
            continue
        OParlFileBlob.objects.filter(pk=sha256).update(
            ref_count=actual, orphaned_at=timezone.now() if actual == 0 else None
        )
        results["fixed"] += 1
    return results


def cleanup_tmp(*, older_than: timedelta = timedelta(hours=6)) -> int:
    """Liegengebliebene Teil-Downloads (abgebrochene Prozesse) entfernen."""
    root = blob_root() / "tmp"
    if not root.is_dir():
        return 0
    cutoff = (timezone.now() - older_than).timestamp()
    removed = 0
    for entry in os.scandir(root):
        if entry.is_file() and entry.name.endswith(".part") and entry.stat().st_mtime < cutoff:
            Path(entry.path).unlink(missing_ok=True)
            removed += 1
    return removed


def stats() -> dict[str, Any]:
    from ..models import OParlFile, OParlFileBlob

    blobs = OParlFileBlob.objects.aggregate(n=Count("pk"), bytes=Sum("size"))
    referenced = OParlFile.objects.filter(blob__isnull=False).aggregate(n=Count("pk"), bytes=Sum("local_size"))
    legacy = OParlFile.objects.filter(local_status="ok", blob__isnull=True).count()
    blob_bytes = blobs["bytes"] or 0
    file_bytes = referenced["bytes"] or 0
    return {
        "layout": layout(),
        "blobs": blobs["n"] or 0,
        "blob_bytes": blob_bytes,
        "files": referenced["n"] or 0,
        "file_bytes": file_bytes,
        "saved_bytes": max(file_bytes - blob_bytes, 0),
        "orphaned": OParlFileBlob.objects.filter(ref_count__lte=0).count(),
        "remote": OParlFileBlob.objects.filter(remote_at__isnull=False).count(),
        "legacy_files": legacy,
        "object_storage": object_storage.enabled(),
    }
