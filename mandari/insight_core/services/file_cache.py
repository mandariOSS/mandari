# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lokaler Dokument-Cache für OParl-Dateien (PDFs).

Warum? Ratsinformationssysteme sind regelmäßig nicht erreichbar (Köln fällt in
Sitzungswochen aus, Bonn steht hinter einem Bot-Schutz). Ohne lokale Kopie
zeigt Insight dann „Vorschau nicht verfügbar“ (Issue #87) und der Proxy hält
Worker minutenlang fest (Issue #86). Mit Cache kommt die Datei von der Platte.

Speicherlayout — je Kommune ein Verzeichnis, damit sich pro Stadt eine eigene
Storage Box mounten lässt:

    <OPARL_FILES_ROOT>/<kommune>/<jahr>/<datei-id>.pdf

Ein Festplatten-Schutz (FILE_CACHE_MIN_FREE_GB) verhindert, dass der Cache das
Systemlaufwerk vollschreibt.

Nur gelistete Kommunen werden zwischengespeichert. Ausgeblendete Quellen (Piloten, Tests)
luden sonst ihr ganzes Archiv nach – im September 2026 rund 46 GB, knapp die Hälfte des
Caches, für Kommunen, die niemand im Portal sieht. Wird eine Kommune gelistet, füllt sich ihr
Cache von selbst; ``prune_file_cache --unlisted`` räumt den Bestand ausgeblendeter Kommunen ab.
"""

import hashlib
import logging
import os
import re
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import IO

from django.conf import settings
from django.db.models import Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

# Ehrliche Kennung mit Kontakt — Kommunen sollen uns zuordnen (und freischalten) können.
USER_AGENT = "mandari-file-cache/1.0 (+https://mandari.de; support@mandari.de)"
STATUS_CHOICES = [
    ("none", "Nicht zwischengespeichert"),
    ("ok", "Lokal vorhanden"),
    ("missing", "Quelle liefert 404"),
    ("error", "Fehler beim Abruf"),
    ("too_large", "Zu groß für den Cache"),
]
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})


def cache_root() -> Path:
    return Path(settings.OPARL_FILES_ROOT)


def caches_body(body) -> bool:
    """Werden Dokumente dieser Kommune zwischengespeichert? Nur, wenn sie im Portal gelistet ist."""
    return bool(getattr(body, "is_listed", True))


def max_bytes() -> int:
    return int(getattr(settings, "FILE_CACHE_MAX_MB", 80)) * 1024 * 1024


def min_free_bytes() -> int:
    return int(getattr(settings, "FILE_CACHE_MIN_FREE_GB", 15)) * 1024**3


def backoff_failures() -> int:
    return int(getattr(settings, "INSIGHT_SOURCE_BACKOFF_FAILURES", 3))


def download_headers(body) -> dict[str, str]:
    """
    Zusätzliche Header für Datei-Downloads je Quelle (``sync_config["download_headers"]``,
    Issue #116): manche RIS liefern Anlagen nur mit Referer oder Sitzungs-Cookie aus.
    Nur String-Werte; leer ohne Body oder Konfiguration.
    """
    source = getattr(body, "source", None) if body is not None else None
    sync_config = getattr(source, "sync_config", None) or {}
    headers = sync_config.get("download_headers") if isinstance(sync_config, dict) else None
    if not isinstance(headers, dict):
        return {}
    return {str(k): str(v) for k, v in headers.items() if isinstance(v, str | int | float) and str(k).strip()}


def source_paused(body) -> bool:
    """
    Quellen-Schonung: Hat der Ingestor die Quelle mehrfach in Folge nicht erreicht
    (Ratenlimit, IP-Sperre, Bot-Schutz), fragen Cache und Proxy sie nicht weiter an.
    Sobald ein Sync wieder gelingt, setzt der Ingestor den Zähler zurück.
    """
    source = getattr(body, "source", None) if body is not None else None
    if source is None:
        return False
    return (source.consecutive_failures or 0) >= backoff_failures()


def http_timeout():
    import httpx

    return httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


# =============================================================================
# Pfade
# =============================================================================


def body_dir_name(body) -> str:
    """Lesbarer Verzeichnisname je Kommune (Slug, sonst aus dem Namen abgeleitet)."""
    from django.utils.text import slugify

    if body.slug:
        return body.slug
    base = (body.short_name or body.name or "").translate(_UMLAUTS)
    return slugify(base) or str(body.id)


def _extension(file_obj) -> str:
    mime = (file_obj.mime_type or "").lower()
    if "pdf" in mime:
        return "pdf"
    if file_obj.file_name and "." in file_obj.file_name:
        ext = file_obj.file_name.rsplit(".", 1)[-1].lower()
        if re.fullmatch(r"[a-z0-9]{1,5}", ext):
            return ext
    return "bin"


def target_path(file_obj) -> Path:
    when = file_obj.file_date or file_obj.oparl_created or file_obj.created_at or timezone.now()
    return cache_root() / body_dir_name(file_obj.body) / str(when.year) / f"{file_obj.id}.{_extension(file_obj)}"


def local_file(file_obj) -> Path | None:
    """Pfad der lokalen Kopie, wenn sie tatsächlich existiert (sonst None)."""
    if not file_obj.local_path:
        return None
    path = Path(file_obj.local_path)
    if path.is_file():
        return path
    return None


def disk_free_bytes() -> int:
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(root).free


def has_room_for(size: int) -> bool:
    return disk_free_bytes() - size > min_free_bytes()


def looks_like_html(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head[:200]


def content_type_for(file_obj, fallback: str = "application/octet-stream") -> str:
    """Normalisiert MIME-Typen wie „pdf“ (Bonn) auf application/pdf."""
    mime = (file_obj.mime_type or "").strip().lower()
    if not mime:
        return fallback
    if "/" not in mime:
        return "application/pdf" if mime == "pdf" else fallback
    return mime


# =============================================================================
# Speichern & Abrufen
# =============================================================================


def _mark(file_obj, status: str, error: str = "") -> str:
    file_obj.local_status = status
    file_obj.local_error = error[:500]
    file_obj.local_cached_at = timezone.now() if status == "ok" else file_obj.local_cached_at
    file_obj.save(update_fields=["local_status", "local_error", "local_cached_at"])
    return status


def store_bytes(file_obj, data: bytes, *, content_type: str | None = None) -> Path:
    """Datei atomar ablegen und Metadaten (Pfad, Größe, Hash, Status) setzen."""
    path = target_path(file_obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return _record_stored(file_obj, path, len(data), hashlib.sha256(data).hexdigest(), content_type)


def store_stream(file_obj, source: IO[bytes], *, content_type: str | None = None) -> Path:
    """Wie ``store_bytes``, aber aus einer Datei gelesen (ohne alles in den Speicher zu laden)."""
    path = target_path(file_obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    digest = hashlib.sha256()
    size = 0
    source.seek(0)
    with open(tmp, "wb") as fh:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            fh.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    os.replace(tmp, path)
    return _record_stored(file_obj, path, size, digest.hexdigest(), content_type)


def _record_stored(file_obj, path: Path, size: int, sha256: str, content_type: str | None) -> Path:
    file_obj.local_path = str(path)
    file_obj.size = size
    file_obj.sha256_hash = sha256
    file_obj.local_status = "ok"
    file_obj.local_error = ""
    file_obj.local_cached_at = timezone.now()
    update_fields = ["local_path", "size", "sha256_hash", "local_status", "local_error", "local_cached_at"]
    if not file_obj.mime_type and content_type:
        file_obj.mime_type = content_type.split(";")[0].strip()[:100]
        update_fields.append("mime_type")
    file_obj.save(update_fields=update_fields)
    return path


def fetch_and_cache(file_obj, client=None) -> str:
    """
    Datei aus dem RIS laden und lokal ablegen.

    Rückgabe: "ok", "missing", "error", "too_large", "disk_full", "skipped", "paused".
    """
    import httpx

    if local_file(file_obj):
        if file_obj.local_status != "ok":
            _mark(file_obj, "ok")
        return "skipped"
    if source_paused(file_obj.body):
        return "paused"
    url = file_obj.download_url or file_obj.access_url
    if not url:
        return _mark(file_obj, "error", "Keine Download-URL")

    from .safe_fetch import guarded_client

    own_client = client is None
    if own_client:
        # Nur öffentliche Ziele, auch nach Weiterleitungen: die Kopie wird später ausgeliefert
        client = guarded_client(headers={"User-Agent": USER_AGENT}, timeout=http_timeout(), follow_redirects=True)
    try:
        try:
            with client.stream("GET", url, headers=download_headers(file_obj.body)) as response:
                if response.status_code in (404, 410):
                    return _mark(file_obj, "missing", f"HTTP {response.status_code}")
                if response.status_code != 200:
                    return _mark(file_obj, "error", f"HTTP {response.status_code}")
                declared = int(response.headers.get("content-length") or 0)
                if declared > max_bytes():
                    return _mark(file_obj, "too_large", f"{declared // 1024 // 1024} MB")
                if not has_room_for(max(declared, 0)):
                    return "disk_full"
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes():
                        return _mark(file_obj, "too_large", f"> {max_bytes() // 1024 // 1024} MB")
                    chunks.append(chunk)
                data = b"".join(chunks)
                content_type = response.headers.get("content-type", "")
        except httpx.HTTPError as exc:
            return _mark(file_obj, "error", f"{type(exc).__name__}: {exc}")

        if not data:
            return _mark(file_obj, "error", "Leere Antwort")
        if looks_like_html(data) and "html" not in (file_obj.mime_type or "").lower():
            return _mark(file_obj, "error", "Quelle liefert eine HTML-Seite statt der Datei")
        if not has_room_for(len(data)):
            return "disk_full"
        store_bytes(file_obj, data, content_type=content_type)
        return "ok"
    finally:
        if own_client:
            client.close()


def pending_queryset(body=None, retry_errors: bool = False):
    from ..models import OParlFile

    statuses = ["none"] + (["error"] if retry_errors else [])
    # text_content/raw_json sind riesig (extrahierter Volltext) — nie mitladen,
    # sonst frisst ein Lauf über zehntausende Dateien den gesamten RAM.
    # Quellen in Schonung (mehrfach nicht erreichbar) werden ausgelassen — Nachladen
    # würde die Sperre nur verlängern (Issue #89).
    qs = (
        OParlFile.objects.filter(deleted=False, local_status__in=statuses, body__is_listed=True)
        .exclude(body__source__consecutive_failures__gte=backoff_failures())
        .select_related("body", "body__source")
        .defer("text_content", "raw_json", "body__raw_json")
    )
    if body is not None:
        qs = qs.filter(body=body)
    return qs.order_by("-file_date", "-oparl_created", "-created_at")


def cache_pending(body=None, *, limit: int = 500, retry_errors: bool = False, sleep: float = 0.05) -> Counter:
    """Fehlende Kopien nachladen — neueste Dokumente zuerst, mit Festplatten-Schutz."""
    from .safe_fetch import guarded_client

    results: Counter = Counter()
    if not has_room_for(0):
        results["disk_full"] += 1
        return results

    with guarded_client(headers={"User-Agent": USER_AGENT}, timeout=http_timeout(), follow_redirects=True) as client:
        for file_obj in pending_queryset(body, retry_errors)[:limit].iterator(chunk_size=200):
            status = fetch_and_cache(file_obj, client)
            results[status] += 1
            if status == "disk_full":
                break
            if sleep:
                time.sleep(sleep)
    return results


# =============================================================================
# Statistik
# =============================================================================


def cache_stats() -> dict:
    from ..models import OParlFile

    qs = OParlFile.objects.filter(deleted=False)
    total = qs.count()
    by_status = dict(Counter(qs.values_list("local_status", flat=True)))
    cached_bytes = qs.filter(local_status="ok").aggregate(s=Sum("size"))["s"] or 0
    ok = by_status.get("ok", 0)
    paused = qs.filter(local_status="none", body__source__consecutive_failures__gte=backoff_failures()).count()
    per_body = []
    for row in (
        qs.values("body__name").annotate(n=Sum(1), cached=Sum("size", filter=Q(local_status="ok"))).order_by("-n")
    ):
        per_body.append({"body": row["body__name"], "files": row["n"], "cached_bytes": row["cached"] or 0})
    return {
        "root": str(cache_root()),
        "total": total,
        "ok": ok,
        # offen = nur gelistete Kommunen; ausgeblendete werden bewusst nicht zwischengespeichert
        "pending": qs.filter(local_status="none", body__is_listed=True).count(),
        "unlisted": qs.filter(body__is_listed=False).count(),
        "missing": by_status.get("missing", 0),
        "error": by_status.get("error", 0),
        "too_large": by_status.get("too_large", 0),
        "paused": paused,
        "coverage": round(ok / total * 100, 1) if total else 0.0,
        "cached_bytes": cached_bytes,
        "cached_gb": round(cached_bytes / 1024**3, 2),
        "disk_free_bytes": disk_free_bytes(),
        "min_free_gb": min_free_bytes() // 1024**3,
        "per_body": per_body,
    }
