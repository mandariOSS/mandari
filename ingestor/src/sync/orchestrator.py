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
import logging
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Mapping
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
    FetchResult,
    FetchWindowClosedError,
    ListFetchError,
    ListMissingError,
    OParlClient,
    SyncStats,
    unique_page_ids,
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
    SYNC_CONFIG_CONSULTATION_FETCHES_KEY,
    DatabaseStorage,
    ForeignObjectError,
    full_sync_progress_of,
    full_sync_resume_of,
    full_sync_slice_pages_of,
    legislative_term_lists_of,
    missing_list_reasons_of,
    missing_lists_of,
    slice_filter_ignored_of,
    slow_lists_of,
    start_foreign_rejections,
    start_unresolved_references,
    timed_lists_of,
)
from src.storage.events import start_correlation
from src.storage.ris_events import content_fingerprint
from src.sync import beratungen_nachladen
from src.sync.fortsetzung import CHANGES, FULL, RESUME, ResumeEntry, ResumeRun
from src.sync.gremien_kalender import SCHLUESSEL as GREMIUM_WEB
from src.sync.gremien_kalender import KalenderLauf, OhneGremium
from src.sync.gremien_kalender import ergaenzen as ergaenze_gremien
from src.sync.html_ergaenzung import FIELDS as ERGAENZUNG_FIELDS
from src.sync.html_ergaenzung import MARKER_KEY as ERGAENZUNG_MARKER
from src.sync.html_ergaenzung import HtmlErgaenzung, HtmlErgaenzungConfig
from src.sync.processor import OParlProcessor, session_location_id
from src.sync.slices import (
    FILTER_IGNORED_RECHECK,
    SLOW_LIST_STUCK_AFTER,
    BodySlices,
    SlicePage,
    SliceRun,
    SliceWindow,
    SlowList,
    day_floor,
    full_windows,
    slow_backoff,
    slow_windows,
)
from src.sync.wahlperioden import WahlperiodenListen, lies_liste

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
#: liefert; ein Fehlerobjekt gilt dort wie bisher als leere Liste (``BodyListCheck``, Issue #1025). Listen, die die
#: Quelle nicht freigibt (HTTP 403 für alle Clients), werden auch als Pflichtlisten vermerkt (Issue #1086).
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
    Issue #1025), und Listen, die sie nicht freigibt (HTTP 403 für alle Clients, auch Pflichtlisten, Issue #1086), mit
    dem Zeitpunkt ihrer letzten Prüfung und dem Grund.

    Gelesen aus ``sync_config["missing_lists"]`` beim Start des Abgleichs einer Quelle. Inkrementelle Läufe fragen
    eine vermerkte Liste erst wieder an, wenn ihre letzte Prüfung einen Tag zurückliegt (``MISSING_LIST_RECHECK``),
    der Vollabgleich jedes Mal. Gibt es die Liste wieder, wird der Vermerk gelöscht. So bleibt eine Liste nach einer
    kurzen Störung höchstens einen Tag ausgeblendet, auch wenn der nächste Vollabgleich Wochen entfernt ist oder sein
    Zeitfenster endet, bevor er die Liste erreicht.
    """

    source_url: str
    known: dict[str, datetime | None] = field(default_factory=dict)
    #: Grund je vermerkter Liste, soweit bekannt (ältere Vermerke tragen nur den Zeitpunkt)
    reasons: dict[str, str] = field(default_factory=dict)

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
    #: Listen dieses Laufs, die es nicht gibt oder die die Quelle nicht freigibt (Adresse -> Grund)
    missing: dict[str, str] = field(default_factory=dict)
    #: Davon die Listen, die die Quelle nicht freigibt (HTTP 403 für alle Clients, Issue #1086)
    forbidden: set[str] = field(default_factory=set)
    #: Pflichtlisten dieses Laufs, deren erste Seite ein OParl-Fehlerobjekt ist (Adresse -> Grund). Sie gelten wie
    #: bisher als leer, solange eine andere Liste der Kommune lesbar war (OParl 1.0, more! rubin: „Requested class
    #: doesn't exist.“); sonst als Lücke. HTTP 404 auf einer Pflichtliste bleibt immer eine Lücke.
    required_errors: dict[str, str] = field(default_factory=dict)
    #: Listen dieses Laufs, deren erste Seite lesbar war
    readable: int = 0


def _note_missing_list(list_check: BodyListCheck, list_url: str, missing: ListMissingError, *, optional: bool) -> None:
    """
    Befund einer Liste, deren erste Seite fehlt, in ``list_check`` festhalten. Optionale Listen und Listen, die die
    Quelle nicht freigibt (HTTP 403 für alle Clients, Issue #1086), gelten als „nicht vorhanden“. Eine Pflichtliste
    mit HTTP 404 ist eine Lücke (``RequiredListMissingError``), mit Fehlerobjekt vorläufig leer (Issue #1025).
    """
    if optional or missing.forbidden:
        list_check.missing[list_url] = missing.reason
        if missing.forbidden:
            list_check.forbidden.add(list_url)
    elif missing.not_found:
        raise RequiredListMissingError(list_url, missing.reason) from missing
    else:
        list_check.required_errors[list_url] = missing.reason


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
#: Ein nicht gespeichertes Objekt hält das Änderungsfenster höchstens so viele Läufe ...
UNSTORED_HOLD_RUNS = 4
#: ... und mindestens so lange; danach ist es zurückgestellt (``settle_unstored_holds``)
UNSTORED_HOLD_MIN = timedelta(hours=12)
#: Zurückgestellte Objekte bleiben so lange nach dem letzten Lauf vermerkt, der sie nicht speichern konnte
UNSTORED_DEFERRED_KEEP = timedelta(days=30)
#: Höchstens so viele haltende Objekte je Kommune und Lauf verfolgt der Ingestor einzeln; weitere halten das Fenster
#: wie bisher, ohne Zurückstellung
UNSTORED_TRACKED_MAX = 200
#: Höchstens so viele Einträge je Kommune in ``sync_config["unstored_holds"]`` (die zuletzt gesehenen bleiben)
UNSTORED_HOLDS_MAX = 500


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

    Die übrigen haltenden Objekte stehen mit Kennung in ``holding``. Hält eines das Fenster seit mindestens
    ``UNSTORED_HOLD_RUNS`` Läufen und ``UNSTORED_HOLD_MIN``, stellt der Lauf es zurück (``settle_unstored_holds``):
    Es hält das Fenster nicht mehr, Log und Sync-Log nennen es. Mönchengladbach (ITK Rheinland) nennt in elf
    Mitgliedschaften Personen, die die Quelle nicht veröffentlicht (Einzelabruf HTTP 404); sie hielten das Fenster
    der Kommune seit dem 13.11.2026 fest.

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
    #: Haltende Objekte mit Kennung: Kennung -> (Typ, ``modified`` in UTC oder ``None``), höchstens
    #: ``UNSTORED_TRACKED_MAX``; nur sie kann ein Lauf zurückstellen
    holding: dict[str, tuple[str, datetime | None]] = field(default_factory=dict)
    #: Ältestes ``modified`` der haltenden Objekte ohne Kennung oder über der Grenze (UTC)
    oldest_untracked: datetime | None = None
    #: Ein haltendes Objekt ohne Kennung oder über der Grenze hatte kein lesbares ``modified``
    untracked_without_stamp: bool = False
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
        raw_id = record.get("id")
        external_id = raw_id if isinstance(raw_id, str) and raw_id else None
        ids = self.unstored_ids.setdefault(entity_type, [])
        if len(ids) < UNSTORED_IDS_LOGGED:
            ids.append(external_id or "ohne Kennung")
        stamp = _stamp(record.get("modified"))
        if stamp is not None:
            stamp = _as_utc(stamp)
            if window is not None and stamp < _as_utc(window) - UNSTORED_BEFORE_WINDOW:
                self.unstored_before_window += 1
                return
        # Das Objekt hält das Fenster
        if stamp is None:
            self.unstored_without_stamp = True
        elif self.oldest_unstored is None or stamp < self.oldest_unstored:
            self.oldest_unstored = stamp
        if external_id is not None and (external_id in self.holding or len(self.holding) < UNSTORED_TRACKED_MAX):
            earlier = self.holding.get(external_id)
            if earlier is not None and (earlier[1] is None or (stamp is not None and earlier[1] <= stamp)):
                return  # Dieselbe Kennung zweimal: Es gilt der Stand, der das Fenster weiter zurück hält
            self.holding[external_id] = (entity_type, stamp)
        elif stamp is None:
            self.untracked_without_stamp = True
        elif self.oldest_untracked is None or stamp < self.oldest_untracked:
            self.oldest_untracked = stamp

    def day_stamped_lists(self) -> list[str]:
        """Listen mit mindestens einer gelesenen Änderung, deren ``modified`` nur das Datum trägt."""
        return sorted(url for url, (day, _timed) in self.stamps.items() if day)

    def timed_lists(self) -> list[str]:
        """Listen, deren gelesene Änderungen alle eine Uhrzeit tragen."""
        return sorted(url for url, (day, timed) in self.stamps.items() if timed and not day)

    def window_start(self, started: datetime, deferred: Iterable[str] = ()) -> datetime | None:
        """
        Neuer Anfang des Änderungsfensters nach einem Lauf, dessen Listen ganz gelesen wurden: Beginn des Laufs,
        bei nicht gespeicherten Objekten höchstens ihr ältestes ``modified``; ``None`` = stehen lassen. Objekte in
        ``deferred`` (Kennungen, zurückgestellt) halten das Fenster nicht.
        """
        skipped = set(deferred)
        if self.untracked_without_stamp:
            return None
        stamps = [self.oldest_untracked] if self.oldest_untracked is not None else []
        for external_id, (_entity_type, stamp) in self.holding.items():
            if external_id in skipped:
                continue
            if stamp is None:
                return None
            stamps.append(stamp)
        return min([started, *stamps])


def _iso(stamp: datetime) -> str:
    return _as_utc(stamp).isoformat(timespec="seconds")


def _entry_stamp(entry: Mapping[str, Any], key: str) -> datetime | None:
    stamp = _stamp(entry.get(key))
    return _as_utc(stamp) if stamp is not None else None


def settle_unstored_holds(
    known: Mapping[str, Mapping[str, Any]], holding: Mapping[str, tuple[str, datetime | None]], now: datetime
) -> tuple[dict[str, dict[str, Any]], set[str], set[str]]:
    """
    Vermerk der nicht gespeicherten Objekte einer Kommune nach einem vollständigen Lauf fortschreiben
    (``sync_config["unstored_holds"]``, Kennung -> ``type``, ``since``, ``seen``, ``runs``, ``deferred``).

    - Ein haltendes Objekt zählt einen Lauf mehr (``runs``), ``since`` bleibt der erste Lauf, der es nicht speichern
      konnte. Ab ``UNSTORED_HOLD_RUNS`` Läufen und ``UNSTORED_HOLD_MIN`` seit ``since`` ist es zurückgestellt
      (``deferred``) und hält das Fenster nicht mehr, auch nicht in späteren Läufen (etwa im nächsten Vollabgleich).
    - Ein zurückgestelltes Objekt, das kein Lauf mehr sieht, bleibt ``UNSTORED_DEFERRED_KEEP`` nach dem letzten Mal
      vermerkt; übrige nicht mehr gesehene fallen weg (gespeichert oder nicht mehr geliefert).

    Nichts geht verloren: Die Quelle liefert ein zurückgestelltes Objekt im nächsten Vollabgleich wieder, ebenso
    nach jeder Änderung, und dann wird es gespeichert, sobald sich seine Verweise auflösen lassen.

    Rückgabe: neuer Vermerk, zurückgestellte Kennungen unter den haltenden, davon in diesem Lauf neu zurückgestellte.
    """
    entries: dict[str, dict[str, Any]] = {}
    deferred: set[str] = set()
    newly: set[str] = set()
    for external_id, (entity_type, _modified) in holding.items():
        prior = known.get(external_id) or {}
        since = _entry_stamp(prior, "since") or now
        runs = prior.get("runs")
        runs = (runs if isinstance(runs, int) and not isinstance(runs, bool) and runs > 0 else 0) + 1
        entry: dict[str, Any] = {"type": entity_type, "since": _iso(since), "seen": _iso(now), "runs": runs}
        deferred_at = _entry_stamp(prior, "deferred")
        if deferred_at is None and runs >= UNSTORED_HOLD_RUNS and now - since >= UNSTORED_HOLD_MIN:
            deferred_at = now
            newly.add(external_id)
        if deferred_at is not None:
            entry["deferred"] = _iso(deferred_at)
            deferred.add(external_id)
        entries[external_id] = entry
    for external_id, prior in known.items():
        if external_id in entries or _entry_stamp(prior, "deferred") is None:
            continue
        seen = _entry_stamp(prior, "seen")
        if seen is not None and now - seen <= UNSTORED_DEFERRED_KEEP:
            entries[external_id] = dict(prior)
    if len(entries) > UNSTORED_HOLDS_MAX:
        latest = sorted(entries, key=lambda key: str(entries[key].get("seen") or ""), reverse=True)
        entries = {key: entries[key] for key in latest[:UNSTORED_HOLDS_MAX]}
    return entries, deferred, newly


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


def outside_window_note(name: str, window: FullSyncWindow, now: datetime | None = None) -> str:
    """Meldung, wenn eine Quelle außerhalb ihres Abruffensters (``sync_window``) abgerufen werden soll (Issue #1186)."""
    opens = window.next_open(now).astimezone(window.tz)
    return (
        f"{name}: außerhalb des Abruffensters {window.label()}, nichts abgerufen; nächstes Fenster ab "
        f"{opens:%d.%m. %H:%M} Uhr"
    )


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
logger = logging.getLogger(__name__)


def end_with_current_task(task: asyncio.Task[Any]) -> None:
    """
    ``task`` endet spätestens mit der laufenden Aufgabe: Endet sie vorzeitig, wird ``task`` abgebrochen.

    Für die Texterkennung, die neben dem Abgleich einer Kommune läuft. Endet der Abgleich vorzeitig (Gesamtfrist des
    Laufs, Daemon stoppt, Quelle abgeschaltet), liefe sie sonst ohne Lauf weiter, und mit jedem Abbruch käme eine
    dazu. Am gewöhnlichen Ende ist sie schon fertig.
    """
    owner = asyncio.current_task()
    if owner is not None:
        owner.add_done_callback(lambda _done: task.cancel())


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
    # Davon zurückgestellt, sie halten das Änderungsfenster nicht mehr (``settle_unstored_holds``), im Sync-Log unter
    # ``unstored_deferred``
    unstored_deferred: Counter[str] = field(default_factory=Counter)
    # Abgewiesene Kennungen anderer Quellen je Art über alle Kommunen (Upsert, Löschung, Verweis), im Sync-Log unter
    # ``foreign`` (``start_foreign_rejections``)
    foreign: Counter[str] = field(default_factory=Counter)
    # Verweise, die sich im Bestand nicht auflösen ließen, je Art über alle Kommunen (Issue #1441), im Sync-Log unter
    # ``unresolved`` (``start_unresolved_references``)
    unresolved: Counter[str] = field(default_factory=Counter)
    # Lief der Abgleich als Vollabgleich? Außerhalb des Zeitfensters der Quelle wird aus einem angeforderten
    # Vollabgleich ein inkrementeller (``_full_sync_plan``); der Daemon protokolliert, was tatsächlich lief
    full: bool = False
    # Zeitscheiben und ausgesetzte Listen über alle Kommunen (Issue #1033), im Sync-Log unter ``full_sync_slices``
    # und ``suspended_lists`` (``merge_slice_details``)
    slice_details: dict[str, Any] = field(default_factory=dict)
    # sync --source --filter: Keine Kommune passt, nichts abgeglichen, die Quelle blieb unverändert (Issue #555)
    filter_without_match: bool = False
    # Außerhalb des Abruffensters der Quelle (``sync_window``, Issue #1186): kein Lauf, nichts abgerufen, die Quelle
    # blieb unverändert; die Meldung steht unter ``errors``
    outside_window: bool = False
    # Vollabgleich, der nicht alle Listen ganz gelesen hat (Zeitüberschreitung, Schutzschalter, Fensterende,
    # ausgesetzte Liste, gestörter Host): im Sync-Protokoll ``partial`` statt ``success`` (Issue #1523)
    full_incomplete: bool = False


#: Zustände im Sync-Protokoll (``insight_sync_synclog.status``, ``SyncLog.Status`` in Django)
SYNC_STATUS_SUCCESS = "success"
SYNC_STATUS_PARTIAL = "partial"
SYNC_STATUS_FAILED = "failed"


def sync_log_status(result: SyncResult) -> str:
    """
    Zustand eines Laufs im Sync-Protokoll (Issue #1523): ``failed`` ohne Erfolg, ``partial`` für einen Vollabgleich,
    der nicht alle Listen ganz gelesen hat, sonst ``success``. Früher stand ein Vollabgleich, den ein offener
    Schutzschalter oder eine Zeitüberschreitung abgebrochen hatte, als ``success`` im Protokoll und verdeckte die
    Lücke im Bestand. Ein inkrementeller Lauf bleibt ``success``; seine Lücken nennt er unter ``errors``.
    """
    if not result.success:
        return SYNC_STATUS_FAILED
    return SYNC_STATUS_PARTIAL if result.full_incomplete else SYNC_STATUS_SUCCESS


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


#: Höchstens so viele Kommunen nennt die Meldung, wenn der Filter nach Kommune nichts trifft
FILTER_BODIES_SHOWN = 10


def no_filter_match_note(body_filter: str, bodies: list[dict[str, Any]]) -> str:
    """Meldung, wenn ``sync --source --filter`` keine Kommune trifft, meist ein Tippfehler (Issue #555)."""
    shown = ", ".join(str(body.get("name") or body.get("id")) for body in bodies[:FILTER_BODIES_SHOWN])
    more = len(bodies) - FILTER_BODIES_SHOWN
    suffix = f" und {more} weitere" if more > 0 else ""
    return f"Keine Kommune passt zum Filter „{body_filter}“, nichts abgeglichen. Kommunen der Quelle: {shown}{suffix}"


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

    def _scraper_runner(self, source_row: Any) -> "ScraperSyncRunner | None":
        """
        ``ScraperSyncRunner`` für eine Scraper-Quelle (``sync_config["source_type"] = "scraper:<vendor>"``), sonst None.

        Scraper-Quellen laufen über den Runner statt über den OParl-Client; Zeitfenster und Mindestabstand des
        Vollabgleichs (``_full_sync_plan``) gelten für sie nur im geplanten Vollabgleich des Daemons, das Abruffenster
        (``sync_window``, Issue #1186) dagegen immer, mit Ende des Crawls am Fensterende. Zeitscheiben,
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
        body_filter: str | None = None,
    ) -> SyncResult:
        """
        Sync from any OParl URL (auto-detects Body, Body-List, or System).

        Einziger Einstieg für den Abgleich einer Quelle (Issue #554): Daemon, ``sync --source``, ``sync --body`` und
        ``sync --all`` kommen hierher. Scraper-Quellen (``sync_config["source_type"] = "scraper:<vendor>"``) gibt er
        an den ``ScraperSyncRunner`` ab.

        Args:
            url: OParl URL (Body, Body-List, or System endpoint)
            full: Whether to perform a full sync
            max_concurrent: Max concurrent HTTP requests
            default_window: Standardfenster des Daemons für Vollabgleiche; es begrenzt den Vollabgleich in
                Zeitscheiben einer Quelle ohne eigenes ``full_sync_window`` (Issue #1033)
            body_filter: nur Kommunen, deren Name (ohne Groß-/Kleinschreibung) oder Kennung den Text enthält
                (``sync --source --filter``); der Abgleich gilt dann nicht als vollständig. Trifft er keine Kommune,
                endet der Lauf ohne Abgleich und ohne Änderung an der Quelle (``filter_without_match``). Nicht für
                Scraper-Quellen.

        Außerhalb des Abruffensters der Quelle (``sync_config["sync_window"]``, Issue #1186) ruft der Lauf nichts ab,
        auf keinem Weg und ohne Übersteuerung: Er endet sofort mit ``outside_window`` und der Meldung unter ``errors``,
        ohne Änderung an der Quelle (kein Fehlversuch). Im Fenster endet jeder Abruf mit dem Fenster
        (``fetch_deadline``): danach keine weitere Seite, offene Listen bleiben Lücken.

        Returns:
            SyncResult with statistics
        """
        start_time = datetime.now(UTC)
        result = SyncResult(source_url=url, source_name="", success=False)
        source_row = await self.storage.get_source_by_url(url)
        self._register_id_base(url, source_row)
        options = SourceFetchOptions.from_sync_config(getattr(source_row, "sync_config", None))
        if options.sync_window is not None and options.outside_sync_window(start_time):
            result.source_name = str(getattr(source_row, "name", "") or url)
            result.outside_window = True
            # Die Meldung gibt der Aufrufer aus (Daemon, Kommandozeile)
            result.errors.append(outside_window_note(result.source_name, options.sync_window, start_time))
            return result
        runner = self._scraper_runner(source_row)
        if runner is not None:
            if body_filter:
                console.print(
                    "[yellow]Der Filter nach Kommune gilt nicht für Scraper-Quellen; abgeglichen wird die ganze "
                    "Quelle.[/yellow]"
                )
            return await runner.run(full=full, fetch_deadline=options.fetch_deadline(start_time))

        # Vermerke „filtert nicht“ dieser Quelle (Issue #1041), alte Einträge je Server übernommen
        filter_notes = self._filter_notes(url, source_row)
        full, fetch_deadline = self._full_sync_plan(url, options, full, source_row, default_window)
        full, slice_run, slice_note = await self._slice_run(
            url, options, full, getattr(source_row, "sync_config", None)
        )
        if slice_note is not None:
            fetch_deadline = None  # nur Änderungsabgleich: das Fenster des Vollabgleichs gilt nicht
            result.errors.append(slice_note)
        # Fortsetzung eines am Fensterende abgebrochenen Vollabgleichs (ohne Zeitscheiben, src/sync/fortsetzung.py)
        resume_run = (
            ResumeRun.build(
                url,
                full_sync_resume_of(getattr(source_row, "sync_config", None)),
                margin=options.modified_since_margin(),
                not_before=getattr(source_row, "last_successful_full_sync", None),
            )
            if full and slice_run.config is None
            else None
        )
        # Das Abruffenster der Quelle begrenzt jeden Lauf, auch den Änderungsabgleich (Issue #1186)
        fetch_deadline = options.fetch_deadline(start_time, fetch_deadline)
        if options.sync_window is not None and fetch_deadline is not None:
            console.print(
                f"[dim]{url}: Abruf bis {fetch_deadline.astimezone(options.sync_window.tz):%H:%M} Uhr "
                f"(Abruffenster {options.sync_window.label()})[/dim]"
            )
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

                # Filter nach Kommune (sync --source --filter). Ohne Treffer endet der Lauf, bevor er die Quelle
                # anfasst: Ein Tippfehler darf weder Name und Rohdaten noch den Fehlerzustand (Schonung) ändern.
                selected_bodies = bodies_data
                if body_filter:
                    selected_bodies = [
                        b
                        for b in bodies_data
                        if body_filter.lower() in b.get("name", "").lower() or body_filter in b.get("id", "")
                    ]
                    console.print(f"[dim]Filtered to {len(selected_bodies)} bodies[/dim]")
                    if not selected_bodies:
                        result.filter_without_match = True
                        result.errors.append(no_filter_match_note(body_filter, bodies_data))
                        return result

                # Ensure source exists in database (use URL as source identifier); Name und Rohdaten der Quelle wie im
                # Daemon, also aus allen Kommunen, auch mit Filter
                source_id = await self.storage.upsert_source(
                    url=url,
                    name=result.source_name,
                    raw_json=bodies_data[0] if len(bodies_data) == 1 else {"bodies_count": len(bodies_data)},
                    oparl_version=oparl_version,
                )

                bodies_data = selected_bodies

                # Sync bodies
                if len(bodies_data) > 1:
                    self._parallel_mode = True
                    console.print(f"[bold green]Starting PARALLEL sync of {len(bodies_data)} bodies...[/bold green]")

                # Bound per-body concurrency so multi-body sources don't
                # multiply peak memory (each body sync holds pages + caches).
                body_semaphore = asyncio.Semaphore(settings.sync_body_concurrency)
                source_config = getattr(source_row, "sync_config", None)
                missing_lists = MissingLists(
                    url, missing_lists_of(source_config), missing_list_reasons_of(source_config)
                )
                timed_lists = TimedLists(url, timed_lists_of(source_config))
                # Letzte Prüfung der Wahlperiodenlisten (Issue #1453)
                wahlperioden = WahlperiodenListen(url, legislative_term_lists_of(source_config))
                # Ergänzung aus Detailseiten (Issue #1431), ein Kontingent für alle Kommunen der Quelle
                ergaenzung = self._html_ergaenzung(url, source_config, client)
                # Namen nicht veröffentlichter Gremien aus dem öffentlichen Kalender (SessionNet, Münster): einmal
                # nach allen Kommunen der Quelle, ein Kontingent und eine Reihenfolge über alle ihre Monate
                kalender = KalenderLauf.fuer(source_config)

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
                                ergaenzung=ergaenzung,
                                resume=resume_run,
                                kalender=kalender,
                                wahlperioden=wahlperioden,
                            )
                        except Exception as e:
                            console.print(f"[red]Error syncing {body_data.get('name', 'Unknown')}: {e}[/red]")
                            return {"errors": [str(e)], "incomplete": ["body"]}

                body_results = await asyncio.gather(
                    *[sync_body_wrapper(bd) for bd in bodies_data],
                    return_exceptions=False,
                )
                if ergaenzung is not None:
                    console.print(f"[dim]{ergaenzung.summary()}[/dim]")
                if kalender is not None and kalender.kommunen:
                    await self._gremien_aus_kalender(kalender, client)

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
                    result.unstored_deferred.update(body_result.get("unstored_deferred") or {})
                    result.foreign.update(body_result.get("foreign") or {})
                    result.unresolved.update(body_result.get("unresolved") or {})
                    merge_slice_details(result.slice_details, body_result)

                # Update sync timestamp; vollständig nur ohne Lücke und ohne Filter auf Kommunen (Issue #556), der
                # Vollabgleich nur ohne ausgesetzte Liste (Issue #1033)
                bodies_complete = sync_complete(body_results, missed_bodies, client)
                full_finished = full_sync_finished(body_results)
                complete = not body_filter and bodies_complete
                full_complete = complete and full_finished
                # Vollabgleich mit Lücke: im Sync-Protokoll ``partial`` (Issue #1523); der Filter auf Kommunen zählt
                # dabei nicht als Lücke
                result.full_incomplete = full and not (bodies_complete and full_finished)
                await self.storage.update_source_sync_time(
                    source_id,
                    full_sync=full,
                    complete=complete,
                    full_complete=full_complete,
                )
                if resume_run is not None:
                    await self._settle_resume(resume_run, full_complete)
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

        except FetchWindowClosedError as e:
            # Fensterende schon beim Lesen der Kommunen: abgebrochen, aber kein Fehlversuch der Quelle (Issue #1186)
            result.errors.append(str(e))
            console.print(f"[yellow]Sync abgebrochen: {e}[/yellow]")
            if client is not None:
                self._collect_host_findings(result, client)
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
        if client.deadline_passed():
            return None  # Zeitfenster der Quelle abgelaufen: keine Anfrage mehr (Issue #1186)
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
            if verdict == MODIFIED_SINCE_INCONCLUSIVE and not client.deadline_passed():
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
        if full and not planned_full and options.full_sync_slices is not None and options.full_window() is None:
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
        run = SliceRun.build(
            url,
            config,
            full_sync_progress_of(sync_config),
            slow_lists_of(sync_config),
            full_sync_slice_pages_of(sync_config),
        )
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
        ergaenzung: HtmlErgaenzung | None = None,
        resume: ResumeRun | None = None,
        kalender: KalenderLauf | None = None,
        wahlperioden: WahlperiodenListen | None = None,
    ) -> dict[str, Any]:
        """
        Sync a single body and all its entities.

        Mit ``wahlperioden``: Die Liste ``legislativeTermList`` der Kommune kommt höchstens einmal am Tag, im
        Vollabgleich jedes Mal; ein Fehler dort ist keine Lücke (``src/sync/wahlperioden.py``, Issue #1453).

        Mit ``resume``: Fortsetzung eines am Fensterende abgebrochenen Vollabgleichs (``src/sync/fortsetzung.py``).
        Listen, die der Durchgang schon ganz gelesen hat, liest der Vollabgleich nur mit ihren Änderungen seit dem
        ersten Lauf des Durchgangs, abgebrochene nach diesen Änderungen ab ihrer Fortsetzungsseite.

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
        # Kennungen anderer Quellen, die der Speicher in diesem Abgleich abweist (Sync-Log ``foreign``)
        foreign = start_foreign_rejections()
        # Verweise, die sich in diesem Abgleich nicht auflösen lassen (Sync-Log ``unresolved``, Issue #1441)
        unresolved = start_unresolved_references()

        # Process and store body; eine Kommune einer anderen Quelle bricht hier ab (ForeignObjectError)
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

        # Optionale Listen, die die Quelle nicht angefragt haben will (``skip_lists``, Issue #1431)
        skip_lists = options.skip_lists if options is not None else frozenset()
        if skip_lists:
            console.print(f"[dim]  Listen nicht angefragt (skip_lists): {', '.join(sorted(skip_lists))}[/dim]")

        # Helper to build common kwargs for _sync_entity_type
        def sync_kwargs(list_url: str | None, entity_type: str) -> dict:
            return {
                "client": client,
                "list_url": None if entity_type in skip_lists else list_url,
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
                "ergaenzung": ergaenzung,
                "resume": resume,
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
            end_with_current_task(extraction_task)
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

        # Beratungen, die Punkte anstehender Sitzungen nennen und die es nicht gibt, einzeln nachladen (Issue #1523)
        await self._beratungen_nachladen(client, source_id, body_id, body_external_id, body_name, stats)
        terms_url = processed_body.legislative_term_list_url
        if wahlperioden is not None and terms_url and "legislativeterm" not in skip_lists:
            await self._sync_legislative_term_list(client, wahlperioden, terms_url, body_id, full, stats)

        if kalender is not None and "meeting" not in stats.get("incomplete", []):
            # Gremiennamen aus dem Kalender liest der Abgleich der Quelle nach allen Kommunen
            kalender.kommunen.append(body_id)
        if list_check is not None:
            await self._settle_missing_lists(list_check, stats)
        if body_slices is not None:
            await self._settle_slices(body_slices, stats, full)
        if unresolved:
            stats["unresolved"] = dict(sorted(unresolved.items()))
            summary = ", ".join(f"{name}: {count}" for name, count in sorted(unresolved.items()))
            console.print(f"[dim]  Verweise nicht auflösbar ({summary})[/dim]")
        if foreign:
            stats["foreign"] = dict(sorted(foreign.items()))
            summary = ", ".join(f"{name}: {count}" for name, count in sorted(foreign.items()))
            console.print(f"[yellow]  Kennungen anderer Quellen abgewiesen, nicht geschrieben ({summary})[/yellow]")

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
                FILE_TEXT_MAX_CHARS,
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

                    # Dateien mit extrahiertem Text: Seiten nach Bytes, Texte schon in der Datenbank gekürzt
                    async for files in self.storage.iter_files_with_text(
                        body_id, page_size, updated_since=updated_since, max_chars=FILE_TEXT_MAX_CHARS
                    ):
                        docs = [file_to_doc(f) for f in files]
                        await indexer.index_documents("files", docs)
                        indexed_total += len(docs)
                        # Texte nicht über das Laden der nächsten Seite hinaus festhalten
                        del files, docs

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
        *,
        body_id: UUID,
    ) -> bool:
        """
        Handle an OParl tombstone (deleted: true): mark the row as deleted
        (never physically delete) and queue its search document for removal.

        Markiert werden nur Objekte derselben Quelle wie die Kommune ``body_id`` des Abgleichs.

        Returns True if the entity was newly marked.
        """
        external_id = item.get("id", "")
        if not external_id:
            return False
        modified = self.processor.parse_datetime(item.get("modified"))
        if entity_type == "meeting":
            await self._retract_session_location(external_id, modified, body_id=body_id)
        entity_id = await self.storage.mark_entity_deleted(entity_type, external_id, modified=modified, body_id=body_id)
        if entity_id is None:
            return False
        index_name = self._ES_INDEX_BY_ENTITY_TYPE.get(entity_type)
        if index_name is not None and es_deletions is not None:
            es_deletions.setdefault(index_name, []).append(str(entity_id))
        return True

    async def _retract_session_location(
        self, meeting_external_id: str, modified: datetime | None = None, *, body_id: UUID
    ) -> None:
        """
        Eigenes Location-Objekt zum Ort einer Session-Sitzung zurücknehmen (``session_location_id``).

        Der Ort einer Session-Sitzung steht im Bestand als Text an der Sitzung. Ein eigenes Objekt unter
        der Kennung des Ortes kann nur ein älterer Stand des Ingestors angelegt haben; es darf weder die
        Rücknahme der Sitzung überdauern noch neben ihr veralten. Deshalb bei jeder Änderung und bei der
        Rücknahme der Sitzung markieren – für Sitzungen anderer Quellen geschieht nichts.
        """
        location_id = session_location_id(meeting_external_id)
        if location_id is not None:
            await self.storage.mark_entity_deleted("location", location_id, modified=modified, body_id=body_id)

    async def _sync_legislative_term_list(
        self,
        client: OParlClient,
        wahlperioden: WahlperiodenListen,
        list_url: str,
        body_id: UUID,
        full: bool,
        stats: dict[str, Any],
    ) -> None:
        """
        Wahlperioden aus ``legislativeTermList`` übernehmen (Issue #1453): leise abgerufen, Löschmarken übernommen,
        Ergebnis vermerkt. Ein Fehler zählt weder als Lücke noch als Befund am Host; der nächste Versuch kommt nach
        einem Tag bzw. mit dem nächsten Vollabgleich.
        """
        if not wahlperioden.faellig(list_url, full=full):
            return
        try:
            gelesen = await lies_liste(client, list_url)
        except Exception as e:  # noqa: BLE001 - optionale Liste: keine Lücke, nächster Versuch im nächsten Lauf
            console.print(f"[yellow]  Wahlperiodenliste nicht gelesen ({type(e).__name__}: {e})[/yellow]")
            return
        if not gelesen.geprueft:
            # Abruffenster vor der ersten Seite zu: keine Prüfung, also auch kein Vermerk
            console.print(f"[dim]  Wahlperiodenliste: {gelesen.ergebnis}[/dim]")
            return
        ergebnis = gelesen.ergebnis
        geschrieben = 0
        try:
            for eintrag in gelesen.eintraege:
                try:
                    if eintrag.get("deleted") is True:
                        modified = self.processor.parse_datetime(eintrag.get("modified"))
                        markiert = await self.storage.mark_entity_deleted(
                            "legislativeterm", eintrag["id"], modified=modified, body_id=body_id
                        )
                        geschrieben += markiert is not None
                    else:
                        term = self.processor.process_legislative_term(eintrag)
                        await self.storage.upsert_legislative_term(term, body_id)
                        geschrieben += 1
                except ForeignObjectError:
                    # Kennung einer anderen Quelle: nicht geschrieben, gezählt (Sync-Log ``foreign``)
                    continue
        except Exception as e:  # noqa: BLE001 - optionale Liste: keine Lücke; vermerkt wird trotzdem (Prüfabstand)
            ergebnis = f"{ergebnis}, Übernahme abgebrochen ({type(e).__name__})"
            console.print(f"[yellow]  Wahlperiodenliste nicht ganz übernommen ({type(e).__name__}: {e})[/yellow]")
        stats["legislative_terms"] = geschrieben
        try:
            await self.storage.note_legislative_term_list(wahlperioden.source_url, list_url, ergebnis)
        except Exception as e:  # noqa: BLE001 - ohne Vermerk prüft der nächste Lauf die Liste erneut
            console.print(f"[yellow]  Prüfung der Wahlperiodenliste nicht vermerkt ({type(e).__name__})[/yellow]")
        console.print(f"[dim]  Wahlperiodenliste: {ergebnis}, {geschrieben} übernommen[/dim]")

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
        ergaenzung: HtmlErgaenzung | None = None,
        resume: ResumeRun | None = None,
        changes_since: datetime | None = None,
    ) -> int:
        """
        Sync all entities of a specific type.

        ``changes_since`` im Vollabgleich: Nachführung einer Liste im Durchgang der Fortsetzung. Die Quelle filtert mit
        ``modified_since``, verarbeitet wird wie im Vollabgleich: jedes gelieferte Objekt, ohne Abbruch nach Seiten ohne
        Änderung (eine Korrektur mit gleichem ``modified`` ginge sonst verloren).

        Mit ``resume`` im Vollabgleich ohne Zeitscheiben (``src/sync/fortsetzung.py``): Hat der Durchgang die Liste
        schon ganz gelesen, kommen nur ihre Änderungen seit dem ersten Lauf des Durchgangs (``ResumeRun.since``) wie im
        inkrementellen Lauf; brach ein früherer Lauf sie am Fensterende ab, zuerst diese Änderungen, dann die Liste ab
        der zuletzt gelesenen Seite. Scheitert der Änderungsabgleich oder hat sich die Liste verschoben, kommt sie ganz.
        Endet das Fenster mitten in der Liste, merkt sich der Lauf die Seite; liest er sie ganz, gilt sie im Durchgang
        als gelesen, nicht aber eine Liste, die es bei der Quelle nicht gibt.

        Optionale Listen, die es bei der Quelle nicht gibt (``list_check``, Issue #1025), und Listen, die sie nicht
        freigibt (Issue #1086), fragt der inkrementelle Lauf einmal am Tag an, der Vollabgleich jedes Mal (in
        Zeitscheiben nicht). Nicht gespeicherte Objekte und die Genauigkeit von
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
        # Fortsetzung eines abgebrochenen Vollabgleichs: ganz, ab der Fortsetzungsseite oder nur die Änderungen
        resume_page: str | None = None
        carried = 0
        track_resume = full and resume is not None and not sliced_full
        if track_resume and resume is not None:
            how, entry = resume.plan(list_url)
            since = resume.since(list_url)
            if how in (CHANGES, RESUME) and since is not None:
                was = (
                    "im Durchgang schon ganz gelesen, nur Änderungen"
                    if how == CHANGES
                    else "Änderungen der gelesenen Seiten"
                )
                console.print(f"[dim]  {entity_type}: {was} seit {since.isoformat()}[/dim]")
                try:
                    carried = await self._sync_entity_type(
                        client,
                        list_url,
                        entity_type,
                        body_id,
                        body_external_id,
                        body_name=body_name,
                        full=True,
                        changes_since=since,
                        es_deletions=es_deletions,
                        list_check=list_check,
                        item_check=item_check,
                        timed_lists=timed_lists,
                        slices=slices,
                        ergaenzung=ergaenzung,
                    )
                except FetchWindowClosedError:
                    raise
                except ListFetchError as error:
                    # Die Stadt Köln lehnte gefilterte Folgeseiten zeitweise mit HTTP 401 ab, ganze Listen nicht: Der
                    # Durchgang bliebe sonst an dieser Liste hängen. Sie kommt in diesem Lauf ganz.
                    console.print(
                        f"[yellow]  {entity_type}: Änderungsabgleich der Liste gescheitert ({error.result.error}), "
                        f"Liste ganz[/yellow]"
                    )
                    how, carried = FULL, 0
                    resume.discard(list_url)
                else:
                    if how == CHANGES:
                        return carried
            # Verschiebung direkt vor dem Weiterlesen prüfen, nach der Nachführung: Was die Quelle bis dahin ändert,
            # soll die Prüfung noch sehen
            if how == RESUME and entry is not None and await self._resume_shifted(client, entry):
                console.print(
                    f"[yellow]  {entity_type}: Liste hat sich seit dem Abbruch um mehr als eine Seite verschoben "
                    f"({entry.page}), Liste ganz[/yellow]"
                )
                how = FULL
                resume.discard(list_url)
            if how == RESUME and entry is not None and entry.page is not None:
                resume_page = entry.page
                console.print(f"[dim]  {entity_type}: Fortsetzung des Vollabgleichs ab {resume_page}[/dim]")
        # Vermerkt sind optionale Listen, die es nicht gibt, und Listen, die die Quelle nicht freigibt (HTTP 403 für
        # alle Clients); nur so stehen auch Pflichtlisten hier (Issue #1086)
        if not full and list_check is not None and list_url in list_check.lists.known:
            if list_check.lists.skip(list_url):
                reason = list_check.lists.reasons.get(list_url) or "fehlt bei der Quelle"
                console.print(f"[dim]  {entity_type}: Liste vermerkt ({reason}), nicht angefragt[/dim]")
                return 0
            # Tägliche Prüfung der vermerkten Liste: Gibt es sie wieder, kommen die Änderungen aus der Zeit des
            # Vermerks mit, so weit wie bisher (sieben Tage), nicht nur die seit dem letzten Lauf
            if modified_since is not None:
                modified_since = fallback_modified_since()
        if sliced_full and list_check is not None and list_url in list_check.lists.known:
            # SD.NET RIM beantwortet jede leere Scheibe wie eine fehlende Liste: Die Prüfung bleibt beim täglichen
            # inkrementellen Lauf, statt hunderte Scheiben einer Liste anzufragen, die es nicht gibt. Ebenso bei
            # einer Liste, die die Quelle nicht freigibt: Eine Scheibe fragt nur mit Filter, ein 403 darauf sagt
            # nicht, ob die Liste ohne Filter käme (Issue #1086)
            reason = list_check.lists.reasons.get(list_url) or "fehlt bei der Quelle"
            console.print(f"[dim]  {entity_type}: Liste vermerkt ({reason}), keine Zeitscheiben[/dim]")
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
        if changes_since is not None and timed_lists is not None and not timed:
            changes_since = day_start_window(changes_since)

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
        server_modified_since = modified_since if not full else changes_since
        if slow_since is not None:
            server_modified_since = slow_since  # langsame Liste: ab ihrem eigenen Fensteranfang

        pages: AsyncIterator[list[dict[str, Any]]]
        if windows is not None and slices is not None:
            pages = self._slice_pages(
                client, list_url, entity_type, windows, list_check, slices, full=full, optional=optional
            )
        else:
            pages = self._list_pages(
                client, list_url, server_modified_since, list_check, optional=optional, next_page=resume_page
            )
            if slices is not None and optional:
                # Bricht die Liste nach einer Zeitüberschreitung ab, wird sie ausgesetzt (Issue #1033). Ihr
                # Fensteranfang: im Vollabgleich der bisherige oder sieben Tage, sonst das Fenster dieses Laufs
                if server_modified_since is not None:
                    keep_since = _as_utc(server_modified_since)
                else:
                    keep_since = slow.since if slow is not None else fallback_modified_since().replace(tzinfo=UTC)
                pages = self._slow_guard(pages, slices, list_url, entity_type, keep_since)
        if track_resume and resume is not None:
            pages = self._resume_guard(pages, resume, list_url)

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
                    await self.storage.get_person_ids_by_external_ids(list(person_refs), body_id)
                if org_refs:
                    await self.storage.get_organization_ids_by_external_ids(list(org_refs), body_id)

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
                            if await self._mark_deleted(item, entity_type, es_deletions, body_id=body_id):
                                deleted_count += 1
                            continue
                        if item_check is not None:
                            item_check.note_stamp(list_url, item)
                        # Content-Hash mitschreiben, damit der nächste inkrementelle
                        # Lauf unveränderte Objekte ohne Zeitstempel erkennt (#122)
                        if timestamps_unreliable(item):
                            item[CONTENT_HASH_FIELD] = content_fingerprint(item)
                        await self._ergaenze(ergaenzung, entity_type, item)
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
                            if await self._mark_deleted(item, entity_type, es_deletions, body_id=body_id):
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
                            await self._ergaenze(ergaenzung, entity_type, item)
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
                            await self._ergaenze(ergaenzung, entity_type, item)
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
                            await self._ergaenze(ergaenzung, entity_type, item)
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
                elif unchanged_on_page > 0:
                    # Nur Seiten mit Bekanntem zählen. Eine Seite allein aus Löschsignalen unbekannter Kennungen sagt
                    # nichts über die Liste: Die Stadt Münster stellt gelöschte Beratungen als Platzhalter
                    # ``…/consultations/0`` seitenweise vor die echten Änderungen (Issue #1523)
                    consecutive_stale_pages += 1

                # Safety stop: 5 consecutive pages without changes (nicht in Zeitscheiben: jede hat ihren Zeitraum;
                # nach einem Rückfall auf das Lesen ohne Scheiben wieder). Nicht bei einer Liste, die die Quelle
                # nachweislich filtert: Jede ihrer Seiten ist schon eine Änderung seit dem Fensteranfang, und hinter
                # Seiten mit Bekanntem (Fenster, das ein nicht gespeichertes Objekt festhält) stehen die neuen Einträge
                unsliced = windows is None or (slices is not None and list_url in slices.unsliced)
                # Nachweis des Filters erst hier prüfen: ``fetch_list`` kann den Vermerk „filtert nicht“ im Lauf löschen
                stale_stop = server_modified_since is None or not client.filter_proven(list_url)
                if consecutive_stale_pages >= stale_pages_to_stop and unsliced and stale_stop:
                    console.print(
                        f"[yellow]  Stopping {entity_type}: "
                        f"{consecutive_stale_pages} consecutive pages without changes[/yellow]"
                    )
                    if slices is not None and optional:
                        # Bis hierher gelesen gilt die Liste als aktuell: Eine Aussetzung endet (Issue #1033)
                        await self._clear_slow_list(slices, list_url)
                    break

        # Ganz gelesen: im Durchgang erledigt. Nicht eine ausgesetzte langsame Liste und keine, die bei der Quelle fehlt
        # (404 oder Fehlerobjekt auf der ersten Seite): Ist die Schnittstelle nur gestört, bliebe sie sonst bis zum
        # Ende des Durchgangs ungelesen
        suspended = slices is not None and list_url in slices.suspended
        absent = list_check is not None and (list_url in list_check.missing or list_url in list_check.required_errors)
        if track_resume and resume is not None and not suspended and not absent:
            resume.note_finished(list_url)

        total = count + updated_count + carried
        if not full and (count > 0 or updated_count > 0 or deleted_count > 0 or skipped_count > 0):
            console.print(
                f"[dim]  {entity_type} summary: {count} new, {updated_count} updated, "
                f"{deleted_count} deleted, {skipped_count} unchanged[/dim]"
            )

        return total

    def _html_ergaenzung(self, url: str, sync_config: Any, client: OParlClient) -> HtmlErgaenzung | None:
        """Ergänzung aus Detailseiten für diesen Lauf, wenn die Quelle sie eingeschaltet hat (Issue #1431)."""
        config = HtmlErgaenzungConfig.parse(sync_config)
        if config is None:
            return None
        storage = self.storage

        async def load_stored(entity_type: str, external_ids: list[str]) -> dict[str, dict[str, Any]]:
            return await storage.get_html_ergaenzung(
                entity_type, external_ids, ERGAENZUNG_FIELDS[entity_type], ERGAENZUNG_MARKER
            )

        console.print(f"[dim]{url}: Ergänzung aus Detailseiten ({config.art}), bis {config.pages_per_run} Seiten[/dim]")
        return HtmlErgaenzung(
            config,
            hosts=[urlparse(url).netloc],
            fetch_page=client.fetch_page,
            load_stored=load_stored,
            deadline_passed=client.deadline_passed,
        )

    async def _beratungen_nachladen(
        self,
        client: OParlClient,
        source_id: UUID,
        body_id: UUID,
        body_external_id: str,
        body_name: str | None,
        stats: dict[str, Any],
    ) -> None:
        """
        Unbekannte Beratungen der Sitzungen um heute einzeln laden (``src/sync/beratungen_nachladen.py``, Issue
        #1523). Gespeichert wird wie im Abgleich; gezählt in ``stats`` (``nachgeladen``), damit der Suchindex sie
        aufnimmt. Fehler halten den Abgleich nicht auf.
        """
        try:
            sync_config = await self.storage.source_sync_config(source_id)
            budget = beratungen_nachladen.kontingent(sync_config)
            if budget <= 0 or client.deadline_passed():
                return
            jetzt = datetime.now(UTC)
            kandidaten = await self.storage.unknown_consultations_of_meetings(
                body_id, jetzt - beratungen_nachladen.ZURUECK, jetzt + beratungen_nachladen.VORAUS
            )
            if not kandidaten:
                return
            versuche = sync_config.get(SYNC_CONFIG_CONSULTATION_FETCHES_KEY)
            reihenfolge = beratungen_nachladen.faellig(
                kandidaten,
                versuche if isinstance(versuche, dict) else {},
                urlparse(body_external_id).netloc,
                jetzt,
            )
            if not reihenfolge:
                return

            async def abrufen(url: str) -> FetchResult:
                return await client.fetch(url, use_cache=False)

            async def vorlage_bekannt(url: str) -> bool:
                return await self.storage.get_paper_uuid(url, body_id) is not None

            async def speichern(entity_type: str, daten: dict[str, Any]) -> bool:
                processed = self.processor.process(daten, body_external_id)
                if not processed:
                    return False
                return await self._store_entity(processed, body_id, entity_type, body_name)

            ergebnis, gescheitert = await beratungen_nachladen.nachladen(
                reihenfolge,
                kontingent=budget,
                jetzt=jetzt,
                abrufen=abrufen,
                vorlage_bekannt=vorlage_bekannt,
                speichern=speichern,
                weiter=lambda: not client.deadline_passed(),
            )
        except Exception as e:  # noqa: BLE001 - das Nachladen ergänzt nur, der Abgleich bleibt gültig
            console.print(f"[yellow]  Nachladen von Beratungen gescheitert: {e}[/yellow]")
            return
        # Zuerst zählen: Was gespeichert ist, gehört in den Suchindex, auch wenn das Merken der Versuche scheitert
        stats["consultations"] = stats.get("consultations", 0) + ergebnis.beratungen
        stats["papers"] = stats.get("papers", 0) + ergebnis.vorlagen
        stats["nachgeladen"] = ergebnis.als_dict()
        if gescheitert:
            try:
                await self.storage.note_consultation_fetches(
                    source_id, gescheitert, keep_after=jetzt - 2 * beratungen_nachladen.ERNEUT_NACH
                )
            except Exception as e:  # noqa: BLE001 - ohne Vermerk kommt die Beratung im nächsten Lauf wieder
                console.print(f"[yellow]  Versuche des Nachladens nicht gespeichert: {e}[/yellow]")
        console.print(
            f"[dim]  Beratungen nachgeladen: {ergebnis.beratungen} (Vorlagen {ergebnis.vorlagen}, gescheitert "
            f"{ergebnis.fehlgeschlagen}, zurückgestellt {ergebnis.zurueckgestellt}, {ergebnis.abrufe} Abrufe)"
            + (f", beendet: {ergebnis.abbruch}" if ergebnis.abbruch else "")
            + "[/dim]"
        )

    async def _gremien_aus_kalender(self, kalender: KalenderLauf, client: OParlClient) -> None:
        """
        Namen nicht veröffentlichter Gremien aus dem Kalender ergänzen, einmal je Quelle für alle Kommunen mit
        vollständig abgeglichenen Sitzungen (``src/sync/gremien_kalender.py``).
        """
        storage = self.storage
        kommunen = list(kalender.kommunen)

        async def lade(von: datetime, bis: datetime, erneut_ab: datetime) -> list[OhneGremium]:
            zeilen = await storage.get_meetings_ohne_gremium(kommunen, von, bis, erneut_ab, GREMIUM_WEB)
            return [OhneGremium(id=z[0], external_id=z[1], start=z[2], stand=z[3], geprueft=z[4]) for z in zeilen]

        async def speichere(sitzung: OhneGremium, wert: dict[str, str]) -> bool:
            return await storage.set_meeting_raw_extension(sitzung.id, GREMIUM_WEB, wert, stand=sitzung.stand)

        try:
            ergebnis = await ergaenze_gremien(
                kalender.config, lade, client.fetch_page, speichere, frist_vorbei=client.deadline_passed
            )
        except Exception as exc:  # noqa: BLE001 - der Abgleich der Quelle ist davon unabhängig
            console.print(f"[yellow]  Gremien aus dem Kalender übersprungen: {exc}[/yellow]")
            return
        if ergebnis.benannt or ergebnis.fehlgeschlagen or ergebnis.ohne_eintrag or ergebnis.verschoben:
            console.print(f"[dim]  Gremien aus dem Kalender: {ergebnis.as_dict()}[/dim]")

    @staticmethod
    async def _ergaenze(ergaenzung: HtmlErgaenzung | None, entity_type: str, item: dict[str, Any]) -> None:
        """Objekt aus der Detailseite ergänzen; ein Fehler dabei hält das Objekt nicht auf (Issue #1431)."""
        if ergaenzung is None:
            return
        try:
            await ergaenzung.ergaenze(entity_type, item)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]  Ergänzung für {item.get('id', '?')} übersprungen: {exc}[/yellow]")

    async def _list_pages(
        self,
        client: OParlClient,
        list_url: str,
        modified_since: datetime | None,
        list_check: BodyListCheck | None,
        *,
        optional: bool = True,
        next_page: str | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Seiten einer Liste; gibt es eine optionale Liste bei der Quelle nicht, keine Seite (Issue #1025).

        ``next_page``: Fortsetzung eines am Fensterende abgebrochenen Vollabgleichs ab dieser Seite
        (``src/sync/fortsetzung.py``). Die Liste gab es dann schon; die Prüfung auf fehlende Listen entfällt.

        HTTP 404 oder ein Fehlerobjekt auf der ersten Seite (ohne Filter) zählt bei Listen, die OParl 1.1 als
        optional führt (``OPTIONAL_LIST_TYPES``), als „nicht vorhanden“, nicht als Lücke: Die Liste landet in
        ``list_check`` und wird am Ende des Laufs in ``sync_config["missing_lists"]`` vermerkt
        (``_settle_missing_lists``); inkrementell fragt sie erst die tägliche Prüfung wieder an, der Vollabgleich
        jedes Mal. Wird eine vermerkte Liste ganz gelesen, wird der Vermerk gelöscht; meldet SD.NET RIM auf den
        Filter nur „keine Treffer“, bleibt er (Issue #1034).

        Pflichtlisten werden als fehlend nie vermerkt. HTTP 404 ist dort eine Lücke (``RequiredListMissingError``).
        Ein Fehlerobjekt gilt wie bisher als leere Liste, solange eine andere Liste der Kommune lesbar war; sonst als
        Lücke (``_settle_missing_lists``). Ohne ``list_check`` wie bisher.

        Gibt die Quelle eine Liste nicht frei (HTTP 403 für alle Clients, ``ListMissingError.forbidden``), gilt sie
        wie eine fehlende optionale Liste als „nicht vorhanden“, auch eine Pflichtliste (Issue #1086).
        """
        if next_page is not None:
            async for page in client.fetch_list(list_url, next_page=next_page):
                yield page
            if list_check is not None:
                list_check.readable += 1
            return
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
            _note_missing_list(list_check, list_url, missing, optional=optional)
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

        Im Vollabgleich setzt eine Scheibe, die ein früherer Lauf mittendrin abbrach (Fensterende, Zeitüberschreitung,
        Schutzschalter), an der zuletzt ganz gelesenen Seite fort (``SlicePage``, Issue #1523); bricht sie wieder ab,
        rückt dieser Stand vor.
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
                # Im Vollabgleich ab der zuletzt ganz gelesenen Seite einer abgebrochenen Scheibe (Issue #1523)
                async for page in self._window_pages(
                    client,
                    slices,
                    list_url,
                    entity_type,
                    window,
                    full=full,
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
                    _note_missing_list(list_check, list_url, missing, optional=optional)
                    return
            except ListFetchError as error:
                # Rückt der Seitenstand in der Scheibe vor, kommt die Liste voran, auch ohne ganz gelesene Scheibe
                seite_vorher = run.pages.get(list_url) if full else None
                if full:
                    await self._remember_slice_page(slices, list_url, window, error)
                seite_jetzt = run.pages.get(list_url) if full else None
                seite_vorgerueckt = seite_jetzt is not None and (
                    seite_vorher is None or seite_jetzt.page != seite_vorher.page
                )
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
                        slices, list_url, entity_type, error, keep_since, progressed=index > 0 or seite_vorgerueckt
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
            if full:
                await self._clear_slice_page(slices, list_url)
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

    async def _window_pages(
        self,
        client: OParlClient,
        slices: BodySlices,
        list_url: str,
        entity_type: str,
        window: SliceWindow,
        *,
        full: bool,
        report_missing: bool,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Seiten einer Scheibe. Im Vollabgleich setzt eine Scheibe, die ein früherer Lauf abgebrochen hat, an der zuletzt
        ganz gelesenen Seite fort (Issue #1523). Geprüft wird die tatsächlich gelesene Fortsetzungsseite, bevor sie
        verarbeitet wird: Trägt sie keine der gemerkten Kennungen mehr (oder kommt nichts), hat sich die Scheibe um
        mehr als eine Seite verschoben, der Stand fällt weg, und die Scheibe kommt ganz.
        """
        entry = slices.run.pages.get(list_url) if full else None
        if entry is not None and entry.matches(window) and entry.ids:
            console.print(f"[dim]  {entity_type}: Scheibe {window.label()} ab {entry.page} (Fortsetzung)[/dim]")
            first = True
            shifted = False
            stream = client.fetch_list(
                list_url,
                modified_since=window.since,
                modified_until=window.until,
                strict_filter=True,
                next_page=entry.page,
            )
            try:
                async for page in stream:
                    if first:
                        first = False
                        if not set(unique_page_ids(page)) & set(entry.ids):
                            shifted = True
                            break
                    yield page
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
            if not first and not shifted:
                return
            console.print(
                f"[yellow]  {entity_type}: Scheibe {window.label()} hat sich seit dem Abbruch verschoben "
                f"({entry.page}), Scheibe ganz[/yellow]"
            )
            await self._clear_slice_page(slices, list_url)
        async for page in client.fetch_list(
            list_url,
            modified_since=window.since,
            modified_until=window.until,
            strict_filter=True,
            report_missing=report_missing,
        ):
            yield page

    async def _remember_slice_page(
        self, slices: BodySlices, list_url: str, window: SliceWindow, error: ListFetchError
    ) -> None:
        """
        Scheibe im Vollabgleich abgebrochen (Issue #1523): Am Fensterende und nach einer Störung (Zeitüberschreitung,
        Serverfehler, Schutzschalter) die zuletzt ganz gelesene Seite merken; ohne gelesene Folgeseite bleibt ein
        früherer Stand. Bei anderen Fehlern (Liste im Kreis, Teilantwort, Seite fehlt) fällt der Stand weg, und die
        Scheibe kommt beim nächsten Mal ganz.
        """
        if not isinstance(error, FetchWindowClosedError) and not error.interrupted:
            await self._clear_slice_page(slices, list_url)
            return
        if error.last_page_url is None or window.until is None:
            # Ohne gelesene Folgeseite bleibt ein früherer Stand; die letzte Scheibe (ohne Obergrenze) kommt immer
            # ganz, in sie kommen laufend neue Objekte, auch vor die gemerkte Seite (``SlicePage.matches``)
            return
        run = slices.run
        entry = SlicePage(
            since=window.since,
            until=window.until,
            page=error.last_page_url,
            at=datetime.now(UTC),
            ids=tuple(error.last_page_ids),
        )
        run.pages[list_url] = entry
        console.print(
            f"[yellow]  Scheibe {window.label()} der Liste {list_url} abgebrochen, der nächste Vollabgleich setzt ab "
            f"{entry.page} fort[/yellow]"
        )
        try:
            await self.storage.set_full_sync_slice_page(run.source_url, list_url, entry.to_entry())
        except Exception as e:  # noqa: BLE001 - dann liest der nächste Vollabgleich die Scheibe ganz
            console.print(f"[yellow]  Fortschritt in der Scheibe der Liste {list_url} nicht gespeichert: {e}[/yellow]")

    async def _clear_slice_page(self, slices: BodySlices, list_url: str) -> None:
        """Fortschritt innerhalb einer Scheibe löschen (Scheibe ganz gelesen oder verschoben, Issue #1523)."""
        run = slices.run
        if run.pages.pop(list_url, None) is None:
            return
        try:
            await self.storage.set_full_sync_slice_page(run.source_url, list_url, None)
        except Exception as e:  # noqa: BLE001 - ein alter Stand gilt nur für genau seine Scheibe
            console.print(f"[yellow]  Fortschritt in der Scheibe der Liste {list_url} nicht gelöscht: {e}[/yellow]")

    @staticmethod
    async def _resume_guard(
        pages: AsyncIterator[list[dict[str, Any]]], resume: ResumeRun, list_url: str
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Endet das Zeitfenster mitten in der Liste, die zuletzt gelesene Seite für die Fortsetzung merken. Ebenso bei
        einer Störung der Quelle (Issue #1523): Zeitüberschreitung, Serverfehler oder Verbindungsfehler nach allen
        Versuchen, offener Schutzschalter (``ListFetchError.interrupted``). Sonst begann der nächste Vollabgleich die
        Liste wieder bei Seite 1 und scheiterte jede Nacht an derselben Stelle (Stadt Bochum: Vorlagen ab Seite 52
        von 2.600 nie gelesen).
        """
        try:
            async for page in pages:
                yield page
        except FetchWindowClosedError as error:
            resume.note_cut(list_url, error.last_page_url, error.last_page_ids)
            raise
        except ListFetchError as error:
            if error.interrupted:
                resume.note_cut(list_url, error.last_page_url, error.last_page_ids)
            else:
                # Anderer Fehler (etwa eine Fortsetzungsseite, die es nicht mehr gibt): Der nächste Lauf liest ganz
                resume.note_failed(list_url)
            raise

    @staticmethod
    async def _resume_shifted(client: OParlClient, entry: ResumeEntry) -> bool:
        """
        Hat sich die Liste seit dem Abbruch um mehr als eine Seite verschoben? Dann trägt die Fortsetzungsseite keine
        der gemerkten Kennungen mehr, und Einträge, die noch nicht gelesen waren, könnten jetzt vor ihr stehen.
        Gemerkt und verglichen werden nur Kennungen, die genau einmal auf der Seite stehen (``unique_page_ids``). Ohne
        gemerkte Kennungen lässt sich die Lage nicht prüfen: ja, die Liste kommt ganz. Nach dem Fensterende oder bei
        einem Fehler: nein (das Weiterlesen bricht dann ab bzw. zeigt den Fehler).
        """
        if entry.page is None or client.deadline_passed():
            return False
        if not entry.ids:
            return True
        result = await client.fetch(entry.page, use_cache=False)
        if result.error or result.data is None:
            return False
        return not (set(unique_page_ids(OParlClient._extract_items(result.data))) & set(entry.ids))

    async def _settle_resume(self, resume: ResumeRun, complete: bool) -> None:
        """
        Stand der Fortsetzung nach dem Lauf speichern (``sync_config["full_sync_resume"]``): vollständig alles
        löschen, sonst gelesene und abgebrochene Listen festhalten. Ein Fehler kostet nur die Fortsetzung.
        """
        changes = resume.changes(complete)
        if not changes:
            return
        cut = sorted(url for url, entry in changes.items() if entry is not None and "page" in entry)
        if cut:
            console.print(
                f"[yellow]Vollabgleich abgebrochen (Fensterende oder Störung), der nächste setzt fort: "
                f"{', '.join(cut)}[/yellow]"
            )
        elif complete:
            console.print("[green]Vollabgleich über mehrere Läufe vollständig, Fortsetzung beendet[/green]")
        try:
            await self.storage.set_full_sync_resume(resume.source_url, changes)
        except Exception as e:  # noqa: BLE001 - dann beginnt der nächste Vollabgleich die Listen von vorn
            console.print(f"[yellow]Fortsetzung des Vollabgleichs nicht gespeichert: {e}[/yellow]")

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
        Hat dieser Versuch mindestens eine Scheibe ganz gelesen oder ist sein Seitenstand in der Scheibe vorgerückt
        (``progressed``), beginnt die Reihe wieder bei einer Stunde: Die Liste kommt voran. Keine Lücke des Laufs,
        aber eine Meldung im Sync-Protokoll.

        Liefert ``False``, wenn sich die Aussetzung nicht speichern ließ: Ohne gespeicherten Fensteranfang gingen die
        Änderungen der Liste verloren, sobald das Fenster der Kommune vorrückt. Der Aufrufer wertet die Liste dann
        wie bisher als Lücke.

        Nach ``SLOW_LIST_STUCK_AFTER`` Zeitüberschreitungen in Folge ohne Fortschritt gilt die Liste als festgefahren
        (Issue #1523): Warnung im Log, ``stuck_since`` im Eintrag, nächster Versuch erst nach einer Woche statt jeden
        Tag an derselben Tagesscheibe.
        """
        run = slices.run
        now = datetime.now(UTC)
        previous = run.slow.get(list_url)
        failures = 1 if progressed or previous is None else previous.failures + 1
        reason = "HTTP 504" if "HTTP 504" in str(error.result.error or "") else "Zeitüberschreitung"
        stuck_since: datetime | None = None
        if failures >= SLOW_LIST_STUCK_AFTER:
            stuck_since = previous.stuck_since if previous is not None and previous.stuck_since is not None else now
        entry = SlowList(
            failures=failures, until=now + slow_backoff(failures), since=since, reason=reason, stuck_since=stuck_since
        )
        try:
            await self.storage.set_slow_list(run.source_url, list_url, entry.to_entry())
        except Exception as e:  # noqa: BLE001 - dann bleibt die Liste eine Lücke, der nächste Lauf fragt sie wieder an
            console.print(f"[yellow]  Aussetzung der Liste {list_url} nicht gespeichert ({e}), Lücke[/yellow]")
            return False
        run.slow[list_url] = entry
        slices.suspended[list_url] = entry.until
        if entry.stuck:
            note = entry.stuck_note(list_url)
            if previous is None or not previous.stuck:
                logger.warning("Langsame Liste festgefahren: %s", note)
            slices.notes.append(note)
            console.print(f"[red]  {entity_type}: {note}[/red]")
            return True
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

        Eine festgefahrene langsame Liste (Issue #1523) steht in jedem Sync-Protokoll, solange sie ausgesetzt bleibt.
        """
        for list_url in sorted(slices.suspended):
            slow = slices.run.slow.get(list_url)
            if slow is not None and slow.stuck and (note := slow.stuck_note(list_url)) not in slices.notes:
                slices.notes.append(note)
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
        Fehlende und nicht freigegebene Listen eines Laufs vermerken (Issues #1025, #1086), wenn mindestens eine
        andere Liste der Kommune lesbar war; Pflichtlisten mit Fehlerobjekt gelten dann als leer (Warnung). Sonst gilt
        der Befund als Störung der Schnittstelle: Lücke im Abgleich, kein Vermerk.
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
                state = (
                    "gibt die Quelle nicht frei" if list_url in list_check.forbidden else "gibt es bei der Quelle nicht"
                )
                console.print(
                    f"[dim]  Liste {list_url} {state} ({reason}): vermerkt, "
                    f"bis zur nächsten Prüfung (Vollabgleich, spätestens nach einem Tag) nicht mehr angefragt[/dim]"
                )
            await self._remember_missing_list(list_check.lists, list_url, missing=True, reason=reason)

    async def _remember_missing_list(
        self, missing_lists: MissingLists | None, list_url: str, missing: bool, *, reason: str | None = None
    ) -> None:
        """
        Vermerk „Liste gibt es nicht“ bzw. „nicht freigegeben“ setzen (mit dem Zeitpunkt dieser Prüfung und dem Grund,
        ohne neuen Grund mit dem bisherigen) oder löschen; Fehler gefährden den Abgleich nie. Angefragt wird eine
        vermerkte Liste nur bei der Prüfung, deshalb schreibt jede Prüfung.
        """
        if missing_lists is None or (not missing and list_url not in missing_lists.known):
            return
        checked_at = datetime.now(UTC)
        if missing:
            missing_lists.known[list_url] = checked_at
            reason = reason or missing_lists.reasons.get(list_url)
            if reason:
                missing_lists.reasons[list_url] = reason
        else:
            del missing_lists.known[list_url]
            missing_lists.reasons.pop(list_url, None)
            console.print(f"[green]  Liste {list_url} wieder abrufbar, Vermerk gelöscht[/green]")
        try:
            await self.storage.set_list_missing(
                missing_lists.source_url, list_url, missing, checked_at=checked_at, reason=reason
            )
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
          liefert (``BodyItemCheck``), und zurückgestellte (``settle_unstored_holds``).
        - Sonst: Beginn dieses Laufs.

        Nicht gespeicherte Objekte nennt das Log je Typ mit Anzahl und bis zu fünf Kennungen; je Quelle stehen sie
        im Sync-Log unter ``unstored``, zurückgestellte zusätzlich unter ``unstored_deferred``. So fallen dauerhaft
        nicht speicherbare Objekte auf.
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
        deferred = await self._settle_unstored_holds(source_id, body_external_id, item_check, stats)
        start = item_check.window_start(body_started, deferred)
        if start is None:
            return
        try:
            await self.storage.set_body_sync_started(source_id, body_external_id, start)
        except Exception as e:  # noqa: BLE001 - dann gilt beim nächsten Mal der bisherige Zeitpunkt
            console.print(f"[yellow]  Anfang des Änderungsfensters nicht gespeichert: {e}[/yellow]")

    async def _settle_unstored_holds(
        self, source_id: UUID, body_external_id: str, item_check: BodyItemCheck, stats: dict[str, Any]
    ) -> set[str]:
        """
        Nicht gespeicherte Objekte, die das Fenster seit mehreren Läufen halten, zurückstellen und melden
        (``settle_unstored_holds``, Vermerk in ``sync_config["unstored_holds"]``). Liefert die Kennungen, die das
        Fenster in diesem Lauf nicht mehr halten. Fehler gefährden den Abgleich nie: Ohne Vermerk halten alle nicht
        gespeicherten Objekte das Fenster wie bisher.
        """
        try:
            known = await self.storage.get_unstored_holds(source_id, body_external_id)
        except Exception as e:  # noqa: BLE001 - ohne Vermerk hält jedes Objekt das Fenster wie bisher
            console.print(f"[yellow]  Vermerk nicht gespeicherter Objekte nicht lesbar: {e}[/yellow]")
            return set()
        if not known and not item_check.holding:
            return set()
        entries, deferred, newly = settle_unstored_holds(known, item_check.holding, datetime.now(UTC))
        if entries != known:
            try:
                await self.storage.set_unstored_holds(source_id, body_external_id, entries)
            except Exception as e:  # noqa: BLE001 - ohne Vermerk zählt der nächste Lauf weiter ab dem alten Stand
                console.print(f"[yellow]  Vermerk nicht gespeicherter Objekte nicht gespeichert: {e}[/yellow]")
                return set()
        if deferred:
            by_type = Counter(item_check.holding[external_id][0] for external_id in deferred)
            stats["unstored_deferred"] = dict(sorted(by_type.items()))
            summary = ", ".join(f"{name}: {count}" for name, count in sorted(by_type.items()))
            console.print(
                f"[yellow]  Zurückgestellt ({summary}): seit mindestens {UNSTORED_HOLD_RUNS} Läufen nicht "
                "speicherbar, sie halten das Fenster nicht mehr; der nächste Vollabgleich versucht sie wieder[/yellow]"
            )
            for external_id in sorted(newly)[:UNSTORED_IDS_LOGGED]:
                entry = entries[external_id]
                console.print(f"[yellow]    neu zurückgestellt: {external_id} (seit {entry['since']})[/yellow]")
        return deferred

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

        Geschrieben und verknüpft werden nur Objekte derselben Quelle wie ``body_id``: Steht die Kennung unter einer
        anderen Quelle, wirft der Speicher ``ForeignObjectError`` (der Lauf zählt das Objekt als nicht gespeichert),
        und Verweise auf Sitzungen und Vorlagen anderer Quellen lösen nicht auf.
        """
        if isinstance(entity, ProcessedMeeting):
            await self.storage.upsert_meeting(entity, body_id)
            await self._retract_session_location(entity.external_id, body_id=body_id)
            # Eingebettete TOPs und Dateien speichert upsert_meeting selbst; hier nur zählen. Dateien an den TOPs
            # danach, mit Bezug zum Punkt
            for nested in entity.nested_entities:
                if isinstance(nested, ProcessedFile):
                    metrics.record_entity_synced("file", body_name or "unknown")
                elif isinstance(nested, ProcessedAgendaItem):
                    metrics.record_entity_synced("agendaitem", body_name or "unknown")
                    await self._store_agenda_item_files(nested, body_id, body_name)
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
            # Process nested memberships (OParl 1.0: embedded in Person objects); eine Mitgliedschaft einer anderen
            # Quelle bleibt aus (gezählt), die Person ist gespeichert
            for nested in entity.nested_entities:
                if isinstance(nested, ProcessedMembership):
                    try:
                        await self.storage.upsert_membership(nested, body_id)
                    except ForeignObjectError:
                        continue
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
                meeting_id = await self.storage.get_meeting_uuid(entity.meeting_external_id, body_id)
            if not meeting_id:
                # Sitzung nicht im Bestand (oder einer anderen Quelle): nicht gespeichert, der Lauf hält es fest
                # (Issue #1025)
                return False
            await self.storage.upsert_agenda_item(entity, meeting_id)
            metrics.record_entity_synced("agendaitem", body_name or "unknown")
            await self._store_agenda_item_files(entity, body_id, body_name)
        elif isinstance(entity, ProcessedFile):
            # Files can belong to papers or meetings
            # Try to resolve paper_id from back-references (OParl spec: standalone files have these)
            paper_id = None
            meeting_id = None
            if entity.paper_external_ids:
                # Use first paper reference (files can belong to multiple papers)
                paper_id = await self.storage.get_paper_uuid(entity.paper_external_ids[0], body_id)
            if entity.meeting_external_ids:
                meeting_id = await self.storage.get_meeting_uuid(entity.meeting_external_ids[0], body_id)
            # Datei am Tagesordnungspunkt (OParl 1.1 ``agendaItem``): mit Punkt und, ohne eigene Angabe, seiner Sitzung
            agenda_item_id = None
            agenda_item_public = True
            if entity.agenda_item_external_ids:
                ref = await self.storage.get_agenda_item_ref(entity.agenda_item_external_ids[0], body_id)
                if ref is not None:
                    agenda_item_id, ref_meeting_id, agenda_item_public = ref
                    meeting_id = meeting_id or ref_meeting_id
            await self.storage.upsert_file(
                entity,
                body_id,
                paper_id=paper_id,
                meeting_id=meeting_id,
                agenda_item_id=agenda_item_id,
                agenda_item_public=agenda_item_public,
            )
            metrics.record_entity_synced("file", body_name or "unknown")
        elif isinstance(entity, ProcessedConsultation):
            # Consultations can belong to papers - try to look it up
            paper_id = None
            if entity.paper_external_id:
                paper_id = await self.storage.get_paper_uuid(entity.paper_external_id, body_id)
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

    async def _store_agenda_item_files(self, item: ProcessedAgendaItem, body_id: UUID, body_name: str | None) -> None:
        """Dateien direkt am Tagesordnungspunkt speichern (``upsert_agenda_item_files``) und zählen."""
        if not item.references.get("files"):
            return
        stored = await self.storage.upsert_agenda_item_files(item, body_id)
        for _ in range(stored):
            metrics.record_entity_synced("file", body_name or "unknown")

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
            console.print(
                "[yellow]Keine aktive Quelle (anlegen: manage.py add_oparl_source, aktivieren: Admin)[/yellow]"
            )
            return []

        # Quellen in Schonung (Issue #89) und außerhalb ihres Abruffensters (Issue #1186) überspringen
        now = datetime.now(UTC)
        due = []
        for source in sources:
            until = source_backoff_until(source, now)
            window = SourceFetchOptions.from_sync_config(getattr(source, "sync_config", None)).sync_window
            if until is None and window is not None and window.closes_at(now) is None:
                console.print(f"[yellow]{outside_window_note(source.name, window, now)}[/yellow]")
            elif until is None:
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
                **(
                    {"unstored_deferred": dict(sorted(result.unstored_deferred.items()))}
                    if result.unstored_deferred
                    else {}
                ),
                **({"foreign": dict(sorted(result.foreign.items()))} if result.foreign else {}),
                **({"unresolved": dict(sorted(result.unresolved.items()))} if result.unresolved else {}),
                **result.slice_details,
            }

        status = SYNC_STATUS_SUCCESS if not all_errors or total_entities > 0 else SYNC_STATUS_FAILED
        # Vollabgleich mit Lücke (Issue #1523), auch ohne geschriebene Objekte, solange kein Lauf gescheitert ist
        if all(result.success for result in results) and any(result.full_incomplete for result in results):
            status = SYNC_STATUS_PARTIAL
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
