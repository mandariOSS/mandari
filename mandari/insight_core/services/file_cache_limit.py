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
  die sicher im Objektspeicher liegen: Jeder echte Lauf fragt vorher per ``HEAD`` nach (``object_storage.remote_size``,
  vorhanden mit derselben Größe). Fehlt der Inhalt dort, bleibt die Kopie; ist der Objektspeicher gestört, bricht der
  Lauf ab (nicht vorhanden ist nicht gestört, Dokumentkette #919). Die Datenbank bleibt unverändert, die Dokumente
  bleiben „Lokal vorhanden“; Vorschau und Texterkennung holen den Inhalt bei Bedarf aus dem Objektspeicher
  (``file_store.local_copy``), nie von der Quelle.
* Ohne Objektspeicher geben alle Dateien eines Inhalts ihre Referenz gemeinsam frei und gehen auf ``evicted``
  („Verdrängt“); danach verweist kein Dokument mehr auf den Inhalt, und er wird gelöscht. ``evicted`` ist kein
  Zustand, aus dem der Abruf (``hub.ris.abruf``) von selbst beansprucht: Die Vorschau holt ein verdrängtes Dokument
  bei Bedarf über den Abrufweg von der Quelle und legt es wieder ab; ``cache_files`` lädt verdrängte Dokumente nur mit
  ``--verdraengte``. Extrahierter Text, Status der Texterkennung und Fingerabdruck (``sha256_hash``) bleiben
  unberührt, es wird also nichts neu erkannt; die Texterkennung beansprucht nur abgelegte Dokumente (``ok``).

**Nie verdrängt** werden Inhalte, deren Text gerade erkannt wird oder darauf wartet (``pending``/``processing`` oder ein
wartender Auftrag ``file.extract_text``, etwa eine Neuerkennung: die Erkennung liest die lokale Kopie), Dokumente, die
gerade abgerufen werden (``fetching``), Kopien aus der letzten Stunde und – ohne Objektspeicher – Dokumente, die sich
nicht neu abrufen ließen: Quelle in Schonung oder nicht aktiv, Dateiabruf abgeschaltet, synthetische Quelle (Domäne
``.invalid``, etwa die Demo), keine Download-Adresse oder eine Quelle, die Dateiabrufe verweigert (``refused``,
robots.txt sperrt Dateien, Dokumente mit HTTP 401/403, siehe ``refusing_sources``). Gesperrte Dokumente
(Löschabgleich, #787) behandelt die Grenze wie alle anderen; ihre Sperre, Frist und Löschung bleiben, wie sie sind.

**Moduswechsel:** Liegen Inhalte laut Datenbank im Objektspeicher (``remote_at``), ist er hier aber nicht
konfiguriert (``OBJ_ENABLED=false`` im Worker, ein Container ohne ``OBJ_*``), setzt die Grenze aus und meldet das
(``Result.aborted``): Freigegebene Inhalte würden verwaisen, und das nächste Aufräumen mit Objektspeicher löschte sie
dort. Ein Handlauf kann ausdrücklich ohne Objektspeicher weitermachen (``without_object_storage``); Inhalte mit
``remote_at`` bleiben auch dann unangetastet (Grund ``im_objektspeicher``), sie verwaisen nie.

**Prüfung im Objektspeicher:** Vor dem Löschen einer lokalen Kopie fragt jeder echte Lauf per ``HEAD`` nach, ob der
Inhalt mit seiner Größe im Objektspeicher liegt. Fehlt er oder weicht die Größe ab, bleibt die Kopie, und ``remote_at``
wird zurückgesetzt, damit ``dokumentablage --hochladen`` ihn erneut überträgt; eine Störung bricht den Lauf ab. Im
Probelauf prüft nur ``verify_remote`` (``prune_file_cache --pruefe-objektspeicher``) alle oder eine Stichprobe der
Inhalte, die gingen; geändert wird dabei nichts.

**Belegung:** Inhalte unter ``<OPARL_FILES_ROOT>/sha256/<ab>/`` auf der Platte (ohne Teil-Downloads) plus Kopien im
alten Layout je Kommune (Größe aus der Datenbank). Gelöscht wird nur unterhalb von ``OPARL_FILES_ROOT`` und nie über
symbolische Verweise.

**Ablauf in Stapeln:** Der Durchgang über die Platte summiert nur Größen. Danach liefert die Datenbank die Inhalte in
Rangfolge über einen Cursor (nie alle zugleich im Speicher); verdrängt wird je Stapel in einer kurzen Transaktion,
gerade gesperrte Zeilen (eine Datei wird eben abgelegt, ein Abruf oder die Texterkennung beansprucht sie) werden
übersprungen statt erwartet. Unter der Sperre wird in beiden Arten nachgeprüft (``_still_evictable``), damit eine eben
beanspruchte Texterkennung oder ein laufender Abruf ihre Kopie behält. Ein Lauf hält eine
Sperre im gemeinsamen Cache, damit Zeitplan und Handlauf nicht doppelt verdrängen.
"""

from __future__ import annotations

import heapq
import logging
import os
import random
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
from django.db.models import Case, Count, Exists, IntegerField, Max, OuterRef, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.common.einmalig import Sperre

from . import file_cache, file_store, object_storage

logger = logging.getLogger(__name__)

#: Status verdrängter Dokumente (ohne Objektspeicher): die Vorschau holt sie bei Bedarf neu
EVICTED = "evicted"
#: Die Texterkennung liest die lokale Kopie: solche Inhalte bleiben
BUSY_TEXT = ("pending", "processing")
#: Ein Abruf beansprucht die Datei gerade (``hub.ris.abruf``): ihre Kopie bleibt
FETCHING = "fetching"
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

#: Grund, aus dem ein Inhalt ohne konfigurierten Objektspeicher bleibt: Er liegt dort (``remote_at``) und verwaist nie
PROTECT_REMOTE = "im_objektspeicher"
#: Zustand eines verweigerten Abrufs (robots.txt, HTML-Seite statt der Datei; ``hub.ris.abruf``)
REFUSED = "refused"
#: Fehler, mit denen eine Quelle Dokumentabrufe verweigert (``local_error`` eines Abrufs mit Zustand ``error``)
REFUSED_ERRORS = ("HTTP 401", "HTTP 403")
#: Kennungen von Crawlern und Abrufprogrammen im User-Agent: Ihre Abrufe zählen nicht als Nutzung
_CRAWLER = re.compile(
    r"bot|crawl|spider|slurp|scrap|facebookexternalhit|meta-external|anthropic-ai|claude-web|cohere-ai"
    r"|googleother|google-extended|google-inspectiontool|mediapartners-google|feedfetcher"
    r"|python-requests|python-httpx|python-urllib|aiohttp|go-http-client|okhttp|curl/|wget/|libwww-perl",
    re.IGNORECASE,
)
#: Nur so viel des User-Agents wird geprüft (die Kopfzeile kommt vom Client)
_UA_MAX = 512

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
    "local_status",
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


def remote_without_storage() -> int:
    """
    Inhalte, auf die Dokumente verweisen und die laut Datenbank im Objektspeicher liegen, obwohl er hier nicht
    konfiguriert ist (0 mit Objektspeicher). Mehr als 0 heißt: stiller Moduswechsel, die Grenze setzt aus.
    """
    from ..models import OParlFile, OParlFileBlob

    if mode() == MODE_REMOTE:
        return 0
    referenced = Exists(OParlFile.objects.filter(blob_id=OuterRef("pk")))
    return OParlFileBlob.objects.filter(referenced, remote_at__isnull=False).count()


# =============================================================================
# Nutzung vermerken
# =============================================================================


def is_crawler(user_agent: str | None) -> bool:
    """Erkennbarer Crawler oder Abrufprogramm (``bot``, ``crawler``, ``spider``, GPTBot, ClaudeBot, Bytespider …)?"""
    return bool(user_agent) and _CRAWLER.search(str(user_agent)[:_UA_MAX]) is not None


def counts_as_use(request: Any) -> bool:
    """Zählt diese Auslieferung für die Rangfolge? Nicht für erkennbare Crawler: Sie sollen sie nicht bestimmen."""
    return not is_crawler((getattr(request, "META", None) or {}).get("HTTP_USER_AGENT", ""))


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


def paused_sources() -> set[Any]:
    """
    Quellen, die gerade nicht angefragt werden (Schonung, Dateiabruf abgeschaltet, nicht aktiv: kein „Abruf
    erlaubt“ im Sinn von ``hub.ris.abruf``) oder synthetisch sind.
    """
    from ..models import OParlSource

    sources = set(file_cache.sources_without_downloads())
    sources.update(OParlSource.objects.filter(is_active=False).values_list("pk", flat=True))
    sources.update(
        OParlSource.objects.filter(consecutive_failures__gte=file_cache.backoff_failures()).values_list("pk", flat=True)
    )
    sources.update(pk for pk, url in OParlSource.objects.values_list("pk", "url") if _synthetic(url))
    return sources


def refusing_sources() -> set[Any]:
    """
    Quellen, die Dateiabrufe verweigern, ohne in Schonung zu sein (die Schnittstelle antwortet, die Dokumente nicht):

    * verweigerte Abrufe (``refused``: robots.txt oder HTML-Seite statt der Datei, ``hub.ris.abruf``) und ältere
      Vermerke der robots.txt (Cache: ``error`` mit ``local_error``, Texterkennung: ``skipped``); eine Freigabe
      (``robots_override``, ``dokumentkette freigeben``) reiht sie neu ein und hebt damit auch den Schutz auf,
    * die robots.txt im gemeinsamen Cache sperrt eine Beispieladresse der Quelle (eine abgelegte Datei); ohne
      Eintrag im Cache gibt es keine Entscheidung, angefragt wird hier nie,
    * Dokumente, deren Abruf mit HTTP 401 oder 403 endete (etwa PDFs hinter einer Sperre).

    Bewusst grob: Ein einziges solches Dokument schützt die ganze Quelle. Ohne Objektspeicher wäre eine verdrängte
    Kopie sonst womöglich verloren; ``cache_files --retry-errors`` hebt den Schutz auf, sobald die Quelle wieder liefert.
    """
    from ..models import OParlFile, OParlSource
    from . import robots

    refused = Q(local_status=REFUSED) | (
        Q(local_status="error")
        & (Q(local_error__startswith=robots.SKIP_ERROR_PREFIX) | Q(local_error__in=REFUSED_ERRORS))
    )
    refused |= Q(text_extraction_status="skipped", text_extraction_error__startswith=robots.SKIP_ERROR_PREFIX)
    sources: set[Any] = set(
        OParlFile.objects.filter(refused).values_list("body__source_id", flat=True).distinct().order_by()
    )
    samples = dict(
        OParlFile.objects.filter(local_status="ok", body__source_id__isnull=False)
        .exclude(download_url__isnull=True)
        .exclude(download_url="")
        .values("body__source_id")
        .annotate(url=Max("download_url"))
        .order_by()
        .values_list("body__source_id", "url")
    )
    for source in OParlSource.objects.filter(pk__in=list(samples)).only("pk", "sync_config"):
        if source.pk in sources:
            continue
        url = samples[source.pk]
        decision = robots.cached_check(
            url, robots.KIND_FILES, sync_config=source.sync_config, agent=robots.user_agent_for(source)
        )
        if decision is not None and decision.blocked:
            sources.add(source.pk)
    sources.discard(None)
    return sources


def _bodies_of(sources: set[Any]) -> list[Any]:
    from ..models import OParlBody

    if not sources:
        return []
    return list(OParlBody.objects.filter(source_id__in=sources).values_list("pk", flat=True))


def unrefetchable_bodies() -> list[Any]:
    """Kommunen, deren Dokumente sich gerade nicht neu abrufen ließen (Schonung, kein Dateiabruf, synthetisch,
    Abrufe verweigert)."""
    return _bodies_of(paused_sources() | refusing_sources())


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
            fetching=Max(_flag(Q(local_status=FETCHING))),
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
            "fetching": bool(row["fetching"]),
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
            # Kopien im alten Layout stehen auf ``ok`` (``_legacy_files``): kein laufender Abruf
            "fetching": False,
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


def _protection(row: dict[str, Any], run_mode: str, now: datetime, queued: frozenset[str] = frozenset()) -> str | None:
    """
    Grund, aus dem ein Inhalt bleibt, sonst ``None``. ``queued``: Dateien mit wartendem Auftrag ``file.extract_text``
    (für Kopien im alten Layout schon hier; Inhalte prüft ``_still_evictable`` je Datei unter der Sperre).
    """
    if row["busy"] or (row["kind"] == _KIND_LEGACY and row["key"] in queued):
        return "texterkennung"
    if row.get("fetching"):
        return "abruf"
    cached = row["cached"]
    if cached is not None and cached > now - FRESH:
        return "frisch"
    if run_mode == MODE_REMOTE and row["kind"] == _KIND_BLOB:
        return None if row["remote"] is not None else "nicht_hochgeladen"
    if row["remote"] is not None:
        # Ohne konfigurierten Objektspeicher: Der Inhalt liegt dort und darf nie verwaisen (Moduswechsel)
        return PROTECT_REMOTE
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
    #: Inhalte im Objektspeicher (``remote_at``), obwohl er hier nicht konfiguriert ist (stiller Moduswechsel)
    remote_conflict: int = 0
    #: Lauf ausgesetzt: Moduswechsel ohne ausdrückliche Zustimmung (``without_object_storage``)
    aborted: bool = False
    #: Prüfung im Objektspeicher per ``HEAD``: vorhanden, fehlt, Größe weicht ab
    verified: Counter[str] = field(default_factory=Counter)

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
    #: Quellen, die Dateiabrufe verweigern (einmal je Lauf ermittelt, ``refusing_sources``)
    refusing: set[Any] = field(default_factory=set)
    names: dict[Any, str] = field(default_factory=dict)
    #: vor dem Löschen einer lokalen Kopie im Objektspeicher nachfragen
    verify: bool = False
    #: Probelauf mit Prüfung: Inhalte, die gingen (Schlüssel, Größe)
    candidates: list[tuple[str, int]] = field(default_factory=list)
    #: Dateien mit wartendem Auftrag ``file.extract_text`` (je Lauf einmal gelesen): ihre Kopie bleibt
    queued: frozenset[str] = frozenset()


def enforce(
    *,
    max_bytes: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    batch: int = BATCH,
    without_object_storage: bool = False,
    verify_remote: bool = False,
    sample: int = 0,
) -> Result:
    """
    Belegung auf ``FILE_CACHE_EVICT_TARGET_PERCENT`` der Grenze bringen, sobald sie die Grenze überschreitet.

    ``max_bytes`` ersetzt ``FILE_CACHE_MAX_TOTAL_GB`` (Handlauf, ``prune_file_cache --max-gb``). Mit ``dry_run`` wird
    nur berechnet, was ginge.

    ``without_object_storage``: weitermachen, obwohl Inhalte im Objektspeicher liegen, der hier nicht konfiguriert
    ist (sonst setzt der Lauf aus); diese Inhalte bleiben trotzdem. Mit Objektspeicher fragt jeder echte Lauf vor dem
    Löschen einer lokalen Kopie per ``HEAD`` nach (Dokumentkette #919: nur sicher vorhandene Inhalte verlassen die
    Platte, bei einer Störung wird nichts verdrängt). ``verify_remote`` prüft auch im Probelauf, alle bzw. ``sample``
    zufällig gewählte Inhalte.
    """
    limit = limit_bytes() if max_bytes is None else max(int(max_bytes), 0)
    result = Result(mode=mode(), limit=limit, target=target_bytes(limit), dry_run=dry_run)
    if not limit:
        result.disabled = True
        return result
    result.remote_conflict = remote_without_storage()
    if result.remote_conflict and not without_object_storage:
        result.aborted = True
        result.before = result.after = usage_bytes()
        logger.error(
            "Obergrenze des Dokument-Caches ausgesetzt: %s Inhalte liegen laut Datenbank im Objektspeicher, "
            "der hier nicht konfiguriert ist (OBJ_ENABLED, OBJ_*)",
            result.remote_conflict,
        )
        return result
    lock = None if dry_run else Sperre(LOCK_NAME, LOCK_TTL)
    if lock is not None and not lock.erwerben():
        result.locked = True
        return result
    try:
        _enforce(
            result,
            now or timezone.now(),
            max(1, batch),
            lock,
            # Im echten Lauf immer; im Probelauf nur auf Wunsch (je Inhalt eine Anfrage)
            verify=result.mode == MODE_REMOTE and (verify_remote or not dry_run),
            sample=max(int(sample), 0),
        )
    finally:
        if lock is not None:
            lock.freigeben()
    return result


def _enforce(result: Result, now: datetime, batch: int, lock: Sperre | None, *, verify: bool, sample: int) -> None:
    result.before = result.after = usage_bytes()
    if result.before <= result.limit:
        return
    roots = _roots()
    if roots is None:
        result.skipped["unsicher"] += 1
        return
    refusing = refusing_sources()
    unrefetchable = _bodies_of(paused_sources() | refusing)
    run = _Run(
        result=result,
        now=now,
        roots=roots,
        unrefetchable=set(unrefetchable),
        lock=lock,
        refusing=refusing,
        verify=verify,
        queued=_queued_files(),
    )
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
        reason = _protection(row, result.mode, now, run.queued)
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
    if run.verify and result.dry_run:
        _verify_candidates(run, sample)


def _queued_files() -> frozenset[str]:
    """Dateien, für die ein Auftrag ``file.extract_text`` wartet (Texterkennung bzw. Neuerkennung eingereiht)."""
    from .text_extraction_job import queued_file_ids

    try:
        return frozenset(queued_file_ids())
    except Exception:  # Ohne Journal (Tests, Rückfall) gibt es keine wartenden Aufträge
        logger.warning("Wartende Aufträge der Texterkennung nicht lesbar", exc_info=True)
        return frozenset()


def _apply(run: _Run, items: list[tuple[dict[str, Any], Path, int]]) -> bool:
    """Einen Stapel verdrängen; ``False``, wenn der Lauf abbrechen muss (Löschen scheitert)."""
    blobs = [item for item in items if item[0]["kind"] == _KIND_BLOB]
    legacy = [item for item in items if item[0]["kind"] == _KIND_LEGACY]
    ok = True
    if run.result.dry_run:
        _count_planned(run, blobs, legacy)
        if run.verify:
            run.candidates.extend((row["key"], size) for row, _, size in blobs)
    else:
        # Schonung und abgeschalteter Dateiabruf können sich seit der Planung geändert haben: je Stapel neu lesen
        run.unrefetchable = set(_bodies_of(paused_sources() | run.refusing))
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


def _remote_state(sha256: str, expected: int | None) -> str:
    """Liegt der Inhalt im Objektspeicher (``HEAD``)? ``vorhanden``, ``fehlt`` oder ``groesse_abweichend``."""
    size = object_storage.remote_size(sha256)
    if size is None:
        return "fehlt"
    if expected is not None and size != expected:
        return "groesse_abweichend"
    return "vorhanden"


def _verify_candidates(run: _Run, sample: int) -> None:
    """Probelauf: alle bzw. eine zufällige Stichprobe der Inhalte, die gingen, im Objektspeicher nachfragen."""
    from ..models import OParlFileBlob

    chosen = run.candidates
    if sample and len(chosen) > sample:
        chosen = random.sample(chosen, sample)
    for start in range(0, len(chosen), BATCH):
        part = chosen[start : start + BATCH]
        sizes = dict(OParlFileBlob.objects.filter(pk__in=[key for key, _ in part]).values_list("pk", "size"))
        for key, local in part:
            try:
                run.result.verified[_remote_state(key, sizes.get(key, local))] += 1
            except Exception:
                logger.warning("Objektspeicher nicht erreichbar, Prüfung abgebrochen", exc_info=True)
                run.result.verified["fehler"] += 1
                return


def _evict_local(run: _Run, items: list[tuple[dict[str, Any], Path, int]]) -> bool:
    """
    Mit Objektspeicher: nur die lokale Kopie löschen, die Dokumente behalten ihre Referenz.

    Erst per ``HEAD`` prüfen, ob der Inhalt sicher im Objektspeicher liegt (vor der Sperre, je Inhalt eine Anfrage),
    dann unter Zeilensperre nachprüfen (``_still_evictable``: eben beanspruchte Texterkennung, laufender Abruf, …) und
    löschen. Eine Störung des Objektspeichers bricht den Lauf ab; was bis dahin geprüft ist, darf noch gehen.
    """
    from ..models import OParlFileBlob

    planned = {row["key"]: (row, path, size) for row, path, size in items}
    sizes: dict[str, int] = {}
    if run.verify:
        sizes = {
            str(pk): size for pk, size in OParlFileBlob.objects.filter(pk__in=list(planned)).values_list("pk", "size")
        }
    present: list[str] = []
    ok = True
    for key, (_row, _path, size) in planned.items():
        if run.verify:
            try:
                state = _remote_state(key, sizes.get(key, size))
            except Exception:
                logger.warning("Objektspeicher nicht erreichbar: Verdrängen abgebrochen", exc_info=True)
                run.result.verified["fehler"] += 1
                run.result.skipped["fehler"] += 1
                ok = False
                break
            run.result.verified[state] += 1
            if state != "vorhanden":
                # Die lokale Kopie ist womöglich die einzige: sie bleibt. Fehlt der Inhalt oder weicht seine Größe ab,
                # lädt --hochladen ihn neu; bis dahin schützt „nicht hochgeladen“ die Kopie auch vor weiteren Läufen.
                OParlFileBlob.objects.filter(pk=key).update(remote_at=None)
                _skip(run, "nicht_im_objektspeicher")
                continue
        present.append(key)
    if not present:
        return ok
    files: dict[str, list[dict[str, Any]]] = {}
    evicted: list[str] = []
    try:
        with transaction.atomic():
            files = _locked_files(run, present, remote=True)
            for key, rows in files.items():
                reason = _still_evictable(run, rows, planned[key][0]["score"], refetch=False)
                if reason:
                    _skip(run, reason)
                    continue
                # Löschen, solange die Sperre gilt: Wer die Datei gleich beansprucht, liest dann aus dem Objektspeicher
                try:
                    planned[key][1].unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise _UnlinkError(str(planned[key][1])) from exc
                evicted.append(key)
    except _UnlinkError:
        logger.warning("Verdrängen abgebrochen: eine lokale Kopie ließ sich nicht löschen", exc_info=True)
        run.result.skipped["fehler"] += 1
        ok = False
    for key in evicted:
        _count(run, [row["body_id"] for row in files[key]], planned[key][2])
    return ok


def _locked_files(run: _Run, keys: list[str], *, remote: bool) -> dict[str, list[dict[str, Any]]]:
    """
    Inhalte und ihre Dateien sperren (``SKIP LOCKED``, Inhalt vor Dateien wie ``file_store.attach``): Inhalt → Zeilen
    seiner Dateien, nur für Inhalte, deren Dateien alle gesperrt werden konnten. ``remote``: nur Inhalte, die (noch)
    im Objektspeicher liegen, sonst nur solche, die dort nicht liegen (Grund ``im_objektspeicher``).
    """
    from ..models import OParlFile, OParlFileBlob

    locked = {
        str(pk): remote_at
        for pk, remote_at in OParlFileBlob.objects.select_for_update(skip_locked=True)
        .filter(pk__in=keys)
        .values_list("pk", "remote_at")
    }
    _skip(run, "gesperrt", len(keys) - len(locked))
    files: dict[str, list[dict[str, Any]]] = {key: [] for key in locked}
    for row in (
        OParlFile.objects.select_for_update(skip_locked=True).filter(blob_id__in=list(locked)).values(*_FILE_FIELDS)
    ):
        files[str(row["blob_id"])].append(row)
    actual = {
        str(blob_id): count
        for blob_id, count in OParlFile.objects.filter(blob_id__in=list(locked))
        .values("blob_id")
        .annotate(n=Count("pk"))
        .values_list("blob_id", "n")
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for key, remote_at in locked.items():
        if remote and remote_at is None:
            # Inzwischen als nicht (mehr) hochgeladen vermerkt: die lokale Kopie bleibt
            _skip(run, "nicht_hochgeladen")
            continue
        if not remote and remote_at is not None:
            # Inzwischen (oder aus früherem Betrieb) im Objektspeicher: nie verwaisen lassen, sonst löschte ihn das
            # nächste Aufräumen mit eingeschaltetem Objektspeicher dort
            _skip(run, PROTECT_REMOTE)
            continue
        rows = files[key]
        count = actual.get(key, 0)
        if not count:
            # Kein Verweis mehr: das Aufräumen verwaister Inhalte löscht ihn
            _skip(run, "ohne_verweis")
            continue
        if len(rows) != count:
            # Eine Datei des Inhalts wird gerade geändert (gesperrte Zeile): beim nächsten Lauf
            _skip(run, "gesperrt")
            continue
        result[key] = rows
    return result


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


def _still_evictable(run: _Run, rows: list[dict[str, Any]], score: datetime, *, refetch: bool = True) -> str | None:
    """
    Prüfung unter Sperre: Hat sich seit der Planung etwas geändert? Grund, sonst ``None``. ``refetch``: Das Dokument
    müsste sich danach von der Quelle neu abrufen lassen (ohne Objektspeicher).
    """
    for row in rows:
        # Eben beansprucht (processing), wartend oder eingereiht: Die Texterkennung liest die lokale Kopie
        if row["text_extraction_status"] in BUSY_TEXT or str(row["pk"]) in run.queued:
            return "texterkennung"
        if row["local_status"] == FETCHING:
            return "abruf"
        cached = row["local_cached_at"]
        if cached is not None and cached > run.now - FRESH:
            return "frisch"
        if refetch and (row["body_id"] in run.unrefetchable or not (row["download_url"] or row["access_url"])):
            return "nicht_abrufbar"
        if score_of(row) > score:
            return "genutzt"
    return None


def _release_blobs(run: _Run, items: list[tuple[dict[str, Any], Path, int]]) -> bool:
    """Ohne Objektspeicher: Referenzen aller Dateien eines Inhalts freigeben (``evicted``), dann den Inhalt löschen."""
    from ..models import OParlFile, OParlFileBlob

    planned = {row["key"]: (row, path, size) for row, path, size in items}
    files: dict[str, list[dict[str, Any]]] = {}
    evict: list[str] = []
    try:
        with transaction.atomic():
            files = _locked_files(run, list(planned), remote=False)
            for key, rows in files.items():
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
            # Kein Dokument verweist mehr auf diese Inhalte, keiner liegt im Objektspeicher: Zeile und Datei gehen
            OParlFileBlob.objects.filter(pk__in=evict, remote_at__isnull=True).delete()
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
