"""
ScraperSyncRunner: führt einen Scraper-Adapter aus und speist dessen
synthetische OParl-Dicts in die bestehende Pipeline ein.

Wiederverwendet vom Orchestrator: OParlProcessor, _store_entity (Upserts,
Events, Metriken), Tombstone-Mechanik (mark_entity_deleted + ES-Cleanup),
Text-Extraktion und Elasticsearch-Indexierung. Der Kern bleibt unverändert.

Änderungserkennung:
- Listen-Diffing: Kalender-Monats-Snapshots (Hash) in sync_config
- Content-Hash je Entität ("mandari:contentHash" im raw_json) — Upsert nur
  bei Differenz; "modified" ist die Crawl-Zeit des letzten echten Updates
- Verschwinden: erst nach N (Default 3) Full-Crawls ohne Sichtung wird
  mark_entity_deleted gesetzt (Tombstone, nie physisches Löschen). Es zählen nur
  vollständige Full-Crawls: Für einen Typ, dessen Liste der Lauf nicht ganz gelesen hat
  (Fehler, Sperr- oder fremde Seite, Detailseiten-Budget), bleibt jeder Zähler stehen;
  fehlen mehr Objekte als die Bremse erlaubt, ebenso (Issue #556, mandari_oparl.abgleich)
- Aktualität: last_sync nur nach einem durchgelaufenen Crawl; der letzte vollständig
  erfolgreiche Abgleich (ohne jede Lücke) steht getrennt an der Quelle

Eine Quelle kann mehrere Körperschaften liefern (SessionNet-Instanz einer
Samtgemeinde mit ihren Mitgliedsgemeinden): je Körperschaft ein Body, jede
Seite des Adapters trägt die Kennung ihres Bodies.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from mandari_oparl.abgleich import brake_engaged
from mandari_oparl.robots import robots_override

from src.client.oparl_client import ERROR_KIND_ROBOTS_BLOCKED, ERROR_KIND_SERVER_ERROR_SERIES
from src.config import settings
from src.metrics import metrics
from src.redaction import MaskingConsole
from src.scrapers import get_adapter
from src.scrapers.base import CrawlWindow, ScraperConfig, ScrapeStats
from src.scrapers.politeness import PoliteFetcher, RobotsDisallowedError, RobotsUnreachableError
from src.storage.database import PREVIOUS_ID_FIELD
from src.storage.events import start_correlation

if TYPE_CHECKING:
    from src.storage.models import OParlSource
    from src.sync.orchestrator import SyncOrchestrator, SyncResult

console = MaskingConsole()

# Entity-Typen, für die auf Full-Crawls Verschwinde-Erkennung läuft.
# Bewusst konservativ: nur Typen, deren Kandidatenmenge vollständig im
# Crawl-Fenster liegt (Sitzungen im Fenster, Gremienliste ist komplett).
TOMBSTONE_ENTITY_TYPES = ("meeting", "organization")
#: Tagesordnungspunkte zählen je vollständig gelesener Sitzung (``ScrapeStats.agendas``, Issue #863)
AGENDA_ITEM_TYPE = "agendaitem"

_RESULT_FIELD_BY_TYPE = {
    "meeting": "meetings_synced",
    "paper": "papers_synced",
    "person": "persons_synced",
    "organization": "organizations_synced",
    "membership": "memberships_synced",
    "file": "files_synced",
    "location": "locations_synced",
    "agendaitem": "agenda_items_synced",
    "consultation": "consultations_synced",
}


class ScraperSyncRunner:
    """Führt einen kompletten Scraper-Lauf für eine Quelle aus."""

    def __init__(self, orchestrator: SyncOrchestrator, source: OParlSource) -> None:
        self.orchestrator = orchestrator
        self.storage = orchestrator.storage
        self.processor = orchestrator.processor
        self.source = source
        self.source_type = str((source.sync_config or {}).get("source_type") or "")

    async def run(self, full: bool = False) -> SyncResult:
        from src.sync.orchestrator import SyncResult

        start_time = datetime.now(UTC)
        result = SyncResult(
            source_url=self.source.url,
            source_name=self.source.name,
            success=False,
            full=full,
        )
        state: dict[str, Any] = dict((self.source.sync_config or {}).get("scraper_state") or {})

        try:
            config = ScraperConfig.from_sync_config(self.source.sync_config)
        except ValueError as e:
            result.errors.append(str(e))
            console.print(f"[red]Scraper-Quelle {self.source.name}: {e}[/red]")
            return result

        window_days = config.full_window_days if full else config.calendar_window_days
        window = CrawlWindow.from_days(*window_days)
        console.print(
            f"\n[bold cyan]Scraper-Sync ({self.source_type}): {self.source.name} "
            f"[{'full' if full else 'incremental'}, Fenster {window.start}..{window.end}]"
            f"[/bold cyan]"
        )

        stats: dict[str, int] = {}
        es_deletions: dict[str, list[str]] = {}
        seen: dict[str, set[str]] = {}
        skipped_unchanged = 0
        crawl_completed = False

        async with PoliteFetcher(
            rate_limit_seconds=config.rate_limit_seconds,
            source_name=self.source.name,
            user_agent=getattr(self.source, "user_agent", None) or None,
            robots_override=robots_override(getattr(self.source, "sync_config", None)),
        ) as fetcher:
            adapter = get_adapter(self.source_type, config, fetcher)
            # Listen-Diffing: bekannte Monats-Snapshots aus dem letzten Lauf
            adapter.previous_snapshots = {} if full else dict(state.get("list_snapshots") or {})

            # robots.txt vorab prüfen: verbietet die Instanz unseren Crawler,
            # wird die Quelle nicht gecrawlt und im Admin sichtbar markiert.
            if not await fetcher.is_allowed(config.base_url):
                message = f"robots.txt verbietet Crawl von {config.base_url}"
                console.print(f"[yellow]{message} — Quelle wird übersprungen[/yellow]")
                result.errors.append(message)
                state["robots_disallowed"] = True
                await self.storage.update_scraper_state(self.source.url, state)
                await self._sperre_festhalten(result, message)
                return result
            state.pop("robots_disallowed", None)

            # Alle Ereignisse dieses Crawls tragen dieselbe Korrelations-ID (src/storage/events.py)
            start_correlation()

            # Body-Kennung -> UUID; mehrere Einträge bei mehreren Körperschaften je Instanz
            body_ids: dict[str, UUID] = {}
            try:
                # Bodies anlegen/aktualisieren (mit Wahlperioden)
                for body_dict in await adapter.resolve_bodies():
                    processed_body = self.processor.process_body(body_dict, body_dict["id"])
                    body_ids[body_dict["id"]] = await self.storage.upsert_body(processed_body, self.source.id)
                result.bodies_synced = len(body_ids)

                async for body_external_id, entity_type, page in adapter.iter_body_entities(window, full=full):
                    body_id = body_ids[body_external_id]
                    external_ids = [item["id"] for item in page if item.get("id")]
                    seen.setdefault(entity_type, set()).update(external_ids)

                    stored_hashes = await self.storage.get_entity_content_hashes(entity_type, external_ids)
                    for item in page:
                        item_hash = item.get("mandari:contentHash")
                        if item_hash and stored_hashes.get(item.get("id", "")) == item_hash:
                            skipped_unchanged += 1
                            continue
                        if entity_type == "meeting":
                            await self._keep_agenda_item_rows(adapter, item)
                        processed = self.processor.process(item, body_external_id)
                        if processed is None:
                            continue
                        stored = await self.orchestrator._store_entity(
                            processed, body_id, entity_type, self.source.name
                        )
                        if stored:
                            stats[entity_type] = stats.get(entity_type, 0) + 1
                crawl_completed = True
            except RobotsDisallowedError as e:
                message = f"robots.txt verbietet {e}"
                result.errors.append(message)
                await self._sperre_festhalten(result, message)
            except RobotsUnreachableError as e:
                # Störung, keine Sperre: kurze Schonung wie bei einer 5xx-Serie, neuer Versuch im nächsten Lauf
                message = f"Störung: {e}"
                result.errors.append(message)
                result.error_kind = ERROR_KIND_SERVER_ERROR_SERIES
                await self.orchestrator._record_source_failure(self.source.url, message, ERROR_KIND_SERVER_ERROR_SERIES)
            except Exception as e:
                result.errors.append(f"Crawl-Fehler: {e}")
                console.print(f"[red]Scraper-Fehler bei {self.source.name}: {e}[/red]")
                await self.orchestrator._record_source_failure(self.source.url, f"Crawl-Fehler: {e}")

            # Parse-Quote prüfen (Metrik + Log-Warnung bei Einbruch)
            quota = adapter.stats.parse_quota
            metrics.record_scraper_quota(self.source.name, quota)
            if adapter.stats.detail_pages_attempted >= 5 and quota < settings.scraper_parse_quota_warn:
                console.print(
                    f"[bold red]WARNUNG: Parse-Quote {quota:.0%} unter Schwellwert "
                    f"{settings.scraper_parse_quota_warn:.0%} für {self.source.name} — "
                    f"HTML-Struktur der Instanz prüfen (Parser-Bruch?)[/bold red]"
                )
                result.errors.append(
                    f"Parse-Quote eingebrochen: {quota:.0%} "
                    f"({adapter.stats.detail_pages_parsed}/"
                    f"{adapter.stats.detail_pages_attempted} Detailseiten)"
                )

            # Vollständig erfolgreich: durchgelaufen, ohne Fehler und ohne Lücke (Issue #556)
            complete = crawl_completed and not result.errors and adapter.stats.complete
            if crawl_completed and not complete:
                gaps = "; ".join(f"{t}: {r}" for t, r in sorted(adapter.stats.incomplete.items()))
                console.print(
                    f"[yellow]Scraper-Lauf {self.source.name} nicht vollständig "
                    f"({adapter.stats.failed_pages - adapter.stats.gone_pages} Seiten gestört"
                    f"{'; ' + gaps if gaps else ''})[/yellow]"
                )

            # Verschwinde-Erkennung: nur nach durchgelaufenen, fehlerfreien Full-Crawls und nur für
            # Typen, deren Liste vollständig gelesen wurde; Tombstone erst nach N Sichtungs-Ausfällen.
            deleted_count = 0
            braked: dict[str, int] = {}
            agenda_braked: dict[str, int] = {}
            if full and crawl_completed and not result.errors:
                deleted_count = await self._handle_missing(
                    list(body_ids.values()),
                    window,
                    seen,
                    state,
                    es_deletions,
                    adapter.stats,
                    braked,
                    removable_agenda_item=getattr(adapter, "is_removable_agenda_item_id", None),
                    agenda_braked=agenda_braked,
                )

            # Zustand persistieren
            snapshots = dict(state.get("list_snapshots") or {})
            snapshots.update(getattr(adapter, "list_snapshots", {}))
            state["list_snapshots"] = snapshots
            state["adapter_schema_version"] = adapter.schema_version
            state["last_run"] = {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "full": full,
                "pages_fetched": adapter.stats.pages_fetched,
                "detail_pages_attempted": adapter.stats.detail_pages_attempted,
                "detail_pages_parsed": adapter.stats.detail_pages_parsed,
                "parse_quota": round(quota, 4),
                "entities_stored": sum(stats.values()),
                "unchanged_skipped": skipped_unchanged,
                "tombstoned": deleted_count,
                "complete": complete,
                "failed_pages": adapter.stats.failed_pages,
                "gone_pages": adapter.stats.gone_pages,
                "incomplete": dict(sorted(adapter.stats.incomplete.items())),
                "tombstone_braked": braked,
                # Tagesordnungen, deren Löschabgleich die Bremse je Sitzung ausgesetzt hat (Issue #863)
                "agenda_braked": agenda_braked,
            }
            await self.storage.update_scraper_state(self.source.url, state)

        # Text-Extraktion für neue Dateien (bestehender Extractor)
        if settings.text_extraction_enabled and body_ids:
            try:
                from src.extraction.extractor import TextExtractor

                extractor = TextExtractor(self.storage)
                for body_id in body_ids.values():
                    extracted = await extractor.extract_pending_files(body_id)
                    if extracted:
                        console.print(f"[green]  {extracted} Dateien extrahiert[/green]")
            except Exception as e:
                result.errors.append(f"Text extraction: {e}")

        # Elasticsearch-Indexierung (gleicher Pfad wie OParl-Sync), je Body;
        # Löschungen aus dem Index nur einmal
        total_synced = sum(stats.values())
        if settings.elasticsearch_indexing_enabled and (total_synced > 0 or es_deletions or full):
            pending_deletions = es_deletions
            from src.sync.orchestrator import index_since

            for body_id in body_ids.values():
                index_stats: dict[str, Any] = {"errors": []}
                await self.orchestrator._index_body_elasticsearch(
                    body_id, index_stats, pending_deletions, full, since=index_since(full, start_time)
                )
                result.errors.extend(index_stats["errors"])
                pending_deletions = {}

        # Nur ein durchgelaufener Crawl ist ein Abgleich: Ein abgebrochener setzte sonst den Fehlerstatus
        # zurück, den er eben festgehalten hat, und die Quelle gälte als aktuell (Issue #556)
        if crawl_completed:
            for body_id in body_ids.values():
                await self.storage.update_body_sync_time(body_id)
            await self.storage.update_source_sync_time(self.source.id, full_sync=full, complete=complete)

        for entity_type, count in stats.items():
            field_name = _RESULT_FIELD_BY_TYPE.get(entity_type)
            if field_name:
                setattr(result, field_name, getattr(result, field_name) + count)
        result.success = crawl_completed
        result.duration_seconds = (datetime.now(UTC) - start_time).total_seconds()

        metrics.record_entities_batch(self.source.name, total_synced)
        console.print(
            f"[green]Scraper-Sync fertig: {self.source.name} — {total_synced} gespeichert, "
            f"{skipped_unchanged} unverändert, Quote {quota:.0%}, "
            f"{adapter.stats.pages_fetched} Seiten, {result.duration_seconds:.0f}s[/green]"
        )
        return result

    async def _keep_agenda_item_rows(self, adapter: Any, meeting: dict[str, Any]) -> None:
        """
        Vor dem Verarbeiten einer Sitzung: Zeilen ihrer Tagesordnungspunkte, die noch eine vom Adapter abgelöste
        Kennung tragen, auf die neue umschreiben (``external_id`` neu, ``id`` bleibt, Issue #863). Sonst entstünde je
        Punkt eine zweite Zeile, und Notizen und Verweise blieben an der alten. Welche Zeile zu welchem Punkt gehört,
        entscheidet der Adapter (``agenda_item_renames``); Adapter ohne die Methode lassen alles, wie es ist.

        Die abgelöste Kennung bleibt an jedem gelieferten Punkt als ``mandari:previousId`` stehen (auch in den
        folgenden Abrufen): Daran findet der Rückweg (``revert_agenda_item_ids``) die Zeile wieder.
        """
        plan = getattr(adapter, "agenda_item_renames", None)
        items = meeting.get("agendaItem")
        if plan is None or not items:
            return
        stored = await self.storage.get_agenda_items_of_meeting(meeting["id"])
        renames = plan(meeting["id"], stored)
        if renames:
            renamed = await self.storage.rename_agenda_items(meeting["id"], renames)
            if renamed:
                console.print(f"[dim]  Sitzung {meeting['id']}: {renamed} TOP-Kennung(en) umgestellt (#863)[/dim]")
        # Neue Kennung -> abgelöste: aus dem Bestand (frühere Umstellung) und aus dieser
        previous = {str(row["external_id"]): str(row["previous_id"]) for row in stored if row.get("previous_id")}
        previous.update({new: old for old, new in renames.items()})
        for item in items:
            if isinstance(item, dict) and item.get("id") in previous:
                item[PREVIOUS_ID_FIELD] = previous[item["id"]]

    async def _sperre_festhalten(self, result: Any, message: str) -> None:
        """
        robots-Sperre als Fehlerklasse an der Quelle festhalten (Issue #116): Betriebsmonitor
        und Admin zeigen „robots.txt sperrt“ mit Empfehlung statt eines stillen Parse-Fehlers,
        und die Quellen-Schonung fragt nur noch täglich nach. Keine Umgehung.
        """
        result.error_kind = ERROR_KIND_ROBOTS_BLOCKED
        await self.orchestrator._record_source_failure(self.source.url, message, ERROR_KIND_ROBOTS_BLOCKED)

    async def _handle_missing(
        self,
        body_ids: list[UUID],
        window: CrawlWindow,
        seen: dict[str, set[str]],
        state: dict[str, Any],
        es_deletions: dict[str, list[str]],
        stats: ScrapeStats,
        braked: dict[str, int],
        *,
        removable_agenda_item: Callable[[str], bool] | None = None,
        agenda_braked: dict[str, int] | None = None,
    ) -> int:
        """
        Zählt nicht mehr gesichtete Objekte hoch und tombstonet nach
        N aufeinanderfolgenden Full-Crawls ohne Sichtung (Default 3).
        Wieder auftauchende Objekte werden vom Upsert-Pfad automatisch
        reaktiviert (deleted=False im update_set). Kandidaten sind die aktiven
        Objekte aller Bodies der Quelle.

        Nicht vollständig gelesene Typen (``stats.incomplete``) und Typen, bei denen mehr Objekte fehlen,
        als die Bremse erlaubt (``braked``), lassen ihre Zähler stehen: Aus einer Lücke folgt nichts.
        Objekte, die eine Liste nennt, deren Detailseite aber nicht lesbar war, gelten als gesehen.
        Tagesordnungspunkte zählt ``_count_missing_agenda_items`` je Sitzung.
        """
        threshold = max(1, settings.scraper_tombstone_full_crawls)
        missing_state: dict[str, dict[str, int]] = {k: dict(v) for k, v in (state.get("missing") or {}).items()}
        deleted_count = 0

        for entity_type in TOMBSTONE_ENTITY_TYPES:
            if entity_type in stats.incomplete:
                console.print(
                    f"[yellow]  Löschabgleich {entity_type} ausgesetzt: {stats.incomplete[entity_type]}[/yellow]"
                )
                continue
            candidates: set[str] = set()
            for body_id in body_ids:
                if entity_type == "meeting":
                    candidates |= await self.storage.get_active_meeting_ids_in_window(body_id, window.start, window.end)
                else:
                    candidates |= await self.storage.get_active_external_ids_for_body(entity_type, body_id)
            seen_ids = seen.get(entity_type, set()) | stats.listed.get(entity_type, set())
            unseen = candidates - seen_ids
            if brake_engaged(len(unseen), settings.scraper_tombstone_max_missing):
                braked[entity_type] = len(unseen)
                console.print(
                    f"[bold red]  Löschabgleich {entity_type} gebremst: {len(unseen)} Objekte fehlen "
                    f"(Grenze {settings.scraper_tombstone_max_missing}), nichts gezählt - Quelle prüfen[/bold red]"
                )
                continue
            counters = missing_state.setdefault(entity_type, {})

            # Wieder gesehen -> Zähler zurücksetzen
            for external_id in list(counters):
                if external_id in seen_ids or external_id not in candidates:
                    del counters[external_id]

            deleted_count += await self._count_up(entity_type, unseen, counters, threshold, es_deletions)

        if removable_agenda_item is not None:
            counters = missing_state.setdefault(AGENDA_ITEM_TYPE, {})
            unseen = await self._missing_agenda_items(stats, counters, removable_agenda_item, agenda_braked)
            deleted_count += await self._count_up(AGENDA_ITEM_TYPE, unseen, counters, threshold, es_deletions)

        state["missing"] = missing_state
        return deleted_count

    async def _missing_agenda_items(
        self,
        stats: ScrapeStats,
        counters: dict[str, int],
        removable: Callable[[str], bool],
        agenda_braked: dict[str, int] | None,
    ) -> set[str]:
        """
        Tagesordnungspunkte, die in diesem Lauf als fehlend zählen (Issue #863); setzt die übrigen Zähler zurück.

        Liste ist die Tagesordnung einer Sitzung: Kandidaten sind nur die Punkte der Sitzungen, deren Tagesordnung der
        Lauf gelesen hat (``stats.agendas``, mindestens ein Punkt), und nur solche, die der Adapter dafür freigibt.
        Die Bremse gilt je Sitzung, nicht je Quelle: Fehlt mehr als die Hälfte ihrer Punkte (etwa ein ganzer Teil der
        Seite), zählt für diese Sitzung nichts, und ihre Zähler bleiben stehen. Was eine Quelle früher nie gelöscht hat,
        hält so den Abgleich der übrigen Sitzungen nicht an. Liest ein Lauf eine Sitzung nicht (oder keine), beginnt die
        Zählung ihrer Punkte von vorn.
        """
        stored = await self.storage.get_active_agenda_item_ids(sorted(stats.agendas)) if stats.agendas else {}
        unseen: set[str] = set()
        held: set[str] = set()
        braked_meetings = braked_items = 0
        for meeting, items in stored.items():
            candidates = {external_id for external_id in items if removable(external_id)}
            missing = candidates - stats.agendas.get(meeting, set())
            if 2 * len(missing) > len(candidates):
                braked_meetings += 1
                braked_items += len(missing)
                held |= missing
                continue
            unseen |= missing
        for external_id in list(counters):
            if external_id not in unseen and external_id not in held:
                del counters[external_id]
        if braked_meetings:
            console.print(
                f"[yellow]  Löschabgleich agendaitem: {braked_meetings} Sitzung(en) gebremst, dort fehlt mehr als die "
                f"Hälfte der Punkte ({braked_items}) - nichts gezählt[/yellow]"
            )
            if agenda_braked is not None:
                agenda_braked.update({"meetings": braked_meetings, "missing": braked_items})
        return unseen

    async def _count_up(
        self,
        entity_type: str,
        unseen: set[str],
        counters: dict[str, int],
        threshold: int,
        es_deletions: dict[str, list[str]],
    ) -> int:
        """Zähler der fehlenden Objekte erhöhen; nach ``threshold`` Läufen in Folge als gelöscht markieren."""
        deleted_count = 0
        for external_id in sorted(unseen):
            counters[external_id] = counters.get(external_id, 0) + 1
            if counters[external_id] >= threshold:
                marked = await self.orchestrator._mark_deleted({"id": external_id}, entity_type, es_deletions)
                if marked:
                    deleted_count += 1
                    console.print(
                        f"[yellow]  Tombstone: {entity_type} {external_id} "
                        f"(nach {counters[external_id]} Full-Crawls ohne Sichtung)[/yellow]"
                    )
                del counters[external_id]
        return deleted_count
