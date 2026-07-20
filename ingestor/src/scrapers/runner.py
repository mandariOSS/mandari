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
  mark_entity_deleted gesetzt (Tombstone, nie physisches Löschen)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rich.console import Console

from src.config import settings
from src.metrics import metrics
from src.scrapers import get_adapter
from src.scrapers.base import CrawlWindow, ScraperConfig
from src.scrapers.politeness import PoliteFetcher, RobotsDisallowedError

if TYPE_CHECKING:
    from src.storage.models import OParlSource
    from src.sync.orchestrator import SyncOrchestrator, SyncResult

console = Console()

# Entity-Typen, für die auf Full-Crawls Verschwinde-Erkennung läuft.
# Bewusst konservativ: nur Typen, deren Kandidatenmenge vollständig im
# Crawl-Fenster liegt (Sitzungen im Fenster, Gremienliste ist komplett).
TOMBSTONE_ENTITY_TYPES = ("meeting", "organization")

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
                return result
            state.pop("robots_disallowed", None)

            # Body anlegen/aktualisieren
            body_dict = adapter.build_body()
            processed_body = self.processor.process_body(body_dict, body_dict["id"])
            body_id = await self.storage.upsert_body(processed_body, self.source.id)
            result.bodies_synced = 1

            try:
                async for entity_type, page in adapter.iter_entities(window, full=full):
                    external_ids = [item["id"] for item in page if item.get("id")]
                    seen.setdefault(entity_type, set()).update(external_ids)

                    stored_hashes = await self.storage.get_entity_content_hashes(
                        entity_type, external_ids
                    )
                    for item in page:
                        item_hash = item.get("mandari:contentHash")
                        if item_hash and stored_hashes.get(item.get("id", "")) == item_hash:
                            skipped_unchanged += 1
                            continue
                        processed = self.processor.process(item, body_dict["id"])
                        if processed is None:
                            continue
                        stored = await self.orchestrator._store_entity(
                            processed, body_id, entity_type, self.source.name
                        )
                        if stored:
                            stats[entity_type] = stats.get(entity_type, 0) + 1
                crawl_completed = True
            except RobotsDisallowedError as e:
                result.errors.append(f"robots.txt verbietet {e}")
            except Exception as e:
                result.errors.append(f"Crawl-Fehler: {e}")
                console.print(f"[red]Scraper-Fehler bei {self.source.name}: {e}[/red]")

            # Parse-Quote prüfen (Metrik + Log-Warnung bei Einbruch)
            quota = adapter.stats.parse_quota
            metrics.record_scraper_quota(self.source.name, quota)
            if (
                adapter.stats.detail_pages_attempted >= 5
                and quota < settings.scraper_parse_quota_warn
            ):
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

            # Verschwinde-Erkennung: nur nach vollständigen, fehlerfreien
            # Full-Crawls zählen; Tombstone erst nach N Sichtungs-Ausfällen.
            deleted_count = 0
            if full and crawl_completed and not result.errors:
                deleted_count = await self._handle_missing(
                    body_id, window, seen, state, es_deletions
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
            }
            await self.storage.update_scraper_state(self.source.url, state)

        # Text-Extraktion für neue Dateien (bestehender Extractor)
        if settings.text_extraction_enabled:
            try:
                from src.extraction.extractor import TextExtractor

                extractor = TextExtractor(self.storage)
                extracted = await extractor.extract_pending_files(body_id)
                if extracted:
                    console.print(f"[green]  {extracted} Dateien extrahiert[/green]")
            except Exception as e:
                result.errors.append(f"Text extraction: {e}")

        # Elasticsearch-Indexierung (gleicher Pfad wie OParl-Sync)
        total_synced = sum(stats.values())
        index_stats: dict[str, Any] = {"errors": []}
        if settings.elasticsearch_indexing_enabled and (
            total_synced > 0 or es_deletions or full
        ):
            await self.orchestrator._index_body_elasticsearch(
                body_id, index_stats, es_deletions, full
            )
            result.errors.extend(index_stats["errors"])

        await self.storage.update_body_sync_time(body_id)
        await self.storage.update_source_sync_time(self.source.id, full_sync=full)

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

    async def _handle_missing(
        self,
        body_id,
        window: CrawlWindow,
        seen: dict[str, set[str]],
        state: dict[str, Any],
        es_deletions: dict[str, list[str]],
    ) -> int:
        """
        Zählt nicht mehr gesichtete Objekte hoch und tombstonet nach
        N aufeinanderfolgenden Full-Crawls ohne Sichtung (Default 3).
        Wieder auftauchende Objekte werden vom Upsert-Pfad automatisch
        reaktiviert (deleted=False im update_set).
        """
        threshold = max(1, settings.scraper_tombstone_full_crawls)
        missing_state: dict[str, dict[str, int]] = {
            k: dict(v) for k, v in (state.get("missing") or {}).items()
        }
        deleted_count = 0

        for entity_type in TOMBSTONE_ENTITY_TYPES:
            if entity_type == "meeting":
                candidates = await self.storage.get_active_meeting_ids_in_window(
                    body_id, window.start, window.end
                )
            else:
                candidates = await self.storage.get_active_external_ids_for_body(
                    entity_type, body_id
                )
            seen_ids = seen.get(entity_type, set())
            counters = missing_state.setdefault(entity_type, {})

            # Wieder gesehen -> Zähler zurücksetzen
            for external_id in list(counters):
                if external_id in seen_ids or external_id not in candidates:
                    del counters[external_id]

            for external_id in candidates - seen_ids:
                counters[external_id] = counters.get(external_id, 0) + 1
                if counters[external_id] >= threshold:
                    marked = await self.orchestrator._mark_deleted(
                        {"id": external_id}, entity_type, es_deletions
                    )
                    if marked:
                        deleted_count += 1
                        console.print(
                            f"[yellow]  Tombstone: {entity_type} {external_id} "
                            f"(nach {counters[external_id]} Full-Crawls ohne Sichtung)[/yellow]"
                        )
                    del counters[external_id]

        state["missing"] = missing_state
        return deleted_count
