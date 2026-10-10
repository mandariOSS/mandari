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

Abgerufen wird nur über ``hub.ris.abruf`` (Issue #919, ein Weg zur Quelle): Zustände, Wiederholungen und
Beanspruchung stehen dort. Dieses Modul ist die Fassade für ``insight_core`` (``fetch_and_cache``,
``pending_queryset``, ``cache_pending``, ``stores_file``) und hält Pfade, Ablegen und Statistik.

Abgelegt werden gelistete Kommunen; mit ``TEXT_EXTRACTION_RUNNER=worker`` alle Quellen mit erlaubtem Abruf ab
ihrem Stichtag (``hub.ris.abruf.stores_file``). Ausgeblendete Quellen (Piloten, Tests) luden früher ihr ganzes
Archiv nach – im September 2026 rund 46 GB, knapp die Hälfte des Caches, für Kommunen, die niemand im Portal
sieht. Wird eine Kommune gelistet, füllt sich ihr Cache von selbst; ``prune_file_cache --unlisted`` räumt den
Bestand ausgeblendeter Kommunen ab, die nichts ablegen.

Die Gesamtgröße lässt sich begrenzen (``FILE_CACHE_MAX_TOTAL_GB``, Issue #961): Darüber verdrängt das stündliche
Aufräumen die am wenigsten gebrauchten Dokumente (``services/file_cache_limit.py``), und ``cache_pending`` lädt nur
bis zur Grenze nach. Verdrängte Dokumente (``evicted``) holt der Abrufweg nur ausdrücklich: die Vorschau bei Bedarf
und ``cache_files --verdraengte``.
"""

import hashlib
import logging
import os
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import IO, Any

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
    ("fetching", "Wird abgerufen"),
    ("ok", "Lokal vorhanden"),
    ("retry", "Abruf wird wiederholt"),
    ("missing", "Quelle liefert 404/410"),
    ("refused", "Quelle verweigert den Abruf"),
    ("error", "Fehler beim Abruf"),
    ("too_large", "Zu groß für den Cache"),
    ("evicted", "Verdrängt (bei Bedarf neu abrufbar)"),
]
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})


def cache_root() -> Path:
    return Path(settings.OPARL_FILES_ROOT)


def caches_body(body) -> bool:
    """Legt diese Kommune überhaupt Dokumente ab (gelistet bzw. Ablage für alle, ``hub.ris.abruf.stores_body``)?"""
    from hub.ris import abruf

    return abruf.stores_body(body)


def stores_file(file_obj) -> bool:
    """Wird diese Datei abgelegt (Regel der Ablage samt Stichtag, ``hub.ris.abruf.stores_file``)?"""
    from hub.ris import abruf

    return abruf.stores_file(file_obj)


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
    return downloads_disabled_in(source.sync_config)


def downloads_disabled_in(sync_config) -> bool:
    """Schaltet ``sync_config`` den Dateiabruf ab (``file_downloads`` ist ``false``)?"""
    return isinstance(sync_config, dict) and sync_config.get(FILE_DOWNLOADS_KEY) is False


def sources_without_downloads() -> list:
    """
    Quellen mit abgeschaltetem Dateiabruf (Primärschlüssel). Es gibt nur wenige Quellen; der Abgleich in
    Python verhält sich in PostgreSQL und SQLite gleich (JSON-Vergleiche auf ``false`` tun das nicht).
    """
    from ..models import OParlSource

    return [pk for pk, config in OParlSource.objects.values_list("pk", "sync_config") if downloads_disabled_in(config)]


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


def store_bytes(file_obj, data: bytes, *, content_type: str | None = None) -> Path:
    """Datei atomar ablegen und Metadaten (Pfad, Größe, Hash, Status) setzen."""
    from . import file_store

    if file_store.uses_blobs():
        with file_store.Spool() as spool:
            spool.write(data)
            return file_store.store_spool(file_obj, spool, content_type=content_type)
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
    from . import file_store

    if file_store.uses_blobs():
        with file_store.Spool() as spool:
            spool.copy_from(source)
            return file_store.store_spool(file_obj, spool, content_type=content_type)
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
    # Abruf gelungen: Zähler und Fehlercode des Abrufs zurück (Issue #919)
    file_obj.fetch_attempts = 0
    file_obj.fetch_next_at = None
    file_obj.fetch_error = ""
    update_fields = [
        "local_path",
        "local_size",
        "size",
        "sha256_hash",
        "local_status",
        "local_error",
        "local_cached_at",
        "fetch_attempts",
        "fetch_next_at",
        "fetch_error",
    ]
    if not file_obj.mime_type and content_type:
        file_obj.mime_type = content_type.split(";")[0].strip()[:100]
        update_fields.append("mime_type")
    file_obj.save(update_fields=update_fields)
    return path


def fetch_and_cache(file_obj, client=None, *, include_errors: bool = False, force: bool = False) -> str:
    """
    Datei aus dem RIS laden und ablegen – über den einen Weg zur Quelle (``hub.ris.abruf.abrufen``).

    Rückgabe: neuer Zustand (``ok``, ``retry``, ``missing``, ``refused``, ``too_large``, ``error``) oder ein
    Ergebnis ohne Zustand (``skipped``: schon abgelegt, beansprucht oder nicht fällig; ``paused``: Quelle in
    Schonung bzw. Abruf abgeschaltet; ``excluded``: gelöscht, gesperrt oder geleert; ``disk_full``;
    ``storage_error``: Ablage gestört, kein Quellabruf; ``busy``). ``include_errors``: auch nicht fällige
    Fehler erneut versuchen; ``force``: jeden Zustand außer ``ok`` (Admin-Aktion von Hand).
    """
    from hub.ris import abruf

    return abruf.abrufen(file_obj, client=client, include_errors=include_errors, force=force)


def download_to_file(url: str, **kwargs: Any) -> Any:
    """
    Datei gestreamt in eine temporäre Datei laden, ohne sie abzulegen (``hub.ris.abruf.download_to_file``).

    Nur noch für den Auftrag ``file.extract_text`` bei Dateien, die nicht abgelegt werden; er ruft ab Etappe 1,
    Teil B der Dokumentkette (Issue #919) nicht mehr selbst bei der Quelle ab.
    """
    from hub.ris import abruf

    return abruf.download_to_file(url, **kwargs)


def pending_queryset(body=None, retry_errors: bool = False, retry_evicted: bool = False):
    """
    Dateien, die ``cache_files`` abrufen soll (``hub.ris.abruf.pending_queryset``). Verdrängte Dokumente (Obergrenze,
    #961) nur mit ``retry_evicted`` (``cache_files --verdraengte``).
    """
    from hub.ris import abruf

    return abruf.pending_queryset(body, retry_errors=retry_errors, retry_evicted=retry_evicted)


def cache_pending(
    body=None, *, limit: int = 500, retry_errors: bool = False, retry_evicted: bool = False, sleep: float = 0.05
) -> Counter:
    """
    Fehlende Kopien nachladen — fällige Wiederholungen und neueste Dokumente zuerst, mit Festplatten-Schutz
    (``hub.ris.abruf.nachladen``).

    Mit Obergrenze (``FILE_CACHE_MAX_TOTAL_GB``, #961) lädt ein Lauf nur bis zur Grenze nach (``limit`` im
    Ergebnis); Platz schafft die Verdrängung im stündlichen Aufräumen (``dokumentablage --aufraeumen``).
    """
    from hub.ris import abruf

    return abruf.nachladen(body, limit=limit, retry_errors=retry_errors, retry_evicted=retry_evicted, sleep=sleep)


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


def stored_bytes() -> int:
    """
    Belegung der Ablage: jeder referenzierte Inhalt einmal (``oparl_file_blobs``) plus Kopien im alten Layout.

    Mit eingeschaltetem Objektspeicher liegt davon lokal höchstens ``OBJ_CACHE_MAX_GB``.
    """
    from ..models import OParlFile, OParlFileBlob

    blobs = OParlFileBlob.objects.filter(ref_count__gt=0).aggregate(s=Sum("size"))["s"] or 0
    legacy = (
        OParlFile.objects.filter(local_status="ok", blob__isnull=True).aggregate(s=Sum(Coalesce("local_size", "size")))[
            "s"
        ]
        or 0
    )
    return int(blobs) + int(legacy)


def remote_bytes() -> int:
    """Inhalte im Objektspeicher (``remote_at``), auf die Dokumente verweisen; jeder Inhalt einmal."""
    from ..models import OParlFileBlob

    return int(
        OParlFileBlob.objects.filter(remote_at__isnull=False, ref_count__gt=0).aggregate(s=Sum("size"))["s"] or 0
    )


def cache_stats() -> dict:
    from hub.ris import abruf

    from ..models import OParlFile
    from . import file_cache_limit

    qs = OParlFile.objects.filter(deleted=False)
    abzulegen = abruf.storable_q()
    jetzt = timezone.now()
    total = qs.count()
    by_status = dict(Counter(qs.values_list("local_status", flat=True)))
    # Gemessene Größe der Kopie (#786); für Kopien vor deren Einführung die Angabe aus der Quelle
    stored = Coalesce("local_size", "size")
    # Je Datei gezählt: Dateien mit gleichem Inhalt zählen mehrfach (Ablage nach SHA-256, #788)
    cached_bytes = qs.filter(local_status="ok").aggregate(s=Sum(stored))["s"] or 0
    without_size = qs.filter(local_status="ok", local_size__isnull=True).count()
    ok = by_status.get("ok", 0)
    paused = qs.filter(
        Q(body__source__consecutive_failures__gte=backoff_failures())
        | Q(body__source_id__in=sources_without_downloads()),
        local_status="none",
    ).count()
    # Mit Objektspeicher liegt nur ein Teil lokal: tatsächliche Belegung aus dem Durchlauf über die Platte (wie die
    # Obergrenze, #961), getrennt von dem, was im Objektspeicher liegt. Ohne ist die Summe aus der Datenbank genau.
    with_remote = file_cache_limit.mode() == file_cache_limit.MODE_REMOTE
    stored_total = stored_bytes()
    per_body = []
    for row in (
        qs.values("body__name").annotate(n=Sum(1), cached=Sum(stored, filter=Q(local_status="ok"))).order_by("-n")
    ):
        per_body.append({"body": row["body__name"], "files": row["n"], "cached_bytes": row["cached"] or 0})
    return {
        "root": str(cache_root()),
        "total": total,
        "ok": ok,
        # offen = nur Dateien, die abgelegt werden (gelistet bzw. Ablage für alle ab Stichtag, hub.ris.abruf)
        "pending": qs.filter(abzulegen, local_status="none").count(),
        # bewusst nicht abgelegt: ausgeblendete Kommunen, Altbestand vor dem Stichtag, Abruf abgeschaltet
        "not_stored": qs.exclude(abzulegen).count(),
        "fetching": by_status.get("fetching", 0),
        "retry": by_status.get("retry", 0),
        "retry_due": qs.filter(local_status="retry", fetch_next_at__lte=jetzt).count(),
        "missing": by_status.get("missing", 0),
        "refused": by_status.get("refused", 0),
        "error": by_status.get("error", 0),
        "too_large": by_status.get("too_large", 0),
        # Von der Obergrenze verdrängt (#961, ohne Objektspeicher): holt die Vorschau bei Bedarf neu, sonst nur
        # cache_files --verdraengte (kein Zustand, aus dem der Abruf von selbst beansprucht)
        "evicted": by_status.get("evicted", 0),
        "paused": paused,
        "coverage": round(ok / total * 100, 1) if total else 0.0,
        "cached_bytes": cached_bytes,
        "cached_gb": round(cached_bytes / 1024**3, 2),
        # Abgelegt: jeder Inhalt einmal (Ablage nach SHA-256) plus Kopien im alten Layout, lokal oder im Objektspeicher
        "stored_bytes": stored_total,
        "object_storage": with_remote,
        # Lokal auf der Platte (mit Objektspeicher gemessen, sonst gleich stored_bytes)
        "local_bytes": file_cache_limit.usage_bytes() if with_remote else stored_total,
        # Im Objektspeicher (nur mit eingeschaltetem Objektspeicher)
        "remote_bytes": remote_bytes() if with_remote else 0,
        # Kopien ohne gemessene Größe: mit ``cache_files --sizes`` nachtragen
        "without_size": without_size,
        "disk_free_bytes": disk_free_bytes(),
        "min_free_gb": min_free_bytes() // 1024**3,
        # Obergrenze der Gesamtgröße (0 = unbegrenzt) und ihr Zielwert in Prozent
        "max_total_bytes": file_cache_limit.limit_bytes(),
        "evict_target_percent": file_cache_limit.target_percent(),
        "per_body": per_body,
    }
