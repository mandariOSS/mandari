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

``<kommune>`` wird je Kommune einmal festgeschrieben (``OParlBody.file_cache_dir``, siehe
``body_dir_name``) und ändert sich danach nicht mehr – auch nicht mit einem neuen Slug.

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
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import robots

logger = logging.getLogger(__name__)

# Ehrliche Kennung mit Infoseite und Kontakt — Kommunen sollen uns zuordnen (und freischalten) können.
# Dasselbe Produkt-Token wie der Ingestor: eine robots.txt-Regel für uns gilt für alle Abrufe.
USER_AGENT = robots.USER_AGENT
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


#: Schalter je Quelle (``OParlSource.sync_config``): ``false`` = Dateien nicht automatisch abrufen.
#: Gleicher Schlüssel wie im Ingestor (``ingestor/src/client/source_options.py``).
FILE_DOWNLOADS_KEY = "file_downloads"


def downloads_disabled(body) -> bool:
    """
    Die Quelle liefert Dokumente nur hinter einer Zugangsprüfung für Menschen (z. B. ALTCHA vor den
    Anlagen). Cache, Vorschau und Textextraktion fragen sie dann gar nicht erst an – jeder Abruf brächte
    nur die Prüfseite und würde den Bot-Schutz weiter verschärfen. Geschaltet über
    ``sync_config["file_downloads"] = false``; die Dateien werden nachgeholt, sobald der Schalter fällt.
    """
    source = getattr(body, "source", None) if body is not None else None
    if source is None:
        return False
    return _downloads_disabled_in(source.sync_config)


def _downloads_disabled_in(sync_config) -> bool:
    return isinstance(sync_config, dict) and sync_config.get(FILE_DOWNLOADS_KEY) is False


def sources_without_downloads() -> list:
    """
    Quellen mit abgeschaltetem Dateiabruf (Primärschlüssel). Es gibt nur wenige Quellen; der Abgleich in
    Python verhält sich in PostgreSQL und SQLite gleich (JSON-Vergleiche auf ``false`` tun das nicht).
    """
    from ..models import OParlSource

    return [pk for pk, config in OParlSource.objects.values_list("pk", "sync_config") if _downloads_disabled_in(config)]


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


#: Längster Verzeichnisname, den Dateisysteme zulassen (NAME_MAX) – länger ließ sich nie etwas ablegen
MAX_DIR_NAME = 255


def derive_body_dir_name(slug, short_name, name, pk) -> str:
    """
    Bisherige Ableitung des Verzeichnisnamens: Slug, sonst Kurz- bzw. Langname (Umlaute umschrieben),
    sonst die ID. Gilt nur, solange für die Kommune noch kein Name festgeschrieben ist.
    """
    from django.utils.text import slugify

    if slug:
        return str(slug)
    base = (short_name or name or "").translate(_UMLAUTS)
    return slugify(base) or str(pk)


def body_dir_name(body) -> str:
    """
    Verzeichnisname der Kommune im Cache: der festgeschriebene (``OParlBody.file_cache_dir``), sonst
    wie bisher abgeleitet (Slug, Kurzname …).

    Festgeschrieben wird der Name beim ersten Ablegen einer Datei (``pin_body_dir``), durch die
    Migration für alle bestehenden Kommunen und vor jeder Änderung einer Kommune über Django
    (``OParlBody.save``). Ein neuer Slug verschiebt daher nichts (Issue #373): bereits
    abgelegte Dateien behalten ihren Pfad (``OParlFile.local_path``), neue landen daneben.
    """
    stored = getattr(body, "file_cache_dir", None)
    if stored:
        return str(stored)
    return derive_body_dir_name(body.slug, body.short_name, body.name, body.id)


def pin_body_dir(body) -> str:
    """
    Verzeichnisnamen der Kommune festschreiben (einmalig) und zurückgeben.

    Maßgeblich ist der Stand in der Datenbank, nicht das Objekt im Speicher: Wer Slug oder Kurznamen
    gerade ändert, soll den bisherigen Namen festschreiben. Ein bereits festgeschriebener Name wird
    nie überschrieben (bedingtes UPDATE, auch bei gleichzeitigen Läufen). Ohne Datenbankzeile
    (noch nicht gespeichert) wird nur abgeleitet.
    """
    from ..models import OParlBody

    if body.file_cache_dir:
        return str(body.file_cache_dir)
    row = OParlBody.objects.filter(pk=body.pk).values("file_cache_dir", "slug", "short_name", "name").first()
    if row is None:
        return body_dir_name(body)
    name = row["file_cache_dir"] or derive_body_dir_name(row["slug"], row["short_name"], row["name"], body.pk)
    if not row["file_cache_dir"]:
        if len(name) > MAX_DIR_NAME:
            return name  # Solch ein Verzeichnis lässt sich nicht anlegen – nichts festschreiben
        offen = Q(file_cache_dir__isnull=True) | Q(file_cache_dir="")
        if not OParlBody.objects.filter(offen, pk=body.pk).update(file_cache_dir=name):
            # Ein paralleler Lauf war schneller – dessen Namen übernehmen
            name = OParlBody.objects.filter(pk=body.pk).values_list("file_cache_dir", flat=True).first() or name
    body.file_cache_dir = name
    return name


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
    pin_body_dir(file_obj.body)
    path = target_path(file_obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return _record_stored(file_obj, path, len(data), hashlib.sha256(data).hexdigest(), content_type)


def store_stream(file_obj, source: IO[bytes], *, content_type: str | None = None) -> Path:
    """Wie ``store_bytes``, aber aus einer Datei gelesen (ohne alles in den Speicher zu laden)."""
    pin_body_dir(file_obj.body)
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
    file_obj.local_size = size
    file_obj.size = size
    file_obj.sha256_hash = sha256
    file_obj.local_status = "ok"
    file_obj.local_error = ""
    file_obj.local_cached_at = timezone.now()
    update_fields = [
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
        update_fields.append("mime_type")
    file_obj.save(update_fields=update_fields)
    return path


def fetch_and_cache(file_obj, client=None) -> str:
    """
    Datei aus dem RIS laden und lokal ablegen.

    Rückgabe: "ok", "missing", "error", "too_large", "disk_full", "skipped", "paused", "robots" (die
    robots.txt sperrt die Datei; vermerkt, bis eine Freigabe sie neu einreiht) oder "deferred" (die robots.txt
    ist nicht erreichbar; nichts vermerkt, der nächste Lauf versucht es erneut).
    """
    import httpx

    if local_file(file_obj):
        if file_obj.local_status != "ok":
            _mark(file_obj, "ok")
        return "skipped"
    if source_paused(file_obj.body) or downloads_disabled(file_obj.body):
        return "paused"
    url = file_obj.download_url or file_obj.access_url
    if not url:
        return _mark(file_obj, "error", "Keine Download-URL")
    # robots.txt gilt auch für Dateien (RFC 9309), geprüft mit dem User-Agent des Abrufs; nach einer Freigabe
    # reiht robots_override neu ein. Nicht erreichbar ist keine Sperre: nichts vermerken, später erneut.
    decision = robots.check(
        url, robots.KIND_FILES, sync_config=robots.sync_config_of(file_obj), agent=robots.user_agent_for(file_obj)
    )
    if decision.unreachable:
        return "deferred"
    if not decision.allowed:
        _mark(file_obj, "error", decision.reason)
        return "robots"

    from . import host_pacing
    from .safe_fetch import guarded_client

    own_client = client is None
    if own_client:
        # Nur öffentliche Ziele, auch nach Weiterleitungen: die Kopie wird später ausgeliefert
        client = guarded_client(headers={"User-Agent": USER_AGENT}, timeout=http_timeout(), follow_redirects=True)
    try:
        try:
            # Drossel je Host über alle Prozesse (Ingestor, Vorschau, andere Quellen auf dem Host)
            host_pacing.wait(url, sync_config=robots.sync_config_of(file_obj))
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
        .exclude(body__source_id__in=sources_without_downloads())
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


def backfill_sizes(batch: int = 2000) -> Counter:
    """
    Gemessene Größe für vorhandene Kopien nachtragen (``local_size`` aus der Datei auf der Platte).

    Idempotent und wiederaufnehmbar: bearbeitet nur Kopien ohne Größe. Fehlt die Datei, bleibt die
    Zeile unverändert (``cache_files --stats`` zählt sie weiter als „ohne Größe“).
    """
    from ..models import OParlFile

    results: Counter = Counter()
    last_pk = None
    while True:
        qs = OParlFile.objects.filter(local_status="ok", local_size__isnull=True).order_by("pk")
        if last_pk is not None:
            qs = qs.filter(pk__gt=last_pk)
        rows = list(qs.values_list("pk", "local_path")[:batch])
        if not rows:
            return results
        last_pk = rows[-1][0]
        for pk, local_path in rows:
            try:
                size = Path(local_path).stat().st_size if local_path else None
            except OSError:
                size = None
            if size is None:
                results["missing"] += 1
                continue
            OParlFile.objects.filter(pk=pk, local_size__isnull=True).update(local_size=size)
            results["updated"] += 1


# =============================================================================
# Statistik
# =============================================================================


def cache_stats() -> dict:
    from ..models import OParlFile

    qs = OParlFile.objects.filter(deleted=False)
    total = qs.count()
    by_status = dict(Counter(qs.values_list("local_status", flat=True)))
    # Gemessene Größe der Kopie (#786); für Kopien vor deren Einführung die Angabe aus der Quelle
    stored = Coalesce("local_size", "size")
    cached_bytes = qs.filter(local_status="ok").aggregate(s=Sum(stored))["s"] or 0
    without_size = qs.filter(local_status="ok", local_size__isnull=True).count()
    ok = by_status.get("ok", 0)
    paused = qs.filter(
        Q(body__source__consecutive_failures__gte=backoff_failures())
        | Q(body__source_id__in=sources_without_downloads()),
        local_status="none",
    ).count()
    per_body = []
    for row in (
        qs.values("body__name").annotate(n=Sum(1), cached=Sum(stored, filter=Q(local_status="ok"))).order_by("-n")
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
        # Kopien ohne gemessene Größe: mit ``cache_files --sizes`` nachtragen
        "without_size": without_size,
        "disk_free_bytes": disk_free_bytes(),
        "min_free_gb": min_free_bytes() // 1024**3,
        "per_body": per_body,
    }
