"""
Sync Orchestrator

Coordinates the complete OParl synchronization process with:
- Full and incremental sync modes
- Parallel processing of entities
- Progress tracking and statistics
- Error handling and retry logic
- Redis event emission
- Prometheus metrics
"""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

from mandari_oparl import (
    ProcessedAgendaItem,
    ProcessedConsultation,
    ProcessedEntity,
    ProcessedFile,
    ProcessedLocation,
    ProcessedMeeting,
    ProcessedMembership,
    ProcessedOrganization,
    ProcessedPaper,
    ProcessedPerson,
)
from mandari_oparl.utils import parse_datetime
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from src.client.filter_notes import FilterNotes
from src.client.oparl_client import (
    ERROR_KIND_ROBOTS_BLOCKED,
    ERROR_KIND_SERVER_ERROR_SERIES,
    ERROR_KIND_UA_BLOCKED,
    MODIFIED_SINCE_INCONCLUSIVE,
    MODIFIED_SINCE_SUPPORTED,
    MODIFIED_SINCE_UNSUPPORTED,
    ListFetchError,
    ListMissingError,
    OParlClient,
    SyncStats,
)
from src.client.oparl_compat import (
    detect_oparl_version,
    is_oparl_error,
    oparl_10_fallback_urls,
    oparl_error_message,
    timestamps_unreliable,
)
from src.client.source_options import FullSyncWindow, SourceFetchOptions
from src.config import settings
from src.metrics import metrics
from src.redaction import MaskingConsole
from src.scrapers.base import CONTENT_HASH_FIELD
from src.storage.database import (
    DatabaseStorage,
    full_sync_progress_of,
    missing_lists_of,
    slice_filter_ignored_of,
    slow_lists_of,
    timed_lists_of,
)
from src.storage.events import start_correlation
from src.storage.ris_events import content_fingerprint
from src.sync.processor import OParlProcessor, session_location_id
from src.sync.slices import (
    FILTER_IGNORED_RECHECK,
    BodySlices,
    SliceRun,
    SliceWindow,
    SlowList,
    day_floor,
    full_windows,
    slow_backoff,
    slow_windows,
)

if TYPE_CHECKING:
    from src.scrapers.runner import ScraperSyncRunner

# Quellen-Schonung (Issue #89): ab BACKOFF_AFTER_FAILURES Fehlversuchen in Folge
# verdoppelt sich der Abstand bis zum nächsten Versuch (10, 20, 40 … Minuten,
# höchstens BACKOFF_MAX_MINUTES). Dauerfeuer gegen gesperrte oder ratenlimitierte
# Ratsinformationssysteme (Köln, Bonn) hält die Sperre sonst nur am Leben.
BACKOFF_AFTER_FAILURES = 3
BACKOFF_BASE_MINUTES = 10
BACKOFF_MAX_MINUTES = 360
# Sperre und 5xx-Serie (Issue #123) schonen die Quelle schon beim ersten Befund —
# Nachfassen im Zehn-Minuten-Takt ändert an einer User-Agent-Sperre nichts und
# hält einen überlasteten Server nur weiter unter Druck. Mindestabstand je Klasse;
# darüber hinaus verdoppelt sich der Abstand wie bei gewöhnlichen Fehlversuchen.
BACKOFF_MIN_MINUTES_BY_KIND = {
    ERROR_KIND_UA_BLOCKED: 60,
    ERROR_KIND_SERVER_ERROR_SERIES: 30,
    # robots.txt ändert sich selten: einmal täglich nachsehen genügt (Issue #116)
    ERROR_KIND_ROBOTS_BLOCKED: 24 * 60,
}
# Inkrementeller Abgleich: Suchindex nur für Objekte, die seit dem vorigen Abgleich des Bodies geschrieben
# wurden. Der Puffer fängt Uhrabweichungen zwischen Ingestor und Datenbank ab; doppelt indexiert schadet nicht.
INDEX_SINCE_MARGIN = timedelta(minutes=5)


def index_since(full: bool, previous_sync: datetime | None) -> datetime | None:
    """
    Ab wann ein Lauf Objekte in den Suchindex schreibt: ``None`` = alle (Vollabgleich oder Body ohne vorigen
    Abgleich), sonst der vorige Abgleich des Bodies abzüglich Puffer. Vorher schrieb jeder inkrementelle Lauf
    mit einer Änderung den ganzen Body neu.
    """
    if full or previous_sync is None:
        return None
    if previous_sync.tzinfo is None:
        previous_sync = previous_sync.replace(tzinfo=UTC)
    return previous_sync - INDEX_SINCE_MARGIN


#: Änderungsfenster ohne vollständigen Abgleich der Kommune: sieben Tage zurück, ab 00:00 Uhr (wie bisher)
MODIFIED_SINCE_FALLBACK_DAYS = 7


def fallback_modified_since(now: datetime | None = None) -> datetime:
    """
    Bisheriges Änderungsfenster: sieben Tage zurück ab 00:00 Uhr, als naive Ortszeit. Der Client sendet naive
    Zeitpunkte mit ``+00:00`` (``OParlClient._append_modified_since``), auf der Leitung beginnt es also um 00:00 UTC.
    """
    local_now = (now.astimezone() if now is not None else datetime.now()).replace(tzinfo=None)
    return (local_now - timedelta(days=MODIFIED_SINCE_FALLBACK_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)


def modified_since_for(sync_started: datetime | None, margin: timedelta, now: datetime | None = None) -> datetime:
    """
    ``modified_since`` eines inkrementellen Laufs (Issue #1025).

    Mit Zeitpunkt: Beginn des letzten vollständig erfolgreichen Abgleichs der Kommune abzüglich ``margin``, als
    UTC-Zeitpunkt (der Client sendet ihn wie bisher mit ``+00:00``). Ein Zeitpunkt in der Zukunft (falsche Uhr)
    zählt als jetzt. Ohne Zeitpunkt wie bisher sieben Tage zurück ab 00:00 Uhr, als naive Ortszeit.

    Nie weiter zurück als dieses bisherige Fenster: Bleibt eine Kommune lange unvollständig (Folgeseite bricht ab,
    Liste dauerhaft gestört, ein Objekt lässt sich nicht speichern), fragte sonst jeder Lauf bis zum letzten
    vollständigen Abgleich zurück, etwa vier Wochen, und damit mehr als vorher. Verglichen wird auf der Zeitbasis,
    mit der der Client das bisherige Fenster sendet (UTC).
    """
    fallback = fallback_modified_since(now)
    if sync_started is None:
        return fallback
    if sync_started.tzinfo is None:
        sync_started = sync_started.replace(tzinfo=UTC)
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    window = min(sync_started.astimezone(UTC), now_utc) - margin
    if window <= fallback.replace(tzinfo=UTC):
        return fallback
    return window


#: Ortszeit der Quellen: Tagesbeginn für Tagesstempel und für Listen ohne nachgewiesene Uhrzeit (Issue #1025)
SOURCE_TZ = ZoneInfo("Europe/Berlin")


def day_start_window(modified_since: datetime, now: datetime | None = None) -> datetime:
    """
    Fensteranfang für Listen ohne nachgewiesene Uhrzeit (``TimedLists``, Issue #1025): ``modified_since`` auf den
    Tagesbeginn abgerundet, und zwar auf den früheren von 00:00 Uhr Ortszeit und 00:00 Uhr UTC.

    Trägt ``modified`` nur das Datum, ob als Ortszeit oder als UTC gezählt, liegt der Stempel jeder Änderung ab
    ``modified_since`` damit im Fenster; das kurze Fenster fände ab etwa 02:00 Uhr nichts mehr vom selben Tag.
    Nie weiter zurück als das bisherige Fenster (sieben Tage). Das bisherige Fenster (naiv) bleibt, wie es ist.
    """
    if modified_since.tzinfo is None:
        return modified_since
    utc = modified_since.astimezone(UTC)
    utc_midnight = utc.replace(hour=0, minute=0, second=0, microsecond=0)
    local_midnight = utc.astimezone(SOURCE_TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    window = min(utc_midnight, local_midnight)
    fallback = fallback_modified_since(now)
    if window <= fallback.replace(tzinfo=UTC):
        return fallback
    return window


#: Listen am Body, die OParl 1.1 als optional führt (Typnamen wie in ``_sync_entity_type``). Nur sie werden bei
#: HTTP 404 oder Fehlerobjekt als „gibt es nicht“ vermerkt. Antwortet eine Pflichtliste (organization, person,
#: meeting, paper) mit 404, ist das eine Lücke, etwa ein Endpunkt, der während eines Updates der Quelle kurz 404
#: liefert; ein Fehlerobjekt gilt dort wie bisher als leere Liste (``BodyListCheck``, Issue #1025).
OPTIONAL_LIST_TYPES = frozenset({"membership", "agendaitem", "consultation", "file", "location", "legislativeterm"})
#: Vermerkte Listen fragt auch der inkrementelle Lauf wieder an, wenn ihre letzte Prüfung so lange zurückliegt
MISSING_LIST_RECHECK = timedelta(days=1)


class RequiredListMissingError(Exception):
    """Pflichtliste antwortet schon auf der ersten Seite mit HTTP 404: Lücke, kein Vermerk."""

    def __init__(self, list_url: str, reason: str) -> None:
        self.list_url = list_url
        self.reason = reason
        super().__init__(f"Liste {list_url} nicht abrufbar ({reason}); Pflichtliste, bleibt eine Lücke")


class _SliceFallbackError(Exception):
    """
    Inkrementelles Nachholen einer langsamen Liste: Die Tagesscheiben gehen bei dieser Quelle nicht (Filter abgelehnt,
    ``modified_until`` ignoriert, Liste mit Filter nicht gefunden). Die Liste kommt dann wie bisher ohne Zeitscheiben
    ab ihrem Fensteranfang, ohne Befund an der Quelle (Issue #1033).
    """


#: Antworten auf eine Zeitscheibe, nach denen das inkrementelle Nachholen ohne Zeitscheiben weiterliest
_SLICE_FALLBACK_STATUS = frozenset({400, 401, 403, 404})


def _filter_rejected(error: ListFetchError) -> bool:
    """Lehnt die Quelle den Filter einer Zeitscheibe ab (HTTP 400, 401, 403, 404, keine Sperre)?"""
    result = error.result
    return result.status_code in _SLICE_FALLBACK_STATUS and result.error_kind != ERROR_KIND_UA_BLOCKED


def full_sync_finished(body_results: Iterable[Any]) -> bool:
    """
    Hat der Vollabgleich jede Liste ganz gelesen? Eine im Vollabgleich ausgesetzte langsame Liste (``full_pending``)
    ist keine Lücke des Laufs, der Vollabgleich bleibt aber fällig: ``last_successful_full_sync`` rückt nicht vor,
    der Durchgang in Zeitscheiben ist nicht fertig (Issue #1033).
    """
    return all(not body_result.get("full_pending") for body_result in body_results)


class SliceFilterIgnoredError(Exception):
    """
    Die Quelle beachtet den Filter einer Zeitscheibe nicht (Issue #1033): Der Vollabgleich in Zeitscheiben bricht ab,
    statt dieselben Objekte in jeder Scheibe erneut abzurufen. Die Liste bleibt eine Lücke.
    """

    def __init__(self, list_url: str, detail: str) -> None:
        self.list_url = list_url
        self.detail = detail
        super().__init__(f"Vollabgleich in Zeitscheiben abgebrochen bei {list_url}: {detail}")


@dataclass
class MissingLists:
    """
    Optionale Listen einer Quelle, die es bei ihr nicht gibt (HTTP 404 oder Fehlerobjekt auf der ersten Seite,
    Issue #1025), mit dem Zeitpunkt ihrer letzten Prüfung.

    Gelesen aus ``sync_config["missing_lists"]`` beim Start des Abgleichs einer Quelle. Inkrementelle Läufe fragen
    eine vermerkte Liste erst wieder an, wenn ihre letzte Prüfung einen Tag zurückliegt (``MISSING_LIST_RECHECK``),
    der Vollabgleich jedes Mal. Gibt es die Liste wieder, wird der Vermerk gelöscht. So bleibt eine Liste nach einer
    kurzen Störung höchstens einen Tag ausgeblendet, auch wenn der nächste Vollabgleich Wochen entfernt ist oder sein
    Zeitfenster endet, bevor er die Liste erreicht.
    """

    source_url: str
    known: dict[str, datetime | None] = field(default_factory=dict)

    def skip(self, list_url: str, now: datetime | None = None) -> bool:
        """Vermerkt und vor weniger als einem Tag geprüft: im inkrementellen Lauf nicht anfragen."""
        if list_url not in self.known:
            return False
        checked = self.known[list_url]
        if checked is None:
            return False
        age = (now or datetime.now(UTC)) - checked
        return timedelta(0) <= age < MISSING_LIST_RECHECK


@dataclass
class BodyListCheck:
    """
    Befund zu fehlenden Listen einer Kommune in einem Lauf (Issue #1025).

    Vermerkt wird erst am Ende des Laufs und nur, wenn mindestens eine andere Liste der Kommune lesbar war.
    Antworten alle Listen mit 404, ist eher die Schnittstelle gestört; dann bleibt es eine Lücke, und kein
    Vermerk sperrt die Listen bis zum nächsten Vollabgleich.
    """

    lists: MissingLists
    #: Listen dieses Laufs, die es nicht gibt (Adresse -> Grund)
    missing: dict[str, str] = field(default_factory=dict)
    #: Pflichtlisten dieses Laufs, deren erste Seite ein OParl-Fehlerobjekt ist (Adresse -> Grund). Sie gelten wie
    #: bisher als leer, solange eine andere Liste der Kommune lesbar war (OParl 1.0, more! rubin: „Requested class
    #: doesn't exist.“); sonst als Lücke. HTTP 404 auf einer Pflichtliste bleibt immer eine Lücke.
    required_errors: dict[str, str] = field(default_factory=dict)
    #: Listen dieses Laufs, deren erste Seite lesbar war
    readable: int = 0


@dataclass
class TimedLists:
    """
    Listen einer Quelle, deren ``modified`` nachweislich die Uhrzeit trägt (``sync_config["timed_lists"]``,
    Issue #1025). Nur sie fragt der inkrementelle Lauf mit dem kurzen Fenster ab „Beginn minus Abstand“ an.

    Alle übrigen Listen bekommen dasselbe Fenster auf den Tagesbeginn abgerundet (``day_start_window``): Listen mit
    reinen Tagesstempeln, Listen mit gemischten Stempeln und Listen, in denen noch kein Lauf eine Änderung gesehen
    hat (etwa nach einer ruhigen Woche). Filtert die Quelle gegen Tagesstempel, fände das kurze Fenster ab etwa
    02:00 Uhr nichts mehr vom selben Tag. Vermerkt wird eine Liste, sobald ein Lauf dort Änderungen der letzten
    sieben Tage sieht und alle eine Uhrzeit tragen, gelöscht, sobald er dort eine solche Änderung nur mit Datum
    sieht (auch 00:00 Uhr Ortszeit in UTC, etwa ``22:00:00Z``). Sieht ein Lauf dort nichts, bleibt es, wie es ist.
    """

    source_url: str
    known: set[str] = field(default_factory=set)


def _stamp(value: Any) -> datetime | None:
    """Zeitpunkt aus ``modified``, sonst ``None``."""
    return parse_datetime(value) if isinstance(value, str) else None


def _as_utc(stamp: datetime) -> datetime:
    """Zeitpunkt in UTC; ohne Zone als UTC gezählt (so sendet der Client naive Zeitpunkte)."""
    return (stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)).astimezone(UTC)


def _midnight(stamp: datetime) -> bool:
    return stamp.hour == 0 and stamp.minute == 0 and stamp.second == 0 and stamp.microsecond == 0


def _day_stamp(stamp: datetime) -> bool:
    """
    Trägt ``modified`` nur das Datum? 00:00:00 Uhr in der angegebenen Zone, ohne Uhrzeit, oder 00:00 Uhr Ortszeit
    in UTC angegeben (``2026-10-14T22:00:00Z``).
    """
    return _midnight(stamp) or (stamp.tzinfo is not None and _midnight(stamp.astimezone(SOURCE_TZ)))


#: Nicht gespeicherte Objekte: so viele Kennungen je Typ nennt das Log
UNSTORED_IDS_LOGGED = 5
#: Ein nicht gespeichertes Objekt, dessen ``modified`` so weit vor dem Fenster der Liste liegt, liefert die Quelle
#: ohne Filter. Der Abstand fängt falsch angegebene Zeitzonen ab (höchstens 14 Stunden).
UNSTORED_BEFORE_WINDOW = timedelta(days=1)


@dataclass
class BodyItemCheck:
    """
    Befund zu den Objekten einer Kommune in einem Lauf (Issue #1025).

    Nicht gespeicherte Objekte: Fehler beim Verarbeiten oder Speichern (etwa Deadlock oder Constraint), eine
    Mitgliedschaft, deren Person oder Gremium sich nicht auflösen lässt, ein Tagesordnungspunkt ohne Sitzung. Früher
    holte jeder Lauf sie sieben Tage lang erneut. Jetzt rückt das Änderungsfenster höchstens bis zu ihrem ältesten
    ``modified`` vor (ohne lesbares ``modified`` gar nicht), der nächste Lauf fragt sie also wieder ab. Als Lücke der
    Quelle (``sync_complete``) zählen sie nicht: Sonst wiederholte eine dauerhaft fehlerhafte Mitgliedschaft den
    Vollabgleich jede Nacht statt im Abstand der Quelle (``full_sync_every_days``).

    Ausgenommen sind Objekte, die die Quelle trotz Filter liefert, deren ``modified`` also mehr als einen Tag vor dem
    Fenster der Liste liegt (Filter nicht unterstützt, Liste ohne Filter wie die Mitgliedschaften der
    Bezirksregierung Köln). Sie kommen in jedem Lauf wieder; dauerhaft nicht speicherbare unter ihnen (Mitgliedschaft
    mit Person einer anderen Quelle) hielten das Fenster sonst für immer bei sieben Tagen.

    Dazu je Liste, wie genau ``modified`` der Änderungen der letzten sieben Tage ist (``TimedLists``).
    """

    #: Nur ``modified`` ab hier zählt für die Genauigkeit (UTC); ältere Stempel, etwa übernommene Altdaten mit
    #: 00:00 Uhr, sagen über heutige Änderungen nichts. ``None``: alle.
    recent_since: datetime | None = None
    #: Nicht gespeicherte Objekte je Typ
    unstored: Counter[str] = field(default_factory=Counter)
    #: Kennungen nicht gespeicherter Objekte je Typ, höchstens ``UNSTORED_IDS_LOGGED``
    unstored_ids: dict[str, list[str]] = field(default_factory=dict)
    #: Davon trotz Filter geliefert (``modified`` vor dem Fenster der Liste); sie halten das Fenster nicht
    unstored_before_window: int = 0
    #: Ältestes ``modified`` der nicht gespeicherten Objekte, die das Fenster halten (UTC)
    oldest_unstored: datetime | None = None
    #: Ein nicht gespeichertes Objekt hatte kein lesbares ``modified``
    unstored_without_stamp: bool = False
    #: ``modified`` je Liste (Adresse): [nur Datum, mit Uhrzeit]
    stamps: dict[str, list[int]] = field(default_factory=dict)

    def note_stamp(self, list_url: str, item: dict[str, Any]) -> None:
        stamp = _stamp(item.get("modified"))
        if stamp is None:
            return
        if self.recent_since is not None and _as_utc(stamp) < self.recent_since:
            return
        counts = self.stamps.setdefault(list_url, [0, 0])
        counts[0 if _day_stamp(stamp) else 1] += 1

    def note_unstored(self, entity_type: str, item: Any, window: datetime | None = None) -> None:
        """
        Nicht gespeichertes Objekt festhalten. ``window``: Fensteranfang, mit dem der Lauf die Liste angefragt hat
        (``None`` im Vollabgleich). Ein Element, das kein Objekt ist, zählt wie eines ohne ``modified``.
        """
        self.unstored[entity_type] += 1
        record = item if isinstance(item, dict) else {}
        ids = self.unstored_ids.setdefault(entity_type, [])
        if len(ids) < UNSTORED_IDS_LOGGED:
            external_id = record.get("id")
            ids.append(external_id if isinstance(external_id, str) and external_id else "ohne Kennung")
        stamp = _stamp(record.get("modified"))
        if stamp is None:
            self.unstored_without_stamp = True
            return
        stamp = _as_utc(stamp)
        if window is not None and stamp < _as_utc(window) - UNSTORED_BEFORE_WINDOW:
            self.unstored_before_window += 1
            return
        if self.oldest_unstored is None or stamp < self.oldest_unstored:
            self.oldest_unstored = stamp

    def day_stamped_lists(self) -> list[str]:
        """Listen mit mindestens einer gelesenen Änderung, deren ``modified`` nur das Datum trägt."""
        return sorted(url for url, (day, _timed) in self.stamps.items() if day)

    def timed_lists(self) -> list[str]:
        """Listen, deren gelesene Änderungen alle eine Uhrzeit tragen."""
        return sorted(url for url, (day, timed) in self.stamps.items() if timed and not day)

    def window_start(self, started: datetime) -> datetime | None:
        """
        Neuer Anfang des Änderungsfensters nach einem Lauf, dessen Listen ganz gelesen wurden: Beginn des Laufs,
        bei nicht gespeicherten Objekten höchstens ihr ältestes ``modified``; ``None`` = stehen lassen.
        """
        if self.unstored_without_stamp:
            return None
        if self.oldest_unstored is None:
            return started
        return min(started, self.oldest_unstored)


# Anzeigetexte der Fehlerklassen (Schonungsmeldung in sync_all, Status in list-sources)
ERROR_KIND_LABELS = {
    ERROR_KIND_UA_BLOCKED: "User-Agent gesperrt",
    ERROR_KIND_SERVER_ERROR_SERIES: "5xx-Serie",
    ERROR_KIND_ROBOTS_BLOCKED: "robots.txt sperrt",
}


def source_status_label(source: object, now: datetime | None = None) -> str:
    """Kurzer Betriebsstatus einer Quelle: inaktiv, Schonung bis …, Fehler oder aktiv."""
    if not getattr(source, "is_active", True):
        return "inaktiv"
    failures = getattr(source, "consecutive_failures", 0) or 0
    attempts = f"{failures} Fehlversuch in Folge" if failures == 1 else f"{failures} Fehlversuche in Folge"
    reason = ERROR_KIND_LABELS.get(getattr(source, "last_error_kind", None) or "", attempts)
    until = source_backoff_until(source, now)
    if until is not None:
        return f"Schonung bis {until:%d.%m. %H:%M} UTC ({reason})"
    if failures:
        return f"Fehler ({reason})"
    return "aktiv"


def source_backoff_until(source, now: datetime | None = None) -> datetime | None:
    """Zeitpunkt, bis zu dem eine mehrfach gescheiterte Quelle in Ruhe gelassen wird (sonst None)."""
    failures = getattr(source, "consecutive_failures", 0) or 0
    last_error_at = getattr(source, "last_error_at", None)
    kind_minimum = BACKOFF_MIN_MINUTES_BY_KIND.get(getattr(source, "last_error_kind", None) or "", 0)
    if kind_minimum:
        failures = max(failures, BACKOFF_AFTER_FAILURES)
    if failures < BACKOFF_AFTER_FAILURES or last_error_at is None:
        return None
    if last_error_at.tzinfo is None:
        last_error_at = last_error_at.replace(tzinfo=UTC)
    minutes = min(BACKOFF_BASE_MINUTES * 2 ** (failures - BACKOFF_AFTER_FAILURES), BACKOFF_MAX_MINUTES)
    minutes = max(minutes, kind_minimum)
    until = last_error_at + timedelta(minutes=minutes)
    now = now or datetime.now(UTC)
    return until if until > now else None


console = MaskingConsole()


@dataclass
class SyncResult:
    """Result of a sync operation."""

    source_url: str
    source_name: str
    success: bool
    bodies_synced: int = 0
    meetings_synced: int = 0
    papers_synced: int = 0
    persons_synced: int = 0
    organizations_synced: int = 0
    memberships_synced: int = 0
    files_synced: int = 0
    locations_synced: int = 0
    agenda_items_synced: int = 0
    consultations_synced: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    http_stats: SyncStats | None = None
    # Sperr-/Störungsbefund des Laufs (Issue #123): Fehlerklasse und Statistik je Host
    error_kind: str | None = None
    host_findings: list[dict[str, Any]] = field(default_factory=list)
    # Gelesen, aber nicht gespeichert, je Typ über alle Kommunen (Issue #1025), im Sync-Log unter ``unstored``
    unstored: Counter[str] = field(default_factory=Counter)
    # Lief der Abgleich als Vollabgleich? Außerhalb des Zeitfensters der Quelle wird aus einem angeforderten
    # Vollabgleich ein inkrementeller (``_full_sync_plan``); der Daemon protokolliert, was tatsächlich lief
    full: bool = False
    # Zeitscheiben und ausgesetzte Listen über alle Kommunen (Issue #1033), im Sync-Log unter ``full_sync_slices``
    # und ``suspended_lists`` (``merge_slice_details``)
    slice_details: dict[str, Any] = field(default_factory=dict)


def merge_slice_details(target: dict[str, Any], body_result: dict[str, Any]) -> None:
    """Befund einer Kommune zu Zeitscheiben und ausgesetzten Listen in den Befund der Quelle übernehmen."""
    suspended = body_result.get("suspended_lists")
    if isinstance(suspended, dict) and suspended:
        target.setdefault("suspended_lists", {}).update(suspended)
    slices = body_result.get("full_sync_slices")
    if not isinstance(slices, dict):
        return
    merged = target.setdefault("full_sync_slices", {"lists": {}, "without_modified": {}, "note": slices.get("note")})
    merged["lists"].update(slices.get("lists") or {})
    for name, count in (slices.get("without_modified") or {}).items():
        merged["without_modified"][name] = merged["without_modified"].get(name, 0) + count


def sync_complete(body_results: list[dict[str, Any]], missed_bodies: list[str], client: OParlClient) -> bool:
    """
    Ist der Abgleich einer Quelle vollständig erfolgreich (Issue #556)?

    Ja, wenn jede Kommune lesbar war, jede ihrer Listen ganz gelesen wurde (keine abgebrochene
    Paginierung, keine Teilantwort) und kein Host gesperrt oder gestört war. Nur dann gilt die Quelle
    als aktuell (``last_successful_sync``); Text-Extraktion und Suchindex zählen nicht dazu.
    """
    if missed_bodies or client.host_findings():
        return False
    return all(not body_result.get("incomplete") for body_result in body_results)


#: Höchstens so viele nicht lesbare Adressen nennt der Eintrag im Sync-Log (``missed_bodies_note``)
MISSED_BODIES_SHOWN = 5

#: Höchstens so viele Seiten einer Kommunenliste liest die Erkennung, mit der ersten (Issue #869); derselbe Wert wie
#: ``MAX_BODY_PAGES`` in ``mandari/insight_core/management/commands/add_oparl_source.py``
MAX_BODY_PAGES = 20


def missed_bodies_note(missed_bodies: list[str]) -> str:
    """
    Eintrag im Sync-Log, wenn Kommunen nicht lesbar waren (Issue #869): Seiten der Kommunenliste oder einzelne
    Kommunen. Wie bei den übrigen Listen steht die Lücke unter den Fehlern des Laufs; der Abgleich ist nicht
    vollständig (``sync_complete``).
    """
    shown = ", ".join(missed_bodies[:MISSED_BODIES_SHOWN])
    more = len(missed_bodies) - MISSED_BODIES_SHOWN
    suffix = f" und {more} weitere" if more > 0 else ""
    return f"Kommunen nicht vollständig gelesen, nicht lesbar: {shown}{suffix}"


def unique_bodies(bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Jede Kommune nur einmal, nach ``id`` (die erste gewinnt; Issue #869): Steht sie auf zwei Seiten einer
    Kommunenliste (Seiten verschieben sich zwischen den Anfragen, Liste im Kreis) oder unter zwei Adressen, gliche
    ``sync_body_url`` sie sonst zweimal parallel ab, mit allen Listen doppelt. Einträge ohne ``id`` bleiben stehen.
    """
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for body in bodies:
        body_id = body.get("id") if isinstance(body, dict) else None
        if isinstance(body_id, str):
            if body_id in seen:
                continue
            seen.add(body_id)
        unique.append(body)
    return unique


class SyncOrchestrator:
    """
    Orchestrates the OParl synchronization process.

    Features:
    - Full sync: Fetches all data from scratch
    - Incremental sync: Only fetches changed data since last sync
    - Parallel processing: Fetches multiple entity types concurrently
    - Progress tracking: Real-time progress display
    - Error recovery: Continues sync even if some entities fail
    - Redis event emission for real-time updates
    - Prometheus metrics for monitoring
    """

    def __init__(
        self,
        database_url: str | None = None,
        max_concurrent: int | None = None,
    ) -> None:
        """
        Initialize the orchestrator.

        Args:
            database_url: Database connection URL
            max_concurrent: Maximum concurrent HTTP requests (default from config)
        """
        from src.config import settings

        self.storage = DatabaseStorage(database_url)
        # Prozessor (Kennungen der Objekte) und Speicher (Verweise in Ereignissen) rechnen mit denselben
        # festgeschriebenen Basen umgezogener Quellen (Issue #733, ``_register_id_base``)
        self.processor = OParlProcessor(self.storage.id_bases)
        self.max_concurrent = max_concurrent or settings.oparl_max_concurrent
        # Track if we're in parallel mode (disables Rich Progress to avoid conflicts)
        self._parallel_mode = False

    async def __aenter__(self) -> "SyncOrchestrator":
        """Async context manager entry."""
        await self.storage.initialize()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.storage.close()

    # ========== Source Management ==========

    async def add_source(self, url: str, name: str | None = None) -> UUID:
        """
        Register a new OParl source.

        Fetches the system endpoint to get source metadata.

        Args:
            url: The OParl API URL (system endpoint)
            name: Optional override for source name

        Returns:
            The source UUID
        """
        console.print(f"[blue]Fetching system info from {url}...[/blue]")

        async with OParlClient(max_concurrent=1) as client:
            system_data = await client.fetch_system(url)
            if is_oparl_error(system_data):
                # 1.1-Pfad auf einem OParl-1.0-Server (Issue #122)
                system_data, _resolved = await self._try_oparl_10_fallback(
                    client, url, oparl_error_message(system_data)
                )

        if not system_data:
            raise ValueError(f"Could not fetch OParl system from {url}")

        source_name = name or system_data.get("name", "Unknown Source")

        source_id = await self.storage.upsert_source(
            url=url,
            name=source_name,
            raw_json=system_data,
            oparl_version=detect_oparl_version(system_data),
        )

        console.print(f"[green]Registered source: {source_name} (ID: {source_id})[/green]")
        return source_id

    def _scraper_runner(self, source_row: Any) -> "ScraperSyncRunner | None":
        """
        ``ScraperSyncRunner`` für eine Scraper-Quelle (``sync_config["source_type"] = "scraper:<vendor>"``), sonst None.

        Scraper-Quellen laufen über den Runner statt über den OParl-Client; Zeitfenster und Mindestabstand des
        Vollabgleichs (``_full_sync_plan``) gelten für sie nur im geplanten Vollabgleich des Daemons. Zeitscheiben,
        langsame Listen (``_slice_run``) und Vermerke „filtert nicht“ (``_filter_notes``) gibt es bei ihnen nicht.
        "bridge:*"-Quellen (z. B. oparl-bridge vor ALLRIS) sind normale OParl-Quellen und nehmen den Standardpfad.
        """
        if source_row is None or not isinstance(getattr(source_row, "sync_config", None), dict):
            return None
        source_type = str(source_row.sync_config.get("source_type") or "oparl")
        if not source_type.startswith("scraper:"):
            return None
        from src.scrapers.runner import ScraperSyncRunner

        return ScraperSyncRunner(self, source_row)

    def _register_id_base(self, url: str, source_row: Any) -> None:
        """
        Festgeschriebene Basis der Kennungen einer Quelle übernehmen (``sync_config["id_base"]``, Issue #733).

        Ist eine Quelle umgezogen (eigene Installation auf neuer Domain), behalten ihre Objekte die Kennungen
        ihrer bisherigen Adressen: Für Adressen unter ``url`` rechnet der Ingestor auf der festgeschriebenen
        Basis (``mandari_oparl.ids.canonical_uri``). Ohne Eintrag sind die Adressen kanonisch. Hat sich beim
        Umzug auch die Form der Adressen geändert, gelten ``id_address`` und die Abbildungsregeln
        ``id_rules`` der Quelle (``IdBases.add_source``).

        Raises:
            ValueError: Die Abbildungsregeln der Quelle sind ungültig; der Abgleich der Quelle unterbleibt,
                statt Objekte mit falschen Kennungen anzulegen.
        """
        if self.storage.id_bases.add_source(url, getattr(source_row, "sync_config", None)):
            self.processor.clear_id_cache()

    # ========== URL Auto-Detection ==========

    async def auto_detect_url(
        self,
        client: OParlClient,
        url: str,
        missed: list[str] | None = None,
    ) -> tuple[str, list[dict[str, Any]], str | None]:
        """
        Detect whether a URL points to a Body, Body-List, or System.

        Handles all known OParl server variants:
        - Single Body object (ITK Rheinland: /Oparl/bodies/0015)
        - Body list with data[] wrapper (Köln, Münster, Bonn, Aachen)
        - System object with body reference (string, list of URLs or
          embedded Body objects)
        - OParl 1.0 (more! rubin): 1.1-Pfade antworten mit einem
          Fehlerobjekt ("Requested class doesn't exist") oder 404 — dann
          fällt die Erkennung auf die 1.0-Pfadvarianten zurück (Issue #122)

        Args:
            client: OParl HTTP client
            url: The URL to detect
            missed: nimmt Adressen auf, unter denen Kommunen nicht lesbar waren (die Seite der Kommunenliste,
                an der das Lesen abbrach, einzelne Kommunen); der Abgleich ist dann nicht vollständig (Issue #556).
                Weitere Seiten einer Kommunenliste (``links.next``) liest die Erkennung mit (Issue #869).

        Returns:
            Tuple of (type_name, list_of_body_dicts, oparl_version), wobei
            oparl_version "1.0"/"1.1" oder None (nicht erkennbar) ist.

        Raises:
            ValueError: If URL is not a valid OParl endpoint
        """
        result = await client.fetch(url, use_cache=False, skip_wait=True)
        response = result.data

        resolved = url
        if result.status_code == 404 or is_oparl_error(response):
            reason = result.error or oparl_error_message(response)
            response, resolved = await self._try_oparl_10_fallback(client, url, reason)
            if response is None:
                raise ValueError(f"Could not fetch OParl endpoint: {url} ({reason})")
        elif result.error or not response:
            raise ValueError(f"Could not fetch OParl endpoint: {url} ({result.error})")

        oparl_version = detect_oparl_version(response)

        # Case 1: Single Body (ITK Rheinland pattern - no data[] wrapper)
        type_str = response.get("type", "") if isinstance(response, dict) else ""
        if type_str.endswith("/Body"):
            console.print("[green]Detected: Single Body object[/green]")
            return "body", [response], oparl_version

        # Case 2: Body list with data[] wrapper (standard OParl)
        if isinstance(response, dict) and "data" in response:
            items = response["data"]
            if isinstance(items, list) and items:
                first_type = items[0].get("type", "") if isinstance(items[0], dict) else ""
                if first_type.endswith("/Body"):
                    console.print(f"[green]Detected: Body list ({len(items)} bodies)[/green]")
                    # Weitere Seiten der Kommunenliste ab links.next, ohne die erste noch einmal anzufragen
                    bodies = await self._fetch_body_list(client, resolved, missed, first_page=response)
                    return "body_list", bodies, oparl_version

        # Case 3: System object -> follow body reference
        if isinstance(response, dict) and type_str.endswith("/System"):
            body_ref = response.get("body")
            if body_ref:
                console.print(f"[green]Detected: System -> fetching bodies from {body_ref}[/green]")
                bodies = await self._fetch_bodies(client, body_ref, missed)
                return "system", bodies, oparl_version or detect_oparl_version(bodies)

        raise ValueError(f"URL is neither Body, Body-List, nor System: {url}\nResponse type: {type_str or 'unknown'}")

    async def _try_oparl_10_fallback(
        self,
        client: OParlClient,
        url: str,
        reason: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """
        Probiert die OParl-1.0-Pfadvarianten zu ``url`` (Issue #122).

        Liefert (Antwort, aufgelöste URL) des ersten Kandidaten, der ein
        echtes OParl-Objekt zurückgibt, sonst (None, url). Wird bewusst nur
        bei "Pfad existiert nicht"-Signalen (404, Fehlerobjekt) aufgerufen,
        nicht bei Timeouts oder 5xx — sonst würden wir einen kränkelnden
        Host mit weiteren Anfragen belasten.
        """
        for candidate in oparl_10_fallback_urls(url):
            console.print(f"[yellow]{url}: {reason} — versuche OParl-1.0-Pfad {candidate}[/yellow]")
            result = await client.fetch(candidate, use_cache=False, skip_wait=True)
            data = result.data
            if result.error or not isinstance(data, dict) or is_oparl_error(data):
                continue
            console.print(f"[green]OParl-1.0-Rückfall erfolgreich: {candidate}[/green]")
            return data, candidate
        return None, url

    async def _fetch_bodies(
        self, client: OParlClient, body_ref: Any, missed: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """
        Löst das ``body``-Feld eines System-Objekts in Body-Dicts auf.

        Varianten: Listen-URL (Standard, auch OParl 1.0 ``/oparl/Body``),
        Liste von Body-URLs oder bereits eingebettete Body-Objekte. Eine nicht lesbare
        Body-URL wird übersprungen und in ``missed`` vermerkt (Issue #556). Kommunenlisten
        werden über alle Seiten gelesen (``_fetch_body_list``, Issue #869).
        """
        if isinstance(body_ref, str):
            return await self._fetch_body_list(client, body_ref, missed)
        bodies: list[dict[str, Any]] = []
        if isinstance(body_ref, list):
            for entry in body_ref:
                if isinstance(entry, dict):
                    bodies.append(entry)
                elif isinstance(entry, str):
                    result = await client.fetch(entry, use_cache=False)
                    data = result.data
                    if isinstance(data, dict) and not is_oparl_error(data):
                        bodies.extend(await self._fetch_body_list(client, entry, missed, first_page=data))
                    elif missed is not None:
                        missed.append(entry)
        return unique_bodies(bodies)

    async def _fetch_body_list(
        self,
        client: OParlClient,
        list_url: str,
        missed: list[str] | None,
        first_page: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Alle Kommunen einer Kommunenliste, Seite für Seite über ``links.next`` (Issue #869).

        ``first_page``: Die erste Seite liegt schon vor (Erkennung der Adresse); dann beginnt der Abruf bei
        ihrer Folgeseite. Eine Antwort ohne ``links.next`` (auch eine einzelne Kommune) ist vollständig.

        Die Folgeseiten laufen wie jede Liste durch ``OParlClient.fetch_list``: im Takt des Hosts, und ein
        Fehler oder eine Teilantwort bricht die Liste mit ``ListFetchError`` ab. Waren bis dahin Kommunen
        lesbar, bleiben sie im Abgleich; die Seite, an der das Lesen abbrach, steht in ``missed`` und der
        Abgleich gilt als nicht vollständig (Issue #556). Ohne eine einzige lesbare Kommune (oder ohne
        ``missed``) geht der Fehler an den Aufrufer.

        Gelesen werden höchstens ``MAX_BODY_PAGES`` Seiten, und eine Folgeseite, die auf eine schon gelesene Seite
        verweist, bricht ab (``ListFetchError``), ebenso Folgeseiten, die immer dieselben Kommunen liefern (Issue
        #1088): Eine fehlerhafte Liste darf den Zyklus nicht endlos aufhalten.
        Nennt die letzte erlaubte Seite noch eine Folgeseite, steht diese in ``missed`` (unvollständig statt endlos).
        Eine Kommune, die auf mehreren Seiten steht, zählt einmal (``unique_bodies``).
        """
        bodies: list[dict[str, Any]] = []
        next_page: str | None = None
        if first_page is not None:
            bodies.extend(OParlClient._extract_items(first_page))
            links = first_page.get("links")
            next_page = str(links["next"]) if isinstance(links, dict) and links.get("next") else None
            if next_page is None:
                return unique_bodies(bodies)
        try:
            async for page in client.fetch_list(list_url, max_pages=MAX_BODY_PAGES, next_page=next_page):
                bodies.extend(page)
        except ListFetchError as e:
            if missed is None or not bodies:
                raise
            bodies = unique_bodies(bodies)
            console.print(
                f"[red]Kommunenliste {list_url} bricht ab ({e.result.error}), "
                f"{len(bodies)} Kommunen gelesen, Abgleich unvollständig: {e.page_url}[/red]"
            )
            missed.append(e.page_url)
            return bodies
        bodies = unique_bodies(bodies)
        unread = client.page_limit_lists.get(list_url)
        if unread is not None:
            console.print(
                f"[yellow]Kommunenliste {list_url} nach {MAX_BODY_PAGES} Seiten nicht weiter gelesen, "
                f"{len(bodies)} Kommunen gelesen, Abgleich unvollständig: {unread}[/yellow]"
            )
            if missed is not None:
                missed.append(unread)
        return bodies

    async def sync_body_url(
        self,
        url: str,
        full: bool = False,
        max_concurrent: int | None = None,
        default_window: FullSyncWindow | None = None,
    ) -> SyncResult:
        """
        Sync from any OParl URL (auto-detects Body, Body-List, or System).

        This is the primary entry point for the Body-First architecture.

        Args:
            url: OParl URL (Body, Body-List, or System endpoint)
            full: Whether to perform a full sync
            max_concurrent: Max concurrent HTTP requests
            default_window: Standardfenster des Daemons für Vollabgleiche; es begrenzt den Vollabgleich in
                Zeitscheiben einer Quelle ohne eigenes ``full_sync_window`` (Issue #1033)

        Returns:
            SyncResult with statistics
        """
        start_time = datetime.now(UTC)
        result = SyncResult(source_url=url, source_name="", success=False)
        source_row = await self.storage.get_source_by_url(url)
        self._register_id_base(url, source_row)
        runner = self._scraper_runner(source_row)
        if runner is not None:
            return await runner.run(full=full)

        # Vermerke „filtert nicht“ dieser Quelle (Issue #1041), alte Einträge je Server übernommen
        filter_notes = self._filter_notes(url, source_row)
        options = SourceFetchOptions.from_sync_config(getattr(source_row, "sync_config", None))
        full, fetch_deadline = self._full_sync_plan(url, options, full, source_row, default_window)
        full, slice_run, slice_note = await self._slice_run(
            url, options, full, getattr(source_row, "sync_config", None)
        )
        if slice_note is not None:
            fetch_deadline = None  # nur Änderungsabgleich: das Zeitfenster gilt nur für den Vollabgleich
            result.errors.append(slice_note)
        result.full = full

        client: OParlClient | None = None
        try:
            concurrent = max_concurrent or self.max_concurrent
            async with OParlClient(
                max_concurrent=concurrent,
                source_name=url.split("/")[2] if "/" in url else "unknown",
                user_agent=getattr(source_row, "user_agent", None),
                fetch_deadline=fetch_deadline,
                filter_notes=filter_notes,
                **options.client_kwargs(),
            ) as client:
                # Auto-detect URL type
                console.print(f"\n[bold blue]Connecting to {url}...[/bold blue]")
                missed_bodies: list[str] = []
                url_type, bodies_data, oparl_version = await self.auto_detect_url(client, url, missed_bodies)

                if not bodies_data:
                    result.errors.append(f"No bodies found at {url}")
                    await self._record_source_failure(
                        url, "Keine Kommunen (Bodies) am Endpunkt gefunden", client.error_kind
                    )
                    return result
                if missed_bodies:
                    result.errors.append(missed_bodies_note(missed_bodies))

                # Use first body name as source name
                result.source_name = bodies_data[0].get("name", "Unknown")
                console.print(f"[green]Source: {result.source_name} ({url_type}, OParl {oparl_version or '?'})[/green]")

                # Ensure source exists in database (use URL as source identifier)
                source_id = await self.storage.upsert_source(
                    url=url,
                    name=result.source_name,
                    raw_json=bodies_data[0] if len(bodies_data) == 1 else {"bodies_count": len(bodies_data)},
                    oparl_version=oparl_version,
                )

                # Sync bodies
                if len(bodies_data) > 1:
                    self._parallel_mode = True
                    console.print(f"[bold green]Starting PARALLEL sync of {len(bodies_data)} bodies...[/bold green]")

                # Bound per-body concurrency so multi-body sources don't
                # multiply peak memory (each body sync holds pages + caches).
                body_semaphore = asyncio.Semaphore(settings.sync_body_concurrency)
                source_config = getattr(source_row, "sync_config", None)
                missing_lists = MissingLists(url, missing_lists_of(source_config))
                timed_lists = TimedLists(url, timed_lists_of(source_config))

                async def sync_body_wrapper(body_data):
                    async with body_semaphore:
                        try:
                            return await self._sync_body(
                                client=client,
                                body_data=body_data,
                                source_id=source_id,
                                full=full,
                                options=options,
                                missing_lists=missing_lists,
                                timed_lists=timed_lists,
                                slices=slice_run,
                            )
                        except Exception as e:
                            console.print(f"[red]Error syncing {body_data.get('name', 'Unknown')}: {e}[/red]")
                            return {"errors": [str(e)], "incomplete": ["body"]}

                body_results = await asyncio.gather(
                    *[sync_body_wrapper(bd) for bd in bodies_data],
                    return_exceptions=False,
                )

                # Aggregate results
                for body_result in body_results:
                    result.bodies_synced += 1
                    result.meetings_synced += body_result.get("meetings", 0)
                    result.papers_synced += body_result.get("papers", 0)
                    result.persons_synced += body_result.get("persons", 0)
                    result.organizations_synced += body_result.get("organizations", 0)
                    result.memberships_synced += body_result.get("memberships", 0)
                    result.files_synced += body_result.get("files", 0)
                    result.locations_synced += body_result.get("locations", 0)
                    result.agenda_items_synced += body_result.get("agenda_items", 0)
                    result.consultations_synced += body_result.get("consultations", 0)
                    result.errors.extend(body_result.get("errors", []))
                    result.unstored.update(body_result.get("unstored") or {})
                    merge_slice_details(result.slice_details, body_result)

                # Update sync timestamp; vollständig nur ohne Lücke (Issue #556), der Vollabgleich nur ohne
                # ausgesetzte Liste (Issue #1033)
                complete = sync_complete(body_results, missed_bodies, client)
                await self.storage.update_source_sync_time(
                    source_id,
                    full_sync=full,
                    complete=complete,
                    full_complete=complete and full_sync_finished(body_results),
                )
                result.http_stats = client.stats
                result.success = True
                await self._apply_host_findings(result, client)
                if full:
                    await self._recheck_modified_since(client, url, bodies_data)

                total_synced = (
                    result.meetings_synced
                    + result.papers_synced
                    + result.persons_synced
                    + result.organizations_synced
                    + result.memberships_synced
                    + result.files_synced
                    + result.agenda_items_synced
                    + result.consultations_synced
                )

                # Sync log is written by the scheduler (one per cycle, not per source)

                metrics.record_entities_batch(result.source_name, total_synced)

        except Exception as e:
            result.errors.append(str(e))
            console.print(f"[red]Sync failed: {e}[/red]")
            await self._record_source_failure(url, str(e), client.error_kind if client else None)
            if client is not None:
                self._collect_host_findings(result, client)

        # Geänderte Vermerke „filtert nicht“ in der sync_config dieser Quelle speichern (Issue #1041)
        await self._save_filter_notes(url, filter_notes)

        result.duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
        return result

    # ========== Sperr- und Störungsbefund (Issue #123) ==========

    def _collect_host_findings(self, result: SyncResult, client: OParlClient) -> None:
        """Befunde des Clients (User-Agent gesperrt, 5xx-Serie) in das Laufergebnis übernehmen."""
        findings = client.host_findings()
        if not findings:
            return
        result.error_kind = findings[0].error_kind
        for health in findings:
            summary = health.describe()
            if summary and summary not in result.errors:
                result.errors.append(summary)
            result.host_findings.append(health.as_dict())

    async def _apply_host_findings(self, result: SyncResult, client: OParlClient) -> None:
        """
        Nach einem durchgelaufenen Sync: Befund als Sync-Warnung und Quellenstatus
        festhalten. Der Lauf bleibt „erfolgreich“ (andere Listen kamen durch), aber
        Fehlerklasse und Statistik landen an der Quelle, damit Schonung und
        Betriebsmonitor greifen und keine stille Lücke entsteht.
        """
        self._collect_host_findings(result, client)
        if result.error_kind is None:
            return
        summary = "; ".join(h.describe() for h in client.host_findings() if h.describe())
        console.print(f"[yellow]Sync-Warnung für {result.source_name or result.source_url}: {summary}[/yellow]")
        await self._record_source_failure(result.source_url, summary, result.error_kind)

    # ========== Sync Operations ==========

    @staticmethod
    def _filter_notes(url: str, source_row: Any) -> FilterNotes:
        """
        Vermerke „filtert nicht“ der Quelle aus ihrer ``sync_config`` (Issue #1041). Einträge im alten Schlüssel
        ``modified_since_unsupported_hosts`` werden übernommen: als Vermerk je Server nur, wenn die Quelle den Filter
        dort nachweislich ganz ignoriert (``FilterNotes.from_sync_config``); gespeichert am Ende des Laufs.
        """
        notes = FilterNotes.from_sync_config(
            getattr(source_row, "sync_config", None), url, getattr(source_row, "oparl_version", None)
        )
        if notes.legacy:
            kept = ", ".join(sorted(notes.changed_hosts)) or "keiner"
            console.print(
                f"[dim]{url}: alte Vermerke „filtert nicht“ je Server übernommen (bleibt: {kept}); Listen, die den "
                f"Filter ablehnen, vermerkt der Lauf je Liste[/dim]"
            )
        return notes

    async def _save_filter_notes(self, url: str, notes: FilterNotes) -> None:
        """
        Geänderte Vermerke „filtert nicht“ nur in der ``sync_config`` dieser Quelle speichern (Issue #1041). Fehler
        gefährden den Abgleich nie: Ein nicht gespeicherter Vermerk kostet im nächsten Lauf eine abgelehnte Anfrage.
        """
        if not notes.dirty:
            return
        try:
            await self.storage.save_filter_notes(
                url, dict(notes.changed_lists), dict(notes.changed_hosts), drop_legacy=notes.legacy
            )
        except Exception as e:  # noqa: BLE001 - der nächste Lauf erkennt die Vermerke neu
            console.print(f"[yellow]Vermerke „filtert nicht“ für {url} nicht gespeichert: {e}[/yellow]")
            return
        notes.saved()

    #: Zeitfenster der modified_since-Prüfung: die Quelle soll nur Objekte liefern, die seitdem geändert wurden
    MODIFIED_SINCE_PROBE_DAYS = 7
    #: Pause vor der Wiederholung einer Prüfung ohne Aussage
    MODIFIED_SINCE_RETRY_SECONDS = 60

    async def _recheck_modified_since(
        self, client: OParlClient, source_url: str, bodies_data: list[dict[str, Any]]
    ) -> str | None:
        """
        Nach einem Vollabgleich prüfen, ob die Quelle (wieder) mit ``modified_since`` filtert.

        Eine Anfrage an die erste Liste (Vorgänge, sonst Sitzungen) mit Filter. Filtert die Quelle, enden die Vermerke
        „filtert nicht“ für ihren Server und für diese Liste; lehnt sie den Filter ab, wird diese Liste vermerkt;
        liefert sie ungefiltert, der Server (die Quelle ignoriert den Filter dort ganz). Alles nur in den Vermerken
        dieser Quelle (``client.filter_notes``, Issue #1041), gespeichert am Ende des Laufs; das Ergebnis steht in
        ``sync_config["modified_since_check"]``. Ohne klare Aussage bleibt alles, wie es ist. Fehler gefährden den
        Abgleich nie.
        """
        if not settings.oparl_modified_since_enabled:
            return None
        list_url = next(
            (
                body.get(key)
                for body in bodies_data
                if isinstance(body, dict)
                for key in ("paper", "meeting")
                if isinstance(body.get(key), str) and body.get(key)
            ),
            None,
        )
        if not list_url:
            return None
        host = urlparse(list_url).netloc
        try:
            since = datetime.now(UTC) - timedelta(days=self.MODIFIED_SINCE_PROBE_DAYS)
            verdict = await client.probe_modified_since(list_url, since)
            if verdict == MODIFIED_SINCE_INCONCLUSIVE:
                # Einzelne Störung (langsame Antwort, 5xx) soll einen veralteten Befund nicht bis zum nächsten
                # Vollabgleich festschreiben: einmal nach kurzer Pause wiederholen
                await asyncio.sleep(self.MODIFIED_SINCE_RETRY_SECONDS)
                verdict = await client.probe_modified_since(list_url, since)
            if verdict == MODIFIED_SINCE_INCONCLUSIVE:
                console.print(f"[dim]  modified_since-Prüfung für {host}: keine Aussage[/dim]")
                return verdict
            notes = client.filter_notes
            if verdict == MODIFIED_SINCE_SUPPORTED:
                notes.clear_host(host)
                notes.clear_list(list_url)
            elif verdict == MODIFIED_SINCE_UNSUPPORTED:
                notes.note_list(list_url, "Prüfung nach Vollabgleich: Filter abgelehnt")
            else:
                notes.note_host(host, "Prüfung nach Vollabgleich: Filter ignoriert")
            await self.storage.set_modified_since_check(source_url, host, verdict)
            console.print(f"[dim]  modified_since-Prüfung für {host}: {verdict}[/dim]")
            return verdict
        except Exception as e:  # noqa: BLE001 - Prüfung ist eine Zugabe, der Abgleich ist durch
            console.print(f"[yellow]modified_since-Prüfung für {host} fehlgeschlagen: {e}[/yellow]")
            return None

    @staticmethod
    def _full_sync_plan(
        url: str,
        options: SourceFetchOptions,
        full: bool,
        source_row: Any = None,
        default_window: FullSyncWindow | None = None,
    ) -> tuple[bool, datetime | None]:
        """
        Vollabgleich nur im Abstand und Zeitfenster der Quelle (``full_sync_every_days``, ``full_sync_window``). Mit
        ``full_sync_slices`` endet er ohne eigenes Fenster mit dem Standardfenster des Daemons (``default_window``);
        ohne Fenster läuft er gar nicht, sondern inkrementell (Issue #1033).
        """
        planned_full, deadline = options.full_sync_plan(
            full,
            last_full_sync=getattr(source_row, "last_successful_full_sync", None),
            default_window=default_window,
        )
        if full and not planned_full and options.full_sync_slices is not None and options.sync_window() is None:
            if default_window is None:
                console.print(
                    f"[yellow]{url}: Vollabgleich in Zeitscheiben nur in einem Zeitfenster (full_sync_window oder "
                    f"Standardfenster des Daemons), nur inkrementell[/yellow]"
                )
            else:
                console.print(
                    f"[yellow]{url}: Vollabgleich in Zeitscheiben nicht fällig (Abstand/Standardfenster "
                    f"{default_window.start:%H:%M}–{default_window.end:%H:%M}), nur inkrementell[/yellow]"
                )
        elif full and not planned_full:
            console.print(
                f"[yellow]{url}: Vollabgleich nicht fällig (Abstand/Zeitfenster der Quelle), nur inkrementell[/yellow]"
            )
        elif deadline is not None:
            console.print(f"[dim]{url}: Vollabgleich bis {deadline:%H:%M} UTC (Zeitfenster der Quelle)[/dim]")
        return planned_full, deadline

    async def _slice_run(
        self, url: str, options: SourceFetchOptions, full: bool, sync_config: Any
    ) -> tuple[bool, SliceRun, str | None]:
        """
        Zeitscheiben und langsame Listen der Quelle für diesen Lauf (Issue #1033). Liefert ``(full, Zustand, Meldung)``.

        Ein Vollabgleich mit ``full_sync_slices`` läuft in Zeitscheiben. Hat ein früherer Lauf festgestellt, dass die
        Quelle den Filter einer Scheibe ignoriert (``full_sync_filter_ignored``), läuft er stattdessen inkrementell,
        mit Meldung, bis der Befund ``FILTER_IGNORED_RECHECK`` alt ist; dann wird er gelöscht und neu geprüft. Ohne
        Zeitscheiben bleibt der Vollabgleich, wie er ist.
        """
        config = options.full_sync_slices if full else None
        note: str | None = None
        if config is not None:
            finding = slice_filter_ignored_of(sync_config)
            checked = _stamp(finding.get("checked_at")) if finding is not None else None
            if checked is not None and checked.tzinfo is None:
                checked = checked.replace(tzinfo=UTC)
            if finding is not None and checked is not None and datetime.now(UTC) - checked < FILTER_IGNORED_RECHECK:
                retry = checked + FILTER_IGNORED_RECHECK
                detail = finding.get("detail") or "Quelle ignoriert den Filter"
                note = (
                    f"Vollabgleich in Zeitscheiben ausgesetzt: {detail} (festgestellt am {checked:%d.%m.%Y}); "
                    f"nur Änderungsabgleich, neuer Versuch ab {retry:%d.%m.%Y}"
                )
                console.print(f"[yellow]{url}: {note}[/yellow]")
                full, config = False, None
            elif finding is not None:
                try:
                    await self.storage.set_slice_filter_ignored(url, None)
                except Exception as e:  # noqa: BLE001 - dann gilt der Befund im nächsten Lauf als alt
                    console.print(f"[yellow]Befund zu Zeitscheiben nicht gelöscht: {e}[/yellow]")
        run = SliceRun.build(url, config, full_sync_progress_of(sync_config), slow_lists_of(sync_config))
        if config is not None:
            console.print(
                f"[dim]{url}: Vollabgleich in Zeitscheiben zu {config.days} Tagen ab {config.since:%Y-%m-%d}[/dim]"
            )
        return full, run, note

    async def _record_source_failure(self, url: str, error: str, error_kind: str | None = None) -> None:
        """Fehlerstatus für den Betriebsmonitor speichern — darf den Sync nie gefährden."""
        try:
            await self.storage.record_source_failure(url, error, error_kind)
        except Exception as exc:
            console.print(f"[yellow]Warning: Quellen-Fehlerstatus nicht gespeichert: {exc}[/yellow]")

    async def sync_source(
        self,
        url: str,
        full: bool = False,
        body_filter: str | None = None,
        default_window: FullSyncWindow | None = None,
    ) -> SyncResult:
        """
        Synchronize a single OParl source.

        Args:
            url: The OParl API URL
            full: Whether to perform a full sync (ignores last_sync)
            body_filter: Optional body name/ID filter
            default_window: Standardfenster für Vollabgleiche (siehe ``sync_body_url``)

        Returns:
            SyncResult with statistics
        """
        start_time = datetime.now(UTC)
        result = SyncResult(source_url=url, source_name="", success=False)
        source_row = await self.storage.get_source_by_url(url)
        self._register_id_base(url, source_row)
        # Auch hier über den Scraper: Worker der Anwendung (``run_sync_with_logging``) und ``sync --source`` kommen
        # über diese Methode; ohne den Zweig riefen sie die Basis-URL eines SessionNet als OParl-System ab.
        runner = self._scraper_runner(source_row)
        if runner is not None:
            if body_filter:
                console.print(
                    "[yellow]Der Filter nach Kommune gilt nicht für Scraper-Quellen; abgeglichen wird die ganze "
                    "Quelle.[/yellow]"
                )
            return await runner.run(full=full)
        # Vermerke „filtert nicht“ dieser Quelle (Issue #1041), alte Einträge je Server übernommen
        filter_notes = self._filter_notes(url, source_row)
        options = SourceFetchOptions.from_sync_config(getattr(source_row, "sync_config", None))
        full, fetch_deadline = self._full_sync_plan(url, options, full, source_row, default_window)
        full, slice_run, slice_note = await self._slice_run(
            url, options, full, getattr(source_row, "sync_config", None)
        )
        if slice_note is not None:
            fetch_deadline = None  # nur Änderungsabgleich: das Zeitfenster gilt nur für den Vollabgleich
            result.errors.append(slice_note)
        result.full = full

        client: OParlClient | None = None
        try:
            async with OParlClient(
                max_concurrent=self.max_concurrent,
                source_name=url.split("/")[2] if "/" in url else "unknown",
                user_agent=getattr(source_row, "user_agent", None),
                fetch_deadline=fetch_deadline,
                filter_notes=filter_notes,
                **options.client_kwargs(),
            ) as client:
                # Fetch system
                console.print(f"\n[bold blue]Connecting to {url}...[/bold blue]")
                system_result = await client.fetch(url, use_cache=False, skip_wait=True)
                system_data = system_result.data
                reason = system_result.error or (
                    f"HTTP {system_result.status_code}" if system_result.status_code else "keine Antwort"
                )

                # OParl 1.0 (more! rubin): 1.1-Pfad liefert Fehlerobjekt/404 (Issue #122)
                if is_oparl_error(system_data):
                    reason = oparl_error_message(system_data)
                    system_data, _resolved = await self._try_oparl_10_fallback(client, url, reason)
                elif system_data is None and system_result.status_code == 404:
                    system_data, _resolved = await self._try_oparl_10_fallback(client, url, reason)

                if not system_data:
                    result.errors.append(f"Failed to fetch system from {url} ({reason})")
                    self._collect_host_findings(result, client)
                    await self._record_source_failure(url, reason, system_result.error_kind or client.error_kind)
                    return result

                result.source_name = system_data.get("name", "Unknown")
                console.print(f"[green]Connected to: {result.source_name}[/green]")

                # Ensure source exists in database
                source_id = await self.storage.upsert_source(
                    url=url,
                    name=result.source_name,
                    raw_json=system_data,
                    oparl_version=detect_oparl_version(system_data),
                )

                # Get body list URL
                body_list_url = system_data.get("body")
                if not body_list_url:
                    result.errors.append("No body list found in system")
                    return result

                # Fetch all bodies
                console.print("[blue]Fetching bodies list...[/blue]")
                missed_bodies: list[str] = []
                bodies_data = await self._fetch_bodies(client, body_list_url, missed_bodies)
                console.print(f"[dim]Found {len(bodies_data)} bodies[/dim]")
                if missed_bodies:
                    result.errors.append(missed_bodies_note(missed_bodies))

                # Filter bodies if requested
                if body_filter:
                    bodies_data = [
                        b
                        for b in bodies_data
                        if body_filter.lower() in b.get("name", "").lower() or body_filter in b.get("id", "")
                    ]
                    console.print(f"[dim]Filtered to {len(bodies_data)} bodies[/dim]")

                # Sync all bodies IN PARALLEL for massive speedup
                console.print(f"[bold green]Starting PARALLEL sync of {len(bodies_data)} bodies...[/bold green]")

                # Disable Progress bars if syncing multiple bodies in parallel
                # Rich doesn't support multiple live displays simultaneously
                if len(bodies_data) > 1:
                    self._parallel_mode = True

                # Bound per-body concurrency so multi-body sources don't
                # multiply peak memory (each body sync holds pages + caches).
                body_semaphore = asyncio.Semaphore(settings.sync_body_concurrency)
                source_config = getattr(source_row, "sync_config", None)
                missing_lists = MissingLists(url, missing_lists_of(source_config))
                timed_lists = TimedLists(url, timed_lists_of(source_config))

                async def sync_body_wrapper(body_data):
                    """Wrapper to catch exceptions per body."""
                    async with body_semaphore:
                        try:
                            return await self._sync_body(
                                client=client,
                                body_data=body_data,
                                source_id=source_id,
                                full=full,
                                options=options,
                                missing_lists=missing_lists,
                                timed_lists=timed_lists,
                                slices=slice_run,
                            )
                        except Exception as e:
                            console.print(f"[red]Error syncing {body_data.get('name', 'Unknown')}: {e}[/red]")
                            return {"errors": [str(e)], "incomplete": ["body"]}

                # Run all body syncs in parallel (bounded by semaphore)
                body_results = await asyncio.gather(
                    *[sync_body_wrapper(body_data) for body_data in bodies_data],
                    return_exceptions=False,
                )

                # Aggregate results
                for body_result in body_results:
                    result.bodies_synced += 1
                    result.meetings_synced += body_result.get("meetings", 0)
                    result.papers_synced += body_result.get("papers", 0)
                    result.persons_synced += body_result.get("persons", 0)
                    result.organizations_synced += body_result.get("organizations", 0)
                    result.memberships_synced += body_result.get("memberships", 0)
                    result.files_synced += body_result.get("files", 0)
                    result.locations_synced += body_result.get("locations", 0)
                    result.agenda_items_synced += body_result.get("agenda_items", 0)
                    result.consultations_synced += body_result.get("consultations", 0)
                    result.errors.extend(body_result.get("errors", []))
                    result.unstored.update(body_result.get("unstored") or {})
                    merge_slice_details(result.slice_details, body_result)

                # Update sync timestamp; vollständig nur ohne Lücke und ohne Filter auf Kommunen (Issue #556)
                complete = not body_filter and sync_complete(body_results, missed_bodies, client)
                await self.storage.update_source_sync_time(
                    source_id,
                    full_sync=full,
                    complete=complete,
                    full_complete=complete and full_sync_finished(body_results),
                )
                result.http_stats = client.stats
                result.success = True
                await self._apply_host_findings(result, client)
                if full:
                    await self._recheck_modified_since(client, url, bodies_data)

                # Calculate total entities synced
                total_synced = (
                    result.meetings_synced
                    + result.papers_synced
                    + result.persons_synced
                    + result.organizations_synced
                    + result.memberships_synced
                    + result.files_synced
                    + result.agenda_items_synced
                    + result.consultations_synced
                )

                end_time = datetime.now(UTC)
                duration = (end_time - start_time).total_seconds()

                # Write sync log to Django's SyncLog table
                try:
                    await self.storage.write_sync_log(
                        source_id=source_id,
                        sync_type="full" if full else "incremental",
                        status="success",
                        started_at=start_time,
                        finished_at=end_time,
                        duration_seconds=duration,
                        entities_synced=total_synced,
                        errors=result.errors,
                        details={
                            "meetings": result.meetings_synced,
                            "papers": result.papers_synced,
                            "persons": result.persons_synced,
                            "organizations": result.organizations_synced,
                            "memberships": result.memberships_synced,
                            "files": result.files_synced,
                            "agenda_items": result.agenda_items_synced,
                            "consultations": result.consultations_synced,
                            **self._finding_details(result),
                            **result.slice_details,
                        },
                        triggered_by="daemon",
                    )
                except Exception as log_err:
                    console.print(f"[yellow]Warning: Could not write sync log: {log_err}[/yellow]")

                # Record metrics
                metrics.record_entities_batch(result.source_name, total_synced)

        except Exception as e:
            result.errors.append(str(e))
            console.print(f"[red]Sync failed: {e}[/red]")
            await self._record_source_failure(url, str(e), client.error_kind if client else None)
            if client is not None:
                self._collect_host_findings(result, client)

            # Sync log is written by the scheduler (one per cycle, not per source)

        # Geänderte Vermerke „filtert nicht“ in der sync_config dieser Quelle speichern (Issue #1041)
        await self._save_filter_notes(url, filter_notes)

        result.duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
        return result

    async def _sync_body(
        self,
        client: OParlClient,
        body_data: dict[str, Any],
        source_id: UUID,
        full: bool,
        options: SourceFetchOptions | None = None,
        missing_lists: MissingLists | None = None,
        timed_lists: TimedLists | None = None,
        slices: SliceRun | None = None,
    ) -> dict[str, Any]:
        """
        Sync a single body and all its entities.

        Mit ``slices`` (Issue #1033): Ein Vollabgleich mit ``full_sync_slices`` liest die Listen in Zeitscheiben, und
        optionale Listen werden nach Zeitüberschreitungen ausgesetzt (``src/sync/slices.py``).

        Inkrementell fragt der Lauf nur nach Änderungen seit dem Beginn des letzten vollständig erfolgreichen
        Abgleichs der Kommune abzüglich Sicherheitsabstand (``modified_since_for``), höchstens sieben Tage zurück.
        Ist dieser Lauf vollständig (jede Liste ganz gelesen oder als fehlend vermerkt), wird sein Beginn der neue
        Zeitpunkt, bei nicht gespeicherten Objekten höchstens deren ältestes ``modified`` (``BodyItemCheck``); ein
        Lauf mit Lücke lässt ihn stehen. Listen ohne nachgewiesene Uhrzeit in ``modified`` fragt er ab dem
        Tagesbeginn an (``TimedLists``, Issue #1025).

        Returns statistics about synced entities.
        """
        # Vor dem ersten Abruf: Was die Quelle danach ändert, erfasst der nächste Lauf
        body_started = datetime.now(UTC)
        stats: dict[str, Any] = {
            "meetings": 0,
            "papers": 0,
            "persons": 0,
            "organizations": 0,
            "memberships": 0,
            "files": 0,
            "locations": 0,
            "agenda_items": 0,
            "consultations": 0,
            "errors": [],
            # Objekttypen, deren Liste nicht vollständig gelesen wurde (Issue #556)
            "incomplete": [],
        }

        body_name = body_data.get("name", "Unknown")
        body_external_id = body_data.get("id", "")

        console.print(f"\n[bold cyan]{'=' * 50}[/bold cyan]")
        console.print(f"[bold cyan]Syncing: {body_name}[/bold cyan]")
        console.print(f"[bold cyan]{'=' * 50}[/bold cyan]")

        # Alle Ereignisse dieses Abgleichs tragen dieselbe Korrelations-ID (src/storage/events.py)
        start_correlation()

        # Process and store body
        processed_body = self.processor.process_body(body_data, body_external_id)
        body_id = await self.storage.upsert_body(processed_body, source_id)
        # Voriger Abgleich des Bodies: Ab dann geschriebene Objekte gehen in den Suchindex (inkrementell)
        try:
            previous_sync = await self.storage.get_body_last_sync(body_id)
        except Exception as e:  # noqa: BLE001 - ohne Zeitpunkt wird der ganze Body indexiert
            console.print(f"[yellow]  Letzter Abgleich des Bodies nicht lesbar ({e}), Suchindex vollständig[/yellow]")
            previous_sync = None

        # Änderungsfenster des inkrementellen Laufs (Issue #1025): seit dem Beginn des letzten vollständig
        # erfolgreichen Abgleichs dieser Kommune abzüglich Sicherheitsabstand; ohne ihn, oder liegt er weiter
        # zurück, wie bisher sieben Tage. Die Quelle filtert vor, der Abgleich mit dem Bestand (batch_check,
        # Content-Hash) prüft genau.
        modified_since: datetime | None = None
        if not full:
            margin = (options or SourceFetchOptions()).modified_since_margin()
            try:
                sync_started = await self.storage.get_body_sync_started(source_id, body_external_id)
            except Exception as e:  # noqa: BLE001 - ohne Zeitpunkt gilt das breite Fenster
                console.print(f"[yellow]  Letzter vollständiger Abgleich nicht lesbar ({e}), Fenster 7 Tage[/yellow]")
                sync_started = None
            modified_since = modified_since_for(sync_started, margin)
            if sync_started is None:
                basis = "7 Tage"
            elif modified_since.tzinfo is None:
                basis = f"7 Tage, letzter vollständiger Abgleich {sync_started.isoformat()} liegt weiter zurück"
            else:
                basis = f"letzter vollständiger Abgleich {sync_started.isoformat()}"
            console.print(f"[dim]Incremental sync: modified_since={modified_since.isoformat()} ({basis})[/dim]")
        elif slices is not None and slices.config is not None:
            console.print("[dim]Full sync: Listen in Zeitscheiben (full_sync_slices)[/dim]")
        else:
            console.print("[dim]Full sync: fetching ALL pages[/dim]")

        # Fehlende Listen dieser Kommune (Issue #1025), vermerkt am Ende des Laufs
        list_check = BodyListCheck(missing_lists) if missing_lists is not None else None
        # Nicht gespeicherte Objekte bestimmen den nächsten Fensteranfang, Stempel der Änderungen der letzten sieben
        # Tage, welche Listen das kurze Fenster bekommen (Issue #1025)
        item_check = BodyItemCheck(recent_since=fallback_modified_since().replace(tzinfo=UTC))
        # Zeitscheiben und langsame Listen dieser Kommune (Issue #1033)
        body_slices = BodySlices(slices) if slices is not None else None

        # Collects ES documents of entities that were tombstoned during this
        # body sync (index name -> doc ids). Flushed in Phase 3 so the search
        # index does not keep documents for source-deleted objects.
        es_deletions: dict[str, list[str]] = {}

        # Helper to build common kwargs for _sync_entity_type
        def sync_kwargs(list_url: str | None, entity_type: str) -> dict:
            return {
                "client": client,
                "list_url": list_url,
                "entity_type": entity_type,
                "body_id": body_id,
                "body_external_id": body_external_id,
                "body_name": body_name,
                "modified_since": modified_since,
                "full": full,
                "es_deletions": es_deletions,
                "list_check": list_check,
                "item_check": item_check,
                "timed_lists": timed_lists,
                "slices": body_slices,
            }

        # Phase 2 (parallel): Start text extraction as background task
        # Processes already-pending files while entity sync runs concurrently.
        # New files synced in this cycle will be picked up in the next cycle.
        extraction_task: asyncio.Task | None = None
        if settings.text_extraction_enabled:

            async def _run_extraction():
                try:
                    from src.extraction.extractor import TextExtractor

                    extractor = TextExtractor(self.storage)
                    return await extractor.extract_pending_files(body_id)
                except Exception as e:
                    console.print(f"[red]  Text extraction error: {e}[/red]")
                    return e

            extraction_task = asyncio.create_task(_run_extraction())
            console.print("[dim]  Text extraction started in background[/dim]")

        # Drossel für Entity-Typ-Parallelität innerhalb eines Bodies
        # (Issue #22): begrenzt zusammen mit sync_body_concurrency und
        # sync_source_concurrency die Peak-Parallelität und damit das
        # Speicherbudget eines Zyklus. Die gather-Struktur (Abhängigkeits-
        # Reihenfolge) bleibt unverändert.
        entity_semaphore = asyncio.Semaphore(max(1, settings.sync_entity_concurrency))

        async def throttled(**kwargs):
            async with entity_semaphore:
                return await self._sync_entity_type(**kwargs)

        # Create progress display (disabled when not running in terminal or in parallel mode)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            disable=not console.is_terminal or self._parallel_mode,
        ) as progress:
            # Sync entities in parallel (but ordered for dependencies)
            # First: Organizations and Persons (no dependencies)
            task1 = progress.add_task("[cyan]Organizations...", total=None)
            task2 = progress.add_task("[cyan]Persons...", total=None)

            org_result, person_result = await asyncio.gather(
                throttled(**sync_kwargs(processed_body.organization_list_url, "organization")),
                throttled(**sync_kwargs(processed_body.person_list_url, "person")),
                return_exceptions=True,
            )

            if isinstance(org_result, Exception):
                stats["errors"].append(f"Organizations: {org_result}")
                stats["incomplete"].append("organization")
            else:
                stats["organizations"] = org_result
                progress.update(task1, completed=org_result, total=org_result)

            if isinstance(person_result, Exception):
                stats["errors"].append(f"Persons: {person_result}")
                stats["incomplete"].append("person")
            else:
                stats["persons"] = person_result
                progress.update(task2, completed=person_result, total=person_result)

            # Membership FK resolution uses per-page batched lookups
            # (get_person_ids_by_external_ids / get_organization_ids_by_external_ids)
            # instead of a global FK cache pre-load — see _sync_entity_type.

            # Second: Memberships (depends on persons + organizations). Eine unlesbare Liste ist eine Lücke wie
            # bei den anderen Typen, bricht aber nicht mehr die ganze Kommune ab: Sitzungen und Vorlagen der
            # Bezirksregierung Köln blieben sonst bis zum nächsten Vollabgleich stehen (Issue #1025).
            task3 = progress.add_task("[cyan]Memberships...", total=None)
            try:
                membership_result = await self._sync_entity_type(
                    **sync_kwargs(processed_body.membership_list_url, "membership")
                )
            except Exception as e:  # noqa: BLE001 - wie bei den übrigen Typen festhalten, weiter mit dem Rest
                stats["errors"].append(f"Memberships: {e}")
                stats["incomplete"].append("membership")
            else:
                stats["memberships"] = membership_result
                progress.update(task3, completed=membership_result, total=membership_result)

            # Third: Meetings and Papers (parallel, include nested entities)
            task4 = progress.add_task("[cyan]Meetings...", total=None)
            task5 = progress.add_task("[cyan]Papers...", total=None)

            meeting_result, paper_result = await asyncio.gather(
                throttled(**sync_kwargs(processed_body.meeting_list_url, "meeting")),
                throttled(**sync_kwargs(processed_body.paper_list_url, "paper")),
                return_exceptions=True,
            )

            if isinstance(meeting_result, Exception):
                stats["errors"].append(f"Meetings: {meeting_result}")
                stats["incomplete"].append("meeting")
            else:
                stats["meetings"] = meeting_result
                progress.update(task4, completed=meeting_result, total=meeting_result)

            if isinstance(paper_result, Exception):
                stats["errors"].append(f"Papers: {paper_result}")
                stats["incomplete"].append("paper")
            else:
                stats["papers"] = paper_result
                progress.update(task5, completed=paper_result, total=paper_result)

            # Fourth: Locations, AgendaItems, Files, Consultations (parallel)
            task6 = progress.add_task("[cyan]Locations...", total=None)
            task7 = progress.add_task("[cyan]AgendaItems...", total=None)
            task8 = progress.add_task("[cyan]Files...", total=None)
            task9 = progress.add_task("[cyan]Consultations...", total=None)

            (
                location_result,
                agenda_item_result,
                file_result,
                consultation_result,
            ) = await asyncio.gather(
                throttled(**sync_kwargs(processed_body.location_list_url, "location")),
                throttled(**sync_kwargs(processed_body.agenda_item_list_url, "agendaitem")),
                throttled(**sync_kwargs(processed_body.file_list_url, "file")),
                throttled(**sync_kwargs(processed_body.consultation_list_url, "consultation")),
                return_exceptions=True,
            )

            if isinstance(location_result, Exception):
                stats["errors"].append(f"Locations: {location_result}")
                stats["incomplete"].append("location")
            else:
                stats["locations"] = location_result
                progress.update(task6, completed=location_result, total=location_result)

            if isinstance(agenda_item_result, Exception):
                stats["errors"].append(f"AgendaItems: {agenda_item_result}")
                stats["incomplete"].append("agendaitem")
            else:
                stats["agenda_items"] = agenda_item_result
                progress.update(task7, completed=agenda_item_result, total=agenda_item_result)

            if isinstance(file_result, Exception):
                stats["errors"].append(f"Files: {file_result}")
                stats["incomplete"].append("file")
            else:
                stats["files"] = file_result
                progress.update(task8, completed=file_result, total=file_result)

            if isinstance(consultation_result, Exception):
                stats["errors"].append(f"Consultations: {consultation_result}")
                stats["incomplete"].append("consultation")
            else:
                stats["consultations"] = consultation_result
                progress.update(task9, completed=consultation_result, total=consultation_result)

        if list_check is not None:
            await self._settle_missing_lists(list_check, stats)
        if body_slices is not None:
            await self._settle_slices(body_slices, stats, full)

        # Update body sync time
        await self.storage.update_body_sync_time(body_id)
        if timed_lists is not None:
            await self._settle_timed_lists(timed_lists, item_check, stats)
        if body_external_id:
            await self._advance_window(source_id, body_external_id, body_started, stats, item_check)

        # Await background text extraction (started before entity sync)
        if extraction_task is not None:
            result = await extraction_task
            if isinstance(result, Exception):
                stats["errors"].append(f"Text extraction: {result}")
            elif isinstance(result, int):
                stats["text_extracted"] = result
                if result > 0:
                    console.print(f"[green]  Extracted text from {result} files (parallel)[/green]")
                else:
                    console.print("[dim]  No pending files for text extraction[/dim]")

        # Phase 3: Elasticsearch Indexing
        # Only re-index during full sync or when entities actually changed.
        total_synced = sum(
            stats.get(k, 0)
            for k in (
                "organizations",
                "persons",
                "memberships",
                "meetings",
                "papers",
                "files",
                "agenda_items",
                "consultations",
                "locations",
            )
        )
        total_tombstoned = sum(len(ids) for ids in es_deletions.values())
        text_extracted = stats.get("text_extracted", 0) or 0
        if settings.elasticsearch_indexing_enabled and (
            full or total_synced > 0 or total_tombstoned > 0 or text_extracted > 0
        ):
            await self._index_body_elasticsearch(
                body_id, stats, es_deletions, full, since=index_since(full, previous_sync)
            )
        elif settings.elasticsearch_indexing_enabled and not full:
            console.print("[dim]  Elasticsearch indexing skipped (no changes)[/dim]")

        # Print summary
        console.print("[green]Body sync complete:[/green]")
        console.print(f"  Organizations: {stats['organizations']}")
        console.print(f"  Persons: {stats['persons']}")
        console.print(f"  Memberships: {stats['memberships']}")
        console.print(f"  Meetings: {stats['meetings']}")
        console.print(f"  Papers: {stats['papers']}")
        console.print(f"  Locations: {stats['locations']}")
        console.print(f"  AgendaItems: {stats['agenda_items']}")
        console.print(f"  Files: {stats['files']}")
        console.print(f"  Consultations: {stats['consultations']}")

        return stats

    async def _index_body_elasticsearch(
        self,
        body_id: UUID,
        stats: dict[str, Any],
        es_deletions: dict[str, list[str]],
        full: bool,
        since: datetime | None = None,
    ) -> None:
        """
        Indexiert die Entitäten eines Bodies in Elasticsearch und entfernt
        Dokumente tombstoneder Objekte. Gemeinsamer Pfad für OParl-Sync
        (_sync_body) und Scraper-Quellen (ScraperSyncRunner).

        Vollabgleich (oder ``since`` leer): alle Objekte, seitenweise. Inkrementell: nur Objekte, die seit
        ``since`` geschrieben wurden, dazu Vorgänge, deren Dateien seitdem geändert wurden (Gewichtung).

        Geschrieben werden Teildokumente (partielles Update): Felder, die nur
        Django setzt (z. B. organization_names), bleiben erhalten (Issue #429).

        Fehler landen in stats["errors"]; der Sync bricht dadurch nicht ab.
        """
        total_tombstoned = sum(len(ids) for ids in es_deletions.values())
        try:
            from src.indexing.document_builders import (
                FILE_PREVIEW_CHARS,
                file_to_doc,
                meeting_to_doc,
                organization_to_doc,
                paper_to_doc,
                person_to_doc,
            )
            from src.indexing.elasticsearch import ElasticsearchIndexer
            from src.storage.models import (
                OParlMeeting as MeetingModel,
            )
            from src.storage.models import (
                OParlOrganization as OrgModel,
            )
            from src.storage.models import (
                OParlPaper as PaperModel,
            )
            from src.storage.models import (
                OParlPerson as PersonModel,
            )

            console.print("\n[bold yellow]Elasticsearch Indexing...[/bold yellow]")
            async with ElasticsearchIndexer() as indexer:
                if not await indexer.is_healthy():
                    console.print("[yellow]  Elasticsearch not reachable, skipping indexing[/yellow]")
                else:
                    # Indizes legt Django an (setup_elasticsearch); fehlende werden
                    # hier nur gemeldet und beim Schreiben übersprungen (Issue #215).
                    missing = await indexer.missing_indices()
                    if missing:
                        console.print(
                            "[yellow]  Elasticsearch indices missing: "
                            f"{', '.join(sorted(missing))} — run `manage.py setup_elasticsearch`[/yellow]"
                        )
                    indexed_total = 0

                    # Remove documents of tombstoned entities from the
                    # search index (DB rows are kept, only flagged).
                    if total_tombstoned:
                        for index_name, doc_ids in es_deletions.items():
                            await indexer.delete_documents(index_name, doc_ids)
                        stats["es_deleted"] = total_tombstoned
                        console.print(
                            f"[yellow]  Removed {total_tombstoned} tombstoned documents from Elasticsearch[/yellow]"
                        )

                    # Seitenweise: jede Art vollständig, Speicher je Seite begrenzt (früher höchstens
                    # 10.000 je Art, größere Kommunen fehlten danach teilweise in der Suche). Inkrementelle
                    # Läufe schreiben nur, was seit dem vorigen Abgleich geändert wurde.
                    page_size = settings.elasticsearch_batch_size
                    updated_since = None if full else since

                    # Vorgänge mit den Texten ihrer Dateien (Gewichtung in der Suche, je Datei nur der Anfang)
                    async for papers in self.storage.iter_for_body(
                        body_id, PaperModel, page_size, updated_since=updated_since, with_changed_files=True
                    ):
                        files_by_paper: dict[str, list[Any]] = {}
                        paper_files = await self.storage.get_files_with_text_for_papers(
                            body_id, [p.id for p in papers], max_chars=FILE_PREVIEW_CHARS
                        )
                        for f in paper_files:
                            files_by_paper.setdefault(str(f.paper_id), []).append(f)
                        docs = [paper_to_doc(p, files=files_by_paper.get(str(p.id), [])) for p in papers]
                        await indexer.index_documents("papers", docs)
                        indexed_total += len(docs)

                    for model, index_name, to_doc in (
                        (MeetingModel, "meetings", meeting_to_doc),
                        (PersonModel, "persons", person_to_doc),
                        (OrgModel, "organizations", organization_to_doc),
                    ):
                        async for rows in self.storage.iter_for_body(
                            body_id, model, page_size, updated_since=updated_since
                        ):
                            docs = [to_doc(row) for row in rows]
                            await indexer.index_documents(index_name, docs)
                            indexed_total += len(docs)

                    # Dateien mit extrahiertem Text
                    async for files in self.storage.iter_files_with_text(
                        body_id, page_size, updated_since=updated_since
                    ):
                        docs = [file_to_doc(f) for f in files]
                        await indexer.index_documents("files", docs)
                        indexed_total += len(docs)

                    stats["indexed"] = indexed_total
                    scope = "" if updated_since is None else f" (geändert seit {updated_since:%d.%m. %H:%M} UTC)"
                    console.print(f"[green]  Indexed {indexed_total} documents in Elasticsearch{scope}[/green]")

        except Exception as e:
            console.print(f"[red]  Elasticsearch indexing error: {e}[/red]")
            stats.setdefault("errors", []).append(f"Elasticsearch indexing: {e}")

    # Entity types that have their own Elasticsearch index (see indexing/)
    _ES_INDEX_BY_ENTITY_TYPE = {
        "paper": "papers",
        "meeting": "meetings",
        "person": "persons",
        "organization": "organizations",
        "file": "files",
    }

    async def _mark_deleted(
        self,
        item: dict[str, Any],
        entity_type: str,
        es_deletions: dict[str, list[str]] | None,
    ) -> bool:
        """
        Handle an OParl tombstone (deleted: true): mark the row as deleted
        (never physically delete) and queue its search document for removal.

        Returns True if the entity was newly marked.
        """
        external_id = item.get("id", "")
        if not external_id:
            return False
        modified = self.processor.parse_datetime(item.get("modified"))
        if entity_type == "meeting":
            await self._retract_session_location(external_id, modified)
        entity_id = await self.storage.mark_entity_deleted(entity_type, external_id, modified=modified)
        if entity_id is None:
            return False
        index_name = self._ES_INDEX_BY_ENTITY_TYPE.get(entity_type)
        if index_name is not None and es_deletions is not None:
            es_deletions.setdefault(index_name, []).append(str(entity_id))
        return True

    async def _retract_session_location(self, meeting_external_id: str, modified: datetime | None = None) -> None:
        """
        Eigenes Location-Objekt zum Ort einer Session-Sitzung zurücknehmen (``session_location_id``).

        Der Ort einer Session-Sitzung steht im Bestand als Text an der Sitzung. Ein eigenes Objekt unter
        der Kennung des Ortes kann nur ein älterer Stand des Ingestors angelegt haben; es darf weder die
        Rücknahme der Sitzung überdauern noch neben ihr veralten. Deshalb bei jeder Änderung und bei der
        Rücknahme der Sitzung markieren – für Sitzungen anderer Quellen geschieht nichts.
        """
        location_id = session_location_id(meeting_external_id)
        if location_id is not None:
            await self.storage.mark_entity_deleted("location", location_id, modified=modified)

    async def _sync_entity_type(
        self,
        client: OParlClient,
        list_url: str | None,
        entity_type: str,
        body_id: UUID,
        body_external_id: str,
        body_name: str | None = None,
        modified_since: datetime | None = None,
        full: bool = False,
        es_deletions: dict[str, list[str]] | None = None,
        list_check: BodyListCheck | None = None,
        item_check: BodyItemCheck | None = None,
        timed_lists: TimedLists | None = None,
        slices: BodySlices | None = None,
    ) -> int:
        """
        Sync all entities of a specific type.

        Optionale Listen, die es bei der Quelle nicht gibt (``list_check``, Issue #1025), fragt der inkrementelle
        Lauf einmal am Tag an, der Vollabgleich jedes Mal. Nicht gespeicherte Objekte und die Genauigkeit von
        ``modified`` hält ``item_check`` fest. Das kurze Fenster gilt nur für Listen, deren ``modified``
        nachweislich die Uhrzeit trägt (``timed_lists``); die übrigen fragt der inkrementelle Lauf ab dem
        Tagesbeginn an (``day_start_window``).

        Mit ``slices`` (Issue #1033): Im Vollabgleich mit ``full_sync_slices`` kommt die Liste in Zeitscheiben ab dem
        gespeicherten Fortschritt (``full_windows``); vermerkte fehlende Listen fragt er dann nicht an. Eine
        ausgesetzte langsame optionale Liste fragt kein Lauf an, eine fällige holt der inkrementelle Lauf in Scheiben
        zu einem Tag ab ihrem eigenen Fensteranfang nach (``slow_windows``), wenn die Quelle die Liste filtert
        (``OParlClient.filters_list``), sonst ohne Scheiben ab dort. Bricht eine optionale Liste nach einer
        Zeitüberschreitung ab, wird sie ausgesetzt statt eine Lücke zu bleiben.

        For full sync:
        - Fetches all pages and upserts everything

        For incremental sync (hybrid approach):
        - Sends ?modified_since= to server (pre-filter, reduces traffic)
        - Compares returned items against DB (batch_check for exact detection)
        - New items: Save, Modified items: Update
        - Stops after 5 consecutive pages with no changes (safety net)

        Items with deleted:true (OParl tombstones) are NEVER upserted or
        physically deleted -- the existing row is only marked
        (deleted=true/deleted_at) and its search-index document is queued
        for removal via es_deletions. Physical deletion happens exclusively
        via Django's purge_deleted command.

        Returns the number of entities actually synced (new + updated).
        """
        if not list_url:
            return 0
        optional = entity_type in OPTIONAL_LIST_TYPES
        run = slices.run if slices is not None else None
        sliced_full = full and run is not None and run.config is not None and run.config.covers(entity_type)
        if not full and optional and list_check is not None and list_url in list_check.lists.known:
            if list_check.lists.skip(list_url):
                console.print(f"[dim]  {entity_type}: Liste fehlt bei der Quelle (vermerkt), nicht angefragt[/dim]")
                return 0
            # Tägliche Prüfung der vermerkten Liste: Gibt es sie wieder, kommen die Änderungen aus der Zeit des
            # Vermerks mit, so weit wie bisher (sieben Tage), nicht nur die seit dem letzten Lauf
            if modified_since is not None:
                modified_since = fallback_modified_since()
        if sliced_full and optional and list_check is not None and list_url in list_check.lists.known:
            # SD.NET RIM beantwortet jede leere Scheibe wie eine fehlende Liste: Die Prüfung bleibt beim täglichen
            # inkrementellen Lauf, statt hunderte Scheiben einer Liste anzufragen, die es nicht gibt
            console.print(f"[dim]  {entity_type}: Liste fehlt bei der Quelle (vermerkt), keine Zeitscheiben[/dim]")
            return 0
        slow = run.slow.get(list_url) if run is not None and optional else None
        if slices is not None and slow is not None and slow.suspended():
            slices.suspended[list_url] = slow.until
            console.print(
                f"[dim]  {entity_type}: nach Zeitüberschreitung ausgesetzt bis {slow.until:%d.%m. %H:%M} UTC, "
                f"nicht angefragt[/dim]"
            )
            return 0
        # Liste ohne nachgewiesene Uhrzeit (Tagesstempel, gemischt, noch nichts gesehen): ab Tagesbeginn
        timed = timed_lists is not None and list_url in timed_lists.known
        if modified_since is not None and timed_lists is not None and not timed:
            modified_since = day_start_window(modified_since)

        # Zeitscheiben (Issue #1033): Vollabgleich ab dem Fortschritt der Liste, langsame Liste ab ihrem Fensteranfang;
        # in Tagesscheiben nur, wenn die Quelle die Liste filtert, sonst wie bisher ohne Scheiben ab dort
        windows: list[SliceWindow] | None = None
        slow_since: datetime | None = None
        if sliced_full and slices is not None and run is not None and run.config is not None:
            slices.types[list_url] = entity_type
            windows = full_windows(run.config, run.progress.get(list_url), datetime.now(UTC))
        elif slow is not None and not full and modified_since is not None:
            start = min(slow.since, _as_utc(modified_since))
            slow_since = start if timed else day_floor(start)
            if client.filters_list(list_url):
                windows = slow_windows(slow_since, datetime.now(UTC))
            else:
                console.print(
                    f"[dim]  {entity_type}: Quelle filtert die Liste nicht, Nachholen ab "
                    f"{slow_since:%d.%m. %H:%M} UTC ohne Zeitscheiben[/dim]"
                )

        count = 0
        updated_count = 0
        deleted_count = 0
        skipped_count = 0
        pages_checked = 0
        consecutive_stale_pages = 0
        stale_pages_to_stop = 5

        # For incremental: send modified_since to server as pre-filter
        server_modified_since = modified_since if not full else None
        if slow_since is not None:
            server_modified_since = slow_since  # langsame Liste: ab ihrem eigenen Fensteranfang

        pages: AsyncIterator[list[dict[str, Any]]]
        if windows is not None and slices is not None:
            pages = self._slice_pages(
                client, list_url, entity_type, windows, list_check, slices, full=full, optional=optional
            )
        else:
            pages = self._list_pages(client, list_url, server_modified_since, list_check, optional=optional)
            if slices is not None and optional:
                # Bricht die Liste nach einer Zeitüberschreitung ab, wird sie ausgesetzt (Issue #1033). Ihr
                # Fensteranfang: im Vollabgleich der bisherige oder sieben Tage, sonst das Fenster dieses Laufs
                if server_modified_since is not None:
                    keep_since = _as_utc(server_modified_since)
                else:
                    keep_since = slow.since if slow is not None else fallback_modified_since().replace(tzinfo=UTC)
                pages = self._slow_guard(pages, slices, list_url, entity_type, keep_since)

        async for page in pages:
            pages_checked += 1

            # Memberships reference persons/organizations by external_id.
            # Resolve the FKs needed for THIS page with one batched SELECT
            # each (WHERE external_id IN (...)) — replaces the former global
            # load_fk_caches() that pulled ALL persons/orgs into memory.
            if entity_type == "membership":
                person_refs = {ref for ref in (item.get("person") for item in page) if isinstance(ref, str) and ref}
                org_refs = {ref for ref in (item.get("organization") for item in page) if isinstance(ref, str) and ref}
                if person_refs:
                    await self.storage.get_person_ids_by_external_ids(list(person_refs))
                if org_refs:
                    await self.storage.get_organization_ids_by_external_ids(list(org_refs))

            new_on_page = 0
            updated_on_page = 0
            deleted_on_page = 0
            unchanged_on_page = 0

            if full:
                # Full sync: upsert everything
                for item in page:
                    try:
                        # Tombstones (deleted:true) also im Full-Sync erkennen:
                        # markieren statt upserten (sonst wuerde ein geloeschtes
                        # Objekt wieder als aktiv eingespielt).
                        if item.get("deleted") is True:
                            if await self._mark_deleted(item, entity_type, es_deletions):
                                deleted_count += 1
                            continue
                        if item_check is not None:
                            item_check.note_stamp(list_url, item)
                        # Content-Hash mitschreiben, damit der nächste inkrementelle
                        # Lauf unveränderte Objekte ohne Zeitstempel erkennt (#122)
                        if timestamps_unreliable(item):
                            item[CONTENT_HASH_FIELD] = content_fingerprint(item)
                        processed = self.processor.process(item, body_external_id)
                        if processed:
                            stored = await self._store_entity(processed, body_id, entity_type, body_name)
                            if stored:
                                count += 1
                            elif item_check is not None:
                                item_check.note_unstored(entity_type, item, server_modified_since)
                    except Exception as e:
                        console.print(f"[red]Error processing {entity_type}: {e}[/red]")
                        if item_check is not None:
                            item_check.note_unstored(entity_type, item, server_modified_since)

            else:
                # Incremental hybrid: server pre-filtered + client-side comparison
                external_ids = [item.get("id", "") for item in page if item.get("id")]
                existing_ids = await self.storage.batch_check_entities_exist(entity_type, external_ids)

                # OParl 1.0 (more! rubin, Issue #122): created/modified sind
                # synthetisch (Abrufdatum) oder fehlen ganz. Für solche Objekte
                # entscheidet der Content-Hash im raw_json statt der Zeitstempel,
                # wie bei Scraper-Quellen — sonst würde jeder Lauf alles upserten.
                unreliable_ids = {item.get("id", "") for item in page if item.get("id") and timestamps_unreliable(item)}
                stored_hashes: dict[str, str | None] = {}
                if unreliable_ids:
                    stored_hashes = await self.storage.get_entity_content_hashes(entity_type, sorted(unreliable_ids))

                for item in page:
                    try:
                        external_id = item.get("id", "")
                        if not external_id:
                            continue

                        # Handle deleted items (Bonn/Aachen/Köln/ITK send deleted
                        # flag): mark as tombstone instead of physically deleting
                        if item.get("deleted") is True:
                            if await self._mark_deleted(item, entity_type, es_deletions):
                                deleted_on_page += 1
                                deleted_count += 1
                            continue
                        if item_check is not None:
                            item_check.note_stamp(list_url, item)

                        db_modified = existing_ids.get(external_id)

                        if external_id in unreliable_ids:
                            item_hash = content_fingerprint(item)
                            stored_hash = stored_hashes.get(external_id)
                            if stored_hash == item_hash:
                                unchanged_on_page += 1
                                skipped_count += 1
                                continue
                            item[CONTENT_HASH_FIELD] = item_hash
                            processed = self.processor.process(item, body_external_id)
                            if processed:
                                stored = await self._store_entity(processed, body_id, entity_type, body_name)
                                if stored and db_modified is None and stored_hash is None:
                                    new_on_page += 1
                                    count += 1
                                elif stored:
                                    updated_on_page += 1
                                    updated_count += 1
                                elif item_check is not None:
                                    item_check.note_unstored(entity_type, item, server_modified_since)
                            continue

                        item_modified = self.processor.parse_datetime(item.get("modified"))

                        if db_modified is None:
                            # New item: save
                            processed = self.processor.process(item, body_external_id)
                            if processed:
                                stored = await self._store_entity(processed, body_id, entity_type, body_name)
                                if stored:
                                    new_on_page += 1
                                    count += 1
                                elif item_check is not None:
                                    item_check.note_unstored(entity_type, item, server_modified_since)
                        elif item_modified and db_modified and item_modified > db_modified:
                            # Modified item: update (upsert handles ON CONFLICT)
                            processed = self.processor.process(item, body_external_id)
                            if processed:
                                stored = await self._store_entity(processed, body_id, entity_type, body_name)
                                if stored:
                                    updated_on_page += 1
                                    updated_count += 1
                                elif item_check is not None:
                                    item_check.note_unstored(entity_type, item, server_modified_since)
                        else:
                            # Unchanged: skip
                            unchanged_on_page += 1
                            skipped_count += 1

                    except Exception as e:
                        console.print(f"[red]Error processing {entity_type}: {e}[/red]")
                        if item_check is not None:
                            item_check.note_unstored(entity_type, item, server_modified_since)

            # Log page results (skip for full sync to reduce noise)
            if not full:
                has_changes = new_on_page > 0 or updated_on_page > 0 or deleted_on_page > 0
                if has_changes:
                    parts = []
                    if new_on_page > 0:
                        parts.append(f"{new_on_page} new")
                    if updated_on_page > 0:
                        parts.append(f"{updated_on_page} updated")
                    if deleted_on_page > 0:
                        parts.append(f"{deleted_on_page} deleted")
                    console.print(f"[green]  Page {pages_checked}: {', '.join(parts)}[/green]")
                    consecutive_stale_pages = 0
                else:
                    consecutive_stale_pages += 1

                # Safety stop: 5 consecutive pages without changes (nicht in Zeitscheiben: jede hat ihren Zeitraum;
                # nach einem Rückfall auf das Lesen ohne Scheiben wieder)
                unsliced = windows is None or (slices is not None and list_url in slices.unsliced)
                if consecutive_stale_pages >= stale_pages_to_stop and unsliced:
                    console.print(
                        f"[yellow]  Stopping {entity_type}: "
                        f"{consecutive_stale_pages} consecutive pages without changes[/yellow]"
                    )
                    if slices is not None and optional:
                        # Bis hierher gelesen gilt die Liste als aktuell: Eine Aussetzung endet (Issue #1033)
                        await self._clear_slow_list(slices, list_url)
                    break

        total = count + updated_count
        if not full and (count > 0 or updated_count > 0 or deleted_count > 0 or skipped_count > 0):
            console.print(
                f"[dim]  {entity_type} summary: {count} new, {updated_count} updated, "
                f"{deleted_count} deleted, {skipped_count} unchanged[/dim]"
            )

        return total

    async def _list_pages(
        self,
        client: OParlClient,
        list_url: str,
        modified_since: datetime | None,
        list_check: BodyListCheck | None,
        *,
        optional: bool = True,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Seiten einer Liste; gibt es eine optionale Liste bei der Quelle nicht, keine Seite (Issue #1025).

        HTTP 404 oder ein Fehlerobjekt auf der ersten Seite (ohne Filter) zählt bei Listen, die OParl 1.1 als
        optional führt (``OPTIONAL_LIST_TYPES``), als „nicht vorhanden“, nicht als Lücke: Die Liste landet in
        ``list_check`` und wird am Ende des Laufs in ``sync_config["missing_lists"]`` vermerkt
        (``_settle_missing_lists``); inkrementell fragt sie erst die tägliche Prüfung wieder an, der Vollabgleich
        jedes Mal. Wird eine vermerkte Liste ganz gelesen, wird der Vermerk gelöscht; meldet SD.NET RIM auf den
        Filter nur „keine Treffer“, bleibt er (Issue #1034).

        Pflichtlisten werden nie vermerkt. HTTP 404 ist dort eine Lücke (``RequiredListMissingError``). Ein
        Fehlerobjekt gilt wie bisher als leere Liste, solange eine andere Liste der Kommune lesbar war; sonst als
        Lücke (``_settle_missing_lists``). Ohne ``list_check`` wie bisher.
        """
        if list_check is None:
            async for page in client.fetch_list(list_url, modified_since=modified_since):
                yield page
            return
        readable = False
        try:
            async for page in client.fetch_list(list_url, modified_since=modified_since, report_missing=True):
                if not readable:
                    readable = True
                    list_check.readable += 1
                yield page
        except ListMissingError as missing:
            if optional:
                list_check.missing[list_url] = missing.reason
            elif missing.not_found:
                raise RequiredListMissingError(list_url, missing.reason) from missing
            else:
                list_check.required_errors[list_url] = missing.reason
            return
        if not readable:
            list_check.readable += 1  # lesbar, nur leer
        if list_url in list_check.lists.known and list_url in client.filtered_empty_lists:
            # SD.NET RIM: „keine Treffer“ auf den Filter sagt nicht, ob es die vermerkte Liste gibt (Issue #1034). Der
            # Vermerk bleibt und gilt ab jetzt wieder einen Tag; Änderungen fände die nächste Prüfung (sieben Tage)
            await self._remember_missing_list(list_check.lists, list_url, missing=True)
            return
        await self._remember_missing_list(list_check.lists, list_url, missing=False)

    # ========== Zeitscheiben und langsame Listen (Issue #1033) ==========

    async def _slice_pages(
        self,
        client: OParlClient,
        list_url: str,
        entity_type: str,
        windows: list[SliceWindow],
        list_check: BodyListCheck | None,
        slices: BodySlices,
        *,
        full: bool,
        optional: bool,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Seiten einer Liste in Zeitscheiben, von alt nach neu, jede Scheibe nur mit Filter (``strict_filter``).

        Nach jeder ganz gelesenen Scheibe (alle Seiten verarbeitet) rückt der Fortschritt vor: im Vollabgleich in
        ``sync_config["full_sync_progress"]``, bei einer langsamen Liste ihr Fensteranfang in
        ``sync_config["slow_lists"]``. Ein Objekt mehr als ``SLICE_TOLERANCE`` außerhalb der Scheibe heißt: Die
        Quelle ignoriert den Filter. Im Vollabgleich bricht die Liste dann ab (``SliceFilterIgnoredError``), jede
        andere Liste dieses Laufs vor ihrer nächsten Scheibe, und der Befund wird gespeichert. Bricht eine optionale
        Liste nach einer Zeitüberschreitung ab, wird sie ausgesetzt (``_suspend_slow_list``), ihr Fortschritt bleibt.
        Die erste Scheibe meldet eine fehlende Liste wie ``_list_pages``, im Vollabgleich nur die erste Scheibe des
        Durchgangs: Setzt eine Liste an ihrem gespeicherten Fortschritt fort, haben frühere Scheiben sie nachgewiesen.
        HTTP 404 (ohne Code 802) oder ein Fehlerobjekt sind dann eine Lücke (``ListFetchError``), kein Vermerk „fehlt“
        und keine leere Pflichtliste; sonst zählte der Durchgang als fertig, obwohl die Liste nicht fertig ist.

        Beim inkrementellen Nachholen einer langsamen Liste gibt es keinen Befund an der Quelle: Ignoriert sie den
        Filter, lehnt sie ihn ab (HTTP 400, 401, 403, 404) oder findet sie die Liste mit Filter nicht, liest der Lauf
        die Liste ab der offenen Scheibe wie bisher ohne Zeitscheiben (``_slice_fallback``).
        """
        run = slices.run
        readable = False
        # Eine Scheibe hat die Liste nachgewiesen: Daten oder eine leere Liste, nicht nur „keine Treffer“ von SD.NET RIM
        exists = False
        # Fortgesetzter Durchgang im Vollabgleich: Die Liste gibt es, frühere Scheiben haben sie gelesen
        resumed = full and run.progress.get(list_url) is not None
        for index, window in enumerate(windows):
            if full and run.filter_ignored is not None:
                raise SliceFilterIgnoredError(list_url, run.filter_ignored)
            fallback: str | None = None
            try:
                async for page in client.fetch_list(
                    list_url,
                    modified_since=window.since,
                    modified_until=window.until,
                    strict_filter=True,
                    report_missing=list_check is not None and index == 0 and not resumed,
                ):
                    if list_check is not None and not readable:
                        readable = True
                        list_check.readable += 1
                    for item in page:
                        stamp = window.outside(item)
                        if stamp is not None and not full:
                            raise _SliceFallbackError(
                                f"Quelle ignoriert den Filter: Scheibe {window.label()} liefert modified "
                                f"{stamp.isoformat(timespec='seconds')}"
                            )
                        if stamp is not None:
                            await self._remember_filter_ignored(slices, list_url, window, item, stamp)
                            raise SliceFilterIgnoredError(list_url, run.filter_ignored or "")
                    slices.note_without_modified(entity_type, page)
                    yield page
            except _SliceFallbackError as reason:
                fallback = str(reason)
            except ListMissingError as missing:
                if not full:
                    fallback = f"Liste mit Filter nicht gefunden ({missing.reason})"
                elif list_check is None:
                    return
                else:
                    if optional:
                        list_check.missing[list_url] = missing.reason
                    elif missing.not_found:
                        raise RequiredListMissingError(list_url, missing.reason) from missing
                    else:
                        list_check.required_errors[list_url] = missing.reason
                    return
            except ListFetchError as error:
                if not full and _filter_rejected(error):
                    fallback = f"Filter abgelehnt (HTTP {error.result.status_code})"
                elif not (optional and error.timed_out):
                    raise
                else:
                    slow = run.slow.get(list_url)
                    if full:
                        keep_since = slow.since if slow is not None else fallback_modified_since().replace(tzinfo=UTC)
                    else:
                        keep_since = window.since or (slow.since if slow is not None else datetime.now(UTC))
                    if not await self._suspend_slow_list(
                        slices, list_url, entity_type, error, keep_since, progressed=index > 0
                    ):
                        raise
                    return
            if fallback is not None:
                async for page in self._slice_fallback(
                    client, list_url, entity_type, window, list_check, slices, fallback, optional=optional
                ):
                    yield page
                return
            exists = exists or list_url not in client.filtered_empty_lists
            slices.read[list_url] += 1
            await self._slice_done(slices, list_url, window, full)
        if list_check is not None and not readable:
            list_check.readable += 1  # lesbar, nur leer
        if full:
            slices.finished.add(list_url)
        await self._clear_slow_list(slices, list_url)
        if list_check is not None and list_url in list_check.lists.known:
            # Wie in ``_list_pages``: Gibt es die Liste, endet der Vermerk; meldete SD.NET RIM nur „keine Treffer“,
            # bleibt er und gilt ab jetzt wieder einen Tag (Issue #1034)
            await self._remember_missing_list(list_check.lists, list_url, missing=not exists)

    async def _slow_guard(
        self,
        pages: AsyncIterator[list[dict[str, Any]]],
        slices: BodySlices,
        list_url: str,
        entity_type: str,
        keep_since: datetime,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Optionale Liste ohne Zeitscheiben: Bricht sie nach einer Zeitüberschreitung ab, wird sie ausgesetzt statt eine
        Lücke zu bleiben (Fensteranfang ``keep_since``); ganz gelesen, endet eine Aussetzung (Issue #1033).
        """
        try:
            async for page in pages:
                yield page
        except ListFetchError as error:
            if not error.timed_out:
                raise
            if not await self._suspend_slow_list(slices, list_url, entity_type, error, keep_since):
                raise
            return
        await self._clear_slow_list(slices, list_url)

    async def _slice_fallback(
        self,
        client: OParlClient,
        list_url: str,
        entity_type: str,
        window: SliceWindow,
        list_check: BodyListCheck | None,
        slices: BodySlices,
        reason: str,
        *,
        optional: bool,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Inkrementelles Nachholen ohne Zeitscheiben (Issue #1033): Die Liste kommt ab dem Beginn der offenen Scheibe
        wie jede andere Liste (``_list_pages``: Rückfall des Clients bei abgelehntem Filter, Abbruch nach Seiten ohne
        Änderung); eine Zeitüberschreitung setzt sie wieder aus. Kein Befund an der Quelle: Ob sie den Filter einer
        Scheibe beachtet, prüft nur der Vollabgleich in Zeitscheiben.
        """
        slow = slices.run.slow.get(list_url)
        since = window.since or (slow.since if slow is not None else fallback_modified_since().replace(tzinfo=UTC))
        slices.unsliced.add(list_url)
        console.print(
            f"[yellow]  {entity_type}: Nachholen in Tagesscheiben nicht möglich ({reason}); Liste ab "
            f"{since:%d.%m.%Y %H:%M} UTC ohne Zeitscheiben[/yellow]"
        )
        pages = self._list_pages(client, list_url, since, list_check, optional=optional)
        async for page in self._slow_guard(pages, slices, list_url, entity_type, since):
            yield page

    async def _suspend_slow_list(
        self,
        slices: BodySlices,
        list_url: str,
        entity_type: str,
        error: ListFetchError,
        since: datetime,
        *,
        progressed: bool = False,
    ) -> bool:
        """
        Optionale Liste nach einer Zeitüberschreitung aussetzen: 1, 2, 4, 8, 16, dann 24 Stunden (``slow_backoff``).
        Ihr Fensteranfang ``since`` bleibt, der nächste Lauf nach der Aussetzung holt die Änderungen ab dort nach.
        Hat dieser Versuch mindestens eine Scheibe ganz gelesen (``progressed``), beginnt die Reihe wieder bei einer
        Stunde: Die Liste kommt voran. Keine Lücke des Laufs, aber eine Meldung im Sync-Protokoll.

        Liefert ``False``, wenn sich die Aussetzung nicht speichern ließ: Ohne gespeicherten Fensteranfang gingen die
        Änderungen der Liste verloren, sobald das Fenster der Kommune vorrückt. Der Aufrufer wertet die Liste dann
        wie bisher als Lücke.
        """
        run = slices.run
        now = datetime.now(UTC)
        previous = run.slow.get(list_url)
        failures = 1 if progressed or previous is None else previous.failures + 1
        reason = "HTTP 504" if "HTTP 504" in str(error.result.error or "") else "Zeitüberschreitung"
        entry = SlowList(failures=failures, until=now + slow_backoff(failures), since=since, reason=reason)
        try:
            await self.storage.set_slow_list(run.source_url, list_url, entry.to_entry())
        except Exception as e:  # noqa: BLE001 - dann bleibt die Liste eine Lücke, der nächste Lauf fragt sie wieder an
            console.print(f"[yellow]  Aussetzung der Liste {list_url} nicht gespeichert ({e}), Lücke[/yellow]")
            return False
        run.slow[list_url] = entry
        slices.suspended[list_url] = entry.until
        note = (
            f"Liste {list_url} nach {reason} ausgesetzt bis {entry.until:%d.%m.%Y %H:%M} UTC "
            f"({failures}. Mal in Folge); Änderungen ab {since:%d.%m.%Y %H:%M} UTC holt der Ingestor danach nach"
        )
        slices.notes.append(note)
        console.print(f"[yellow]  {entity_type}: {note}[/yellow]")
        return True

    async def _clear_slow_list(self, slices: BodySlices, list_url: str) -> None:
        """Liste ganz gelesen: Aussetzung beenden (Eintrag in ``slow_lists`` löschen)."""
        run = slices.run
        if run.slow.pop(list_url, None) is None:
            return
        console.print(f"[green]  Liste {list_url} wieder ganz gelesen, Aussetzung beendet[/green]")
        try:
            await self.storage.set_slow_list(run.source_url, list_url, None)
        except Exception as e:  # noqa: BLE001 - dann holt der nächste Lauf die Liste noch einmal in Scheiben
            console.print(f"[yellow]  Ende der Aussetzung der Liste {list_url} nicht gespeichert: {e}[/yellow]")

    async def _slice_done(self, slices: BodySlices, list_url: str, window: SliceWindow, full: bool) -> None:
        """
        Scheibe ganz gelesen: Fortschritt auf ihr Ende setzen (Vollabgleich) bzw. den Fensteranfang der langsamen
        Liste. Die letzte Scheibe hat kein Ende; nach ihr ist die Liste fertig (``_slice_pages``).
        """
        if window.until is None:
            return
        run = slices.run
        try:
            if full:
                run.progress[list_url] = window.until
                await self.storage.set_full_sync_progress(run.source_url, list_url, window.until)
            elif (slow := run.slow.get(list_url)) is not None:
                slow.since = window.until
                await self.storage.set_slow_list(run.source_url, list_url, slow.to_entry())
        except Exception as e:  # noqa: BLE001 - dann beginnt der nächste Lauf bei der Scheibe davor
            console.print(f"[yellow]  Fortschritt der Liste {list_url} nicht gespeichert: {e}[/yellow]")

    async def _remember_filter_ignored(
        self, slices: BodySlices, list_url: str, window: SliceWindow, item: dict[str, Any], stamp: datetime
    ) -> None:
        """Befund „Quelle ignoriert den Filter“ für alle Listen dieses Laufs setzen und an der Quelle speichern."""
        run = slices.run
        bound = "modified_until" if window.until is not None and stamp > window.until else "modified_since"
        external_id = item.get("id") if isinstance(item, dict) else None
        detail = (
            f"Quelle ignoriert {bound}: Scheibe {window.label()} liefert {external_id or 'ein Objekt'} mit modified "
            f"{stamp.isoformat(timespec='seconds')}"
        )
        if run.filter_ignored is None:
            run.filter_ignored = detail
            console.print(f"[red]{list_url}: {detail}; Vollabgleich in Zeitscheiben bricht ab[/red]")
        finding = {
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "list": list_url,
            "detail": run.filter_ignored,
        }
        try:
            await self.storage.set_slice_filter_ignored(run.source_url, finding)
        except Exception as e:  # noqa: BLE001 - dann prüft der nächste Vollabgleich erneut
            console.print(f"[yellow]Befund zu Zeitscheiben nicht gespeichert: {e}[/yellow]")

    async def _settle_slices(self, slices: BodySlices, stats: dict[str, Any], full: bool) -> None:
        """
        Befund der Kommune zu Zeitscheiben und ausgesetzten Listen ins Ergebnis (Sync-Protokoll).

        Ist der Vollabgleich der Kommune vollständig (keine Lücke, keine ausgesetzte Liste), ist der Durchgang für jede
        Liste fertig, die ihre letzte Scheibe in diesem Lauf gelesen hat: Ihr Fortschritt wird gelöscht, der nächste
        Vollabgleich beginnt wieder bei ``since``. Mit Lücke oder ausgesetzter Liste bleibt jeder Fortschritt; fertige
        Listen fragen im nächsten Vollabgleich nur ihre letzte Scheibe noch einmal an.

        Eine im Vollabgleich ausgesetzte Liste (auch ohne Zeitscheiben) steht unter ``full_pending``: keine Lücke des
        Laufs, aber der Vollabgleich bleibt fällig (``full_sync_finished``).
        """
        stats["errors"].extend(slices.notes)
        if slices.suspended:
            stats["suspended_lists"] = {
                url: until.isoformat(timespec="seconds") for url, until in sorted(slices.suspended.items())
            }
            if full:
                stats["full_pending"] = sorted(slices.suspended)
        run = slices.run
        if not full or run.config is None:
            return
        if not stats["incomplete"] and not slices.suspended:
            for list_url in sorted(slices.finished):
                if run.progress.pop(list_url, None) is None:
                    continue
                try:
                    await self.storage.set_full_sync_progress(run.source_url, list_url, None)
                except Exception as e:  # noqa: BLE001 - dann setzt der nächste Vollabgleich an der letzten Grenze an
                    console.print(f"[yellow]  Fortschritt der Liste {list_url} nicht gelöscht: {e}[/yellow]")
        summary = slices.summary()
        stats["full_sync_slices"] = summary
        without = summary["without_modified"]
        if without:
            counts = ", ".join(f"{name}: {count}" for name, count in without.items())
            console.print(
                f"[yellow]  Zeitscheiben: Objekte ohne modified geliefert ({counts}); Objekte ohne modified, die die "
                f"Quelle in keiner Scheibe liefert, erfasst der Vollabgleich in Zeitscheiben nicht[/yellow]"
            )

    async def _settle_missing_lists(self, list_check: BodyListCheck, stats: dict[str, Any]) -> None:
        """
        Fehlende Listen eines Laufs vermerken (Issue #1025), wenn mindestens eine andere Liste der Kommune lesbar
        war; Pflichtlisten mit Fehlerobjekt gelten dann als leer (Warnung). Sonst gilt der Befund als Störung der
        Schnittstelle: Lücke im Abgleich, kein Vermerk.
        """
        if not list_check.missing and not list_check.required_errors:
            return
        if list_check.readable == 0:
            for list_url, reason in sorted({**list_check.missing, **list_check.required_errors}.items()):
                stats["errors"].append(f"Liste {list_url} nicht abrufbar ({reason}), keine Liste der Kommune lesbar")
            stats["incomplete"].append("lists")
            return
        if list_check.required_errors:
            stats["empty_required_lists"] = sorted(list_check.required_errors)
        for list_url, reason in sorted(list_check.required_errors.items()):
            console.print(
                f"[yellow]  Pflichtliste {list_url} antwortet mit Fehlerobjekt ({reason}): wie bisher als leer "
                f"gewertet[/yellow]"
            )
        if not list_check.missing:
            return
        stats["missing_lists"] = sorted(list_check.missing)
        for list_url, reason in sorted(list_check.missing.items()):
            if list_url not in list_check.lists.known:
                console.print(
                    f"[dim]  Liste {list_url} gibt es bei der Quelle nicht ({reason}): vermerkt, "
                    f"bis zur nächsten Prüfung (Vollabgleich, spätestens nach einem Tag) nicht mehr angefragt[/dim]"
                )
            await self._remember_missing_list(list_check.lists, list_url, missing=True)

    async def _remember_missing_list(self, missing_lists: MissingLists | None, list_url: str, missing: bool) -> None:
        """
        Vermerk „Liste gibt es nicht“ setzen (mit dem Zeitpunkt dieser Prüfung) oder löschen; Fehler gefährden den
        Abgleich nie. Angefragt wird eine vermerkte Liste nur bei der Prüfung, deshalb schreibt jede Prüfung.
        """
        if missing_lists is None or (not missing and list_url not in missing_lists.known):
            return
        checked_at = datetime.now(UTC)
        if missing:
            missing_lists.known[list_url] = checked_at
        else:
            del missing_lists.known[list_url]
            console.print(f"[green]  Liste {list_url} gibt es wieder, Vermerk gelöscht[/green]")
        try:
            await self.storage.set_list_missing(missing_lists.source_url, list_url, missing, checked_at=checked_at)
        except Exception as e:  # noqa: BLE001 - ohne Vermerk wird die Liste im nächsten Lauf erneut geprüft
            console.print(f"[yellow]  Vermerk zur Liste {list_url} nicht gespeichert: {e}[/yellow]")

    async def _advance_window(
        self,
        source_id: UUID,
        body_external_id: str,
        body_started: datetime,
        stats: dict[str, Any],
        item_check: BodyItemCheck,
    ) -> None:
        """
        Anfang des Änderungsfensters der Kommune nach dem Lauf festhalten (Issue #1025).

        - Lücke in einer Liste: stehen lassen.
        - Nicht gespeicherte Objekte: höchstens bis zu ihrem ältesten ``modified``, ohne lesbares ``modified`` gar
          nicht; der nächste Lauf fragt sie so wieder ab. Ausgenommen sind Objekte, die die Quelle trotz Filter
          liefert (``BodyItemCheck``).
        - Sonst: Beginn dieses Laufs.

        Nicht gespeicherte Objekte nennt das Log je Typ mit Anzahl und bis zu fünf Kennungen; je Quelle stehen sie
        im Sync-Log unter ``unstored``. So fallen dauerhaft nicht speicherbare Objekte auf, die das Fenster halten.
        """
        if item_check.unstored:
            stats["unstored"] = dict(sorted(item_check.unstored.items()))
            summary = ", ".join(f"{name}: {count}" for name, count in sorted(item_check.unstored.items()))
            unfiltered = item_check.unstored_before_window
            note = f"; {unfiltered} davon liefert die Quelle trotz Filter, sie halten es nicht" if unfiltered else ""
            console.print(
                f"[yellow]  Nicht gespeichert ({summary}): Das Fenster bleibt bei diesen Objekten{note}[/yellow]"
            )
            for name, ids in sorted(item_check.unstored_ids.items()):
                rest = item_check.unstored[name] - len(ids)
                more = f" und {rest} weitere" if rest > 0 else ""
                console.print(f"[yellow]    {name}: {', '.join(ids)}{more}[/yellow]")
        if stats["incomplete"]:
            return
        start = item_check.window_start(body_started)
        if start is None:
            return
        try:
            await self.storage.set_body_sync_started(source_id, body_external_id, start)
        except Exception as e:  # noqa: BLE001 - dann gilt beim nächsten Mal der bisherige Zeitpunkt
            console.print(f"[yellow]  Anfang des Änderungsfensters nicht gespeichert: {e}[/yellow]")

    async def _settle_timed_lists(
        self, timed_lists: TimedLists, item_check: BodyItemCheck, stats: dict[str, Any]
    ) -> None:
        """
        Listen vermerken, deren Änderungen der letzten sieben Tage alle eine Uhrzeit tragen, und Listen austragen,
        in denen eine solche Änderung nur das Datum trägt (``TimedLists``, Issue #1025). Fehler gefährden den
        Abgleich nie; ohne Vermerk gilt für die Liste das Fenster ab Tagesbeginn.
        """
        day_stamped = item_check.day_stamped_lists()
        if day_stamped:
            stats["day_stamped_lists"] = day_stamped
        changes = [(url, True) for url in item_check.timed_lists() if url not in timed_lists.known]
        changes += [(url, False) for url in day_stamped if url in timed_lists.known]
        for list_url, timed in changes:
            if timed:
                timed_lists.known.add(list_url)
                console.print(f"[dim]  Liste {list_url}: modified mit Uhrzeit, inkrementell mit kurzem Fenster[/dim]")
            else:
                timed_lists.known.discard(list_url)
                console.print(
                    f"[yellow]  Liste {list_url}: modified nur mit Datum, inkrementell ab Tagesbeginn[/yellow]"
                )
            try:
                await self.storage.set_list_timed(timed_lists.source_url, list_url, timed)
            except Exception as e:  # noqa: BLE001 - ohne Vermerk gilt das Fenster ab Tagesbeginn
                console.print(f"[yellow]  Vermerk zur Liste {list_url} nicht gespeichert: {e}[/yellow]")

    async def _store_entity(
        self,
        entity: ProcessedEntity,
        body_id: UUID,
        entity_type: str,
        body_name: str | None = None,
    ) -> bool:
        """Store a processed entity to the database.

        Returns True if the entity was actually stored, False if skipped
        (e.g. membership with unresolvable FK references).
        """
        if isinstance(entity, ProcessedMeeting):
            await self.storage.upsert_meeting(entity, body_id)
            await self._retract_session_location(entity.external_id)
            # Eingebettete TOPs und Dateien speichert upsert_meeting selbst; hier nur zählen
            for nested in entity.nested_entities:
                if isinstance(nested, ProcessedFile):
                    metrics.record_entity_synced("file", body_name or "unknown")
                elif isinstance(nested, ProcessedAgendaItem):
                    metrics.record_entity_synced("agendaitem", body_name or "unknown")
        elif isinstance(entity, ProcessedPaper):
            await self.storage.upsert_paper(entity, body_id)
            # Eingebettete Dateien und Beratungen speichert upsert_paper selbst; hier nur zählen
            for nested in entity.nested_entities:
                if isinstance(nested, ProcessedFile):
                    metrics.record_entity_synced("file", body_name or "unknown")
                elif isinstance(nested, ProcessedConsultation):
                    metrics.record_entity_synced("consultation", body_name or "unknown")
        elif isinstance(entity, ProcessedPerson):
            await self.storage.upsert_person(entity, body_id)
            metrics.record_entity_synced("person", body_name or "unknown")
            # Process nested memberships (OParl 1.0: embedded in Person objects)
            for nested in entity.nested_entities:
                if isinstance(nested, ProcessedMembership):
                    await self.storage.upsert_membership(nested, body_id)
                    metrics.record_entity_synced("membership", body_name or "unknown")
        elif isinstance(entity, ProcessedOrganization):
            await self.storage.upsert_organization(entity, body_id)
            metrics.record_entity_synced("organization", body_name or "unknown")
        elif isinstance(entity, ProcessedMembership):
            result = await self.storage.upsert_membership(entity, body_id)
            if result is None:
                return False  # FK resolution failed (person/org not found)
            metrics.record_entity_synced("membership", body_name or "unknown")
        elif isinstance(entity, ProcessedLocation):
            await self.storage.upsert_location(entity, body_id)
            metrics.record_entity_synced("location", body_name or "unknown")
        elif isinstance(entity, ProcessedAgendaItem):
            # AgendaItems need meeting_id - try to look it up
            meeting_id = None
            if entity.meeting_external_id:
                meeting_id = await self.storage.get_meeting_uuid(entity.meeting_external_id)
            if not meeting_id:
                # Sitzung nicht im Bestand: nicht gespeichert, der Lauf hält es fest (Issue #1025)
                return False
            await self.storage.upsert_agenda_item(entity, meeting_id)
            metrics.record_entity_synced("agendaitem", body_name or "unknown")
        elif isinstance(entity, ProcessedFile):
            # Files can belong to papers or meetings
            # Try to resolve paper_id from back-references (OParl spec: standalone files have these)
            paper_id = None
            meeting_id = None
            if entity.paper_external_ids:
                # Use first paper reference (files can belong to multiple papers)
                paper_id = await self.storage.get_paper_uuid(entity.paper_external_ids[0])
            if entity.meeting_external_ids:
                meeting_id = await self.storage.get_meeting_uuid(entity.meeting_external_ids[0])
            await self.storage.upsert_file(entity, body_id, paper_id=paper_id, meeting_id=meeting_id)
            metrics.record_entity_synced("file", body_name or "unknown")
        elif isinstance(entity, ProcessedConsultation):
            # Consultations can belong to papers - try to look it up
            paper_id = None
            if entity.paper_external_id:
                paper_id = await self.storage.get_paper_uuid(entity.paper_external_id)
            await self.storage.upsert_consultation(entity, body_id, paper_id)
            metrics.record_entity_synced("consultation", body_name or "unknown")
        else:
            await self.storage.upsert_entity(entity, body_id)

        # Record metrics for meetings and papers
        if isinstance(entity, ProcessedMeeting):
            metrics.record_entity_synced("meeting", body_name or "unknown")
        elif isinstance(entity, ProcessedPaper):
            metrics.record_entity_synced("paper", body_name or "unknown")

        return True

    # ========== Sync All Sources ==========

    async def sync_all(
        self,
        full: bool = False,
        parallel: bool = True,  # Default to parallel for better performance
    ) -> list[SyncResult]:
        """
        Sync all registered sources.

        Args:
            full: Whether to perform full sync
            parallel: Whether to sync sources in parallel (default: True)

        Returns:
            List of SyncResults for each source
        """
        sources = await self.storage.get_all_sources()

        if not sources:
            console.print("[yellow]No sources registered. Use 'add-source' first.[/yellow]")
            return []

        # Quellen in Schonung überspringen (Issue #89)
        now = datetime.now(UTC)
        due = []
        for source in sources:
            until = source_backoff_until(source, now)
            if until is None:
                due.append(source)
            else:
                reason = ERROR_KIND_LABELS.get(
                    getattr(source, "last_error_kind", None) or "",
                    f"{source.consecutive_failures} Fehlversuche in Folge",
                )
                console.print(
                    f"[yellow]Quelle {source.name}: {reason} — "
                    f"Schonung, nächster Versuch nach {until:%d.%m. %H:%M} UTC[/yellow]"
                )
        sources = due
        if not sources:
            return []

        console.print(f"[bold]Syncing {len(sources)} sources...[/bold]")

        if parallel:
            # Sync all sources in PARALLEL for massive speedup
            # Disable Rich Progress bars to avoid "Only one live display" error
            self._parallel_mode = True

            # Drossel für Quellen-Parallelität (Issue #22): begrenzt zusammen
            # mit sync_body_concurrency und sync_entity_concurrency die
            # Peak-Parallelität (Quellen x Bodies x Entity-Typen) und damit
            # das Speicherbudget eines Zyklus.
            source_semaphore = asyncio.Semaphore(max(1, settings.sync_source_concurrency))

            async def sync_source_wrapper(source):
                """Wrapper to catch exceptions per source."""
                async with source_semaphore:
                    try:
                        return await self.sync_body_url(source.url, full=full)
                    except Exception as e:
                        console.print(f"[red]Error syncing {source.name}: {e}[/red]")
                        return SyncResult(
                            source_url=source.url,
                            source_name=source.name,
                            success=False,
                            errors=[str(e)],
                        )

            try:
                results = await asyncio.gather(
                    *[sync_source_wrapper(source) for source in sources], return_exceptions=False
                )
                return list(results)
            finally:
                self._parallel_mode = False
                # FK-UUID-Caches nach jedem Zyklus leeren (Issue #22):
                # verhindert monotones Wachstum über die Daemon-Laufzeit.
                self.storage.clear_uuid_caches()
        else:
            # Sequential sync (fallback)
            results = []
            try:
                for source in sources:
                    result = await self.sync_body_url(source.url, full=full)
                    results.append(result)
            finally:
                self.storage.clear_uuid_caches()
            return results

    # ========== Status & Statistics ==========

    async def get_status(self) -> dict[str, Any]:
        """Get current sync status and statistics."""
        stats = await self.storage.get_stats()

        return {
            "database_stats": stats,
            "status": "ready",
        }

    def print_result(self, result: SyncResult) -> None:
        """Print a sync result summary."""
        console.print("\n[bold]" + "=" * 60 + "[/bold]")
        console.print(f"[bold]Sync Result: {result.source_name}[/bold]")
        console.print("[bold]" + "=" * 60 + "[/bold]")

        status = "[green]SUCCESS[/green]" if result.success else "[red]FAILED[/red]"
        console.print(f"Status: {status}")
        console.print(f"Duration: {result.duration_seconds:.1f}s")

        console.print("\n[bold]Entities Synced:[/bold]")
        console.print(f"  Bodies:        {result.bodies_synced:,}")
        console.print(f"  Organizations: {result.organizations_synced:,}")
        console.print(f"  Persons:       {result.persons_synced:,}")
        console.print(f"  Memberships:   {result.memberships_synced:,}")
        console.print(f"  Meetings:      {result.meetings_synced:,}")
        console.print(f"  Papers:        {result.papers_synced:,}")
        console.print(f"  Locations:     {result.locations_synced:,}")
        console.print(f"  AgendaItems:   {result.agenda_items_synced:,}")
        console.print(f"  Files:         {result.files_synced:,}")
        console.print(f"  Consultations: {result.consultations_synced:,}")

        if result.http_stats:
            console.print("\n[bold]HTTP Statistics:[/bold]")
            console.print(f"  Requests:    {result.http_stats.http_requests:,}")
            console.print(f"  Cache Hits:  {result.http_stats.cache_hits:,}")
            console.print(f"  HTTP Time:   {result.http_stats.http_time:.1f}s")
            if result.http_stats.http_requests > 0:
                avg = result.http_stats.http_time / result.http_stats.http_requests
                console.print(f"  Avg/Request: {avg * 1000:.0f}ms")

        if result.errors:
            console.print(f"\n[red]Errors ({len(result.errors)}):[/red]")
            for error in result.errors[:10]:
                console.print(f"  - {error}")
            if len(result.errors) > 10:
                console.print(f"  ... and {len(result.errors) - 10} more")

    @staticmethod
    def _finding_details(result: SyncResult) -> dict[str, Any]:
        """Fehlerklasse und Host-Statistik für die Sync-Log-Details (nur bei Befund)."""
        if not result.error_kind:
            return {}
        return {"error_kind": result.error_kind, "host_findings": result.host_findings}

    async def write_results_to_synclog(
        self,
        results: list[SyncResult],
        start_time: datetime,
        sync_type: str = "incremental",
        triggered_by: str = "daemon",
    ) -> None:
        """Schreibt aggregierte Sync-Ergebnisse in Django's SyncLog-Tabelle.

        ``start_time`` ohne Zeitzone gilt als Ortszeit des Prozesses (so entsteht sie mit
        ``datetime.now()``), nicht als UTC: Im Container mit ``TZ=Europe/Berlin`` läge der Start
        sonst ein bis zwei Stunden in der Zukunft und die Dauer wäre negativ (Issue #692).
        """
        end_time = datetime.now(UTC)
        start_time = start_time.astimezone(UTC)

        total_entities = 0
        all_errors: list[str] = []
        details: dict = {}

        for result in results:
            synced = (
                result.organizations_synced
                + result.persons_synced
                + result.memberships_synced
                + result.meetings_synced
                + result.papers_synced
                + result.files_synced
                + result.locations_synced
                + result.agenda_items_synced
                + result.consultations_synced
            )
            total_entities += synced
            all_errors.extend(result.errors)
            details[result.source_name or "Unknown"] = {
                "organizations": result.organizations_synced,
                "persons": result.persons_synced,
                "meetings": result.meetings_synced,
                "papers": result.papers_synced,
                "files": result.files_synced,
                **self._finding_details(result),
                **({"unstored": dict(sorted(result.unstored.items()))} if result.unstored else {}),
                **result.slice_details,
            }

        status = "success" if not all_errors or total_entities > 0 else "failed"
        duration = (end_time - start_time).total_seconds()

        try:
            await self.storage.write_sync_log(
                source_id=None,
                sync_type=sync_type,
                status=status,
                started_at=start_time,
                finished_at=end_time,
                duration_seconds=duration,
                entities_synced=total_entities,
                errors=all_errors[:20],
                details=details,
                triggered_by=triggered_by,
            )
            console.print(f"[dim]SyncLog geschrieben: {status}, {total_entities} Entitäten, {duration:.1f}s[/dim]")
        except Exception as e:
            console.print(f"[yellow]Warning: SyncLog konnte nicht geschrieben werden: {e}[/yellow]")
