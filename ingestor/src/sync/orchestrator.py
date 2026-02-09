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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from src.client.oparl_client import OParlClient, SyncStats
from src.config import settings
from src.events import EventEmitter
from src.metrics import metrics
from src.storage.database import DatabaseStorage
from mandari_oparl import (
    ProcessedAgendaItem,
    ProcessedBody,
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
from src.sync.processor import OParlProcessor

console = Console()


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
        self.processor = OParlProcessor()
        self.max_concurrent = max_concurrent or settings.oparl_max_concurrent
        # Track if we're in parallel mode (disables Rich Progress to avoid conflicts)
        self._parallel_mode = False
        # Event emitter for Redis pub/sub
        self._event_emitter: EventEmitter | None = None

    async def __aenter__(self) -> "SyncOrchestrator":
        """Async context manager entry."""
        await self.storage.initialize()
        # Initialize event emitter
        self._event_emitter = EventEmitter()
        await self._event_emitter.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        # Close event emitter
        if self._event_emitter:
            await self._event_emitter.__aexit__(exc_type, exc_val, exc_tb)
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

        if not system_data:
            raise ValueError(f"Could not fetch OParl system from {url}")

        source_name = name or system_data.get("name", "Unknown Source")

        source_id = await self.storage.upsert_source(
            url=url,
            name=source_name,
            raw_json=system_data,
        )

        console.print(f"[green]Registered source: {source_name} (ID: {source_id})[/green]")
        return source_id

    async def list_sources(self) -> list[tuple[UUID, str, str]]:
        """List all registered sources as (id, name, url) tuples."""
        source = await self.storage.get_source_by_url("")  # Get first
        if source:
            return [(source.id, source.name, source.url)]
        return []

    # ========== URL Auto-Detection ==========

    async def auto_detect_url(
        self,
        client: OParlClient,
        url: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Detect whether a URL points to a Body, Body-List, or System.

        Handles all known OParl server variants:
        - Single Body object (ITK Rheinland: /Oparl/bodies/0015)
        - Body list with data[] wrapper (Köln, Münster, Bonn, Aachen)
        - System object with body reference

        Args:
            client: OParl HTTP client
            url: The URL to detect

        Returns:
            Tuple of (type_name, list_of_body_dicts)

        Raises:
            ValueError: If URL is not a valid OParl endpoint
        """
        result = await client.fetch(url, use_cache=False, skip_wait=True)

        if result.error or not result.data:
            raise ValueError(f"Could not fetch OParl endpoint: {url} ({result.error})")

        response = result.data

        # Case 1: Single Body (ITK Rheinland pattern - no data[] wrapper)
        type_str = response.get("type", "") if isinstance(response, dict) else ""
        if type_str.endswith("/Body"):
            console.print(f"[green]Detected: Single Body object[/green]")
            return "body", [response]

        # Case 2: Body list with data[] wrapper (standard OParl)
        if isinstance(response, dict) and "data" in response:
            items = response["data"]
            if isinstance(items, list) and items:
                first_type = items[0].get("type", "") if isinstance(items[0], dict) else ""
                if first_type.endswith("/Body"):
                    console.print(f"[green]Detected: Body list ({len(items)} bodies)[/green]")
                    return "body_list", items

        # Case 3: System object → follow body reference
        if isinstance(response, dict) and type_str.endswith("/System"):
            body_list_url = response.get("body")
            if body_list_url:
                console.print(f"[green]Detected: System → fetching bodies from {body_list_url}[/green]")
                bodies = await client.fetch_list_all(body_list_url)
                return "system", bodies

        raise ValueError(
            f"URL is neither Body, Body-List, nor System: {url}\n"
            f"Response type: {type_str or 'unknown'}"
        )

    async def sync_body_url(
        self,
        url: str,
        full: bool = False,
        max_concurrent: int | None = None,
    ) -> SyncResult:
        """
        Sync from any OParl URL (auto-detects Body, Body-List, or System).

        This is the primary entry point for the Body-First architecture.

        Args:
            url: OParl URL (Body, Body-List, or System endpoint)
            full: Whether to perform a full sync
            max_concurrent: Max concurrent HTTP requests

        Returns:
            SyncResult with statistics
        """
        start_time = datetime.now(timezone.utc)
        result = SyncResult(source_url=url, source_name="", success=False)
        sync_type = "full" if full else "incremental"

        try:
            concurrent = max_concurrent or self.max_concurrent
            async with OParlClient(
                max_concurrent=concurrent,
                source_name=url.split("/")[2] if "/" in url else "unknown",
            ) as client:
                # Auto-detect URL type
                console.print(f"\n[bold blue]Connecting to {url}...[/bold blue]")
                url_type, bodies_data = await self.auto_detect_url(client, url)

                if not bodies_data:
                    result.errors.append(f"No bodies found at {url}")
                    return result

                # Use first body name as source name
                result.source_name = bodies_data[0].get("name", "Unknown")
                console.print(f"[green]Source: {result.source_name} ({url_type})[/green]")

                # Emit sync started event
                if self._event_emitter:
                    await self._event_emitter.emit_sync_started(
                        source_url=url,
                        source_name=result.source_name,
                        full_sync=full,
                    )

                # Ensure source exists in database (use URL as source identifier)
                source_id = await self.storage.upsert_source(
                    url=url,
                    name=result.source_name,
                    raw_json=bodies_data[0] if len(bodies_data) == 1 else {"bodies_count": len(bodies_data)},
                )

                # Sync bodies
                if len(bodies_data) > 1:
                    self._parallel_mode = True
                    console.print(f"[bold green]Starting PARALLEL sync of {len(bodies_data)} bodies...[/bold green]")

                async def sync_body_wrapper(body_data):
                    try:
                        return await self._sync_body(
                            client=client,
                            body_data=body_data,
                            source_id=source_id,
                            full=full,
                        )
                    except Exception as e:
                        console.print(f"[red]Error syncing {body_data.get('name', 'Unknown')}: {e}[/red]")
                        return {"errors": [str(e)]}

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

                # Update sync timestamp
                await self.storage.update_source_sync_time(source_id, full_sync=full)
                result.http_stats = client.stats
                result.success = True

                total_synced = (
                    result.meetings_synced + result.papers_synced
                    + result.persons_synced + result.organizations_synced
                    + result.memberships_synced + result.files_synced
                    + result.agenda_items_synced + result.consultations_synced
                )

                if self._event_emitter:
                    await self._event_emitter.emit_sync_completed(
                        source_url=url,
                        source_name=result.source_name,
                        duration_seconds=(datetime.now(timezone.utc) - start_time).total_seconds(),
                        entities_synced=total_synced,
                        errors_count=len(result.errors),
                    )

                metrics.record_entities_batch(result.source_name, total_synced)

        except Exception as e:
            result.errors.append(str(e))
            console.print(f"[red]Sync failed: {e}[/red]")

            if self._event_emitter:
                await self._event_emitter.emit_sync_failed(
                    source_url=url,
                    source_name=result.source_name or "Unknown",
                    error=str(e),
                    duration_seconds=(datetime.now(timezone.utc) - start_time).total_seconds(),
                )

        result.duration_seconds = (datetime.now(timezone.utc) - start_time).total_seconds()
        return result

    # ========== Sync Operations ==========

    async def sync_source(
        self,
        url: str,
        full: bool = False,
        body_filter: str | None = None,
    ) -> SyncResult:
        """
        Synchronize a single OParl source.

        Args:
            url: The OParl API URL
            full: Whether to perform a full sync (ignores last_sync)
            body_filter: Optional body name/ID filter

        Returns:
            SyncResult with statistics
        """
        start_time = datetime.now(timezone.utc)
        result = SyncResult(source_url=url, source_name="", success=False)
        sync_type = "full" if full else "incremental"

        try:
            async with OParlClient(
                max_concurrent=self.max_concurrent,
                source_name=url.split("/")[2] if "/" in url else "unknown",
            ) as client:
                # Fetch system
                console.print(f"\n[bold blue]Connecting to {url}...[/bold blue]")
                system_data = await client.fetch_system(url)

                if not system_data:
                    result.errors.append(f"Failed to fetch system from {url}")
                    return result

                result.source_name = system_data.get("name", "Unknown")
                console.print(f"[green]Connected to: {result.source_name}[/green]")

                # Emit sync started event
                if self._event_emitter:
                    await self._event_emitter.emit_sync_started(
                        source_url=url,
                        source_name=result.source_name,
                        full_sync=full,
                    )

                # Ensure source exists in database
                source_id = await self.storage.upsert_source(
                    url=url,
                    name=result.source_name,
                    raw_json=system_data,
                )

                # Get body list URL
                body_list_url = system_data.get("body")
                if not body_list_url:
                    result.errors.append("No body list found in system")
                    return result

                # Fetch all bodies
                console.print("[blue]Fetching bodies list...[/blue]")
                bodies_data = await client.fetch_list_all(body_list_url)
                console.print(f"[dim]Found {len(bodies_data)} bodies[/dim]")

                # Filter bodies if requested
                if body_filter:
                    bodies_data = [
                        b for b in bodies_data
                        if body_filter.lower() in b.get("name", "").lower()
                        or body_filter in b.get("id", "")
                    ]
                    console.print(f"[dim]Filtered to {len(bodies_data)} bodies[/dim]")

                # Sync all bodies IN PARALLEL for massive speedup
                console.print(f"[bold green]Starting PARALLEL sync of {len(bodies_data)} bodies...[/bold green]")

                # Disable Progress bars if syncing multiple bodies in parallel
                # Rich doesn't support multiple live displays simultaneously
                if len(bodies_data) > 1:
                    self._parallel_mode = True

                async def sync_body_wrapper(body_data):
                    """Wrapper to catch exceptions per body."""
                    try:
                        return await self._sync_body(
                            client=client,
                            body_data=body_data,
                            source_id=source_id,
                            full=full,
                        )
                    except Exception as e:
                        console.print(f"[red]Error syncing {body_data.get('name', 'Unknown')}: {e}[/red]")
                        return {"errors": [str(e)]}

                # Run all body syncs in parallel
                body_results = await asyncio.gather(
                    *[sync_body_wrapper(body_data) for body_data in bodies_data],
                    return_exceptions=False
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

                # Update sync timestamp
                await self.storage.update_source_sync_time(source_id, full_sync=full)
                result.http_stats = client.stats
                result.success = True

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

                # Emit sync completed event
                if self._event_emitter:
                    await self._event_emitter.emit_sync_completed(
                        source_url=url,
                        source_name=result.source_name,
                        duration_seconds=(datetime.now(timezone.utc) - start_time).total_seconds(),
                        entities_synced=total_synced,
                        errors_count=len(result.errors),
                    )

                # Record metrics
                metrics.record_entities_batch(result.source_name, total_synced)

        except Exception as e:
            result.errors.append(str(e))
            console.print(f"[red]Sync failed: {e}[/red]")

            # Emit sync failed event
            if self._event_emitter:
                await self._event_emitter.emit_sync_failed(
                    source_url=url,
                    source_name=result.source_name or "Unknown",
                    error=str(e),
                    duration_seconds=(datetime.now(timezone.utc) - start_time).total_seconds(),
                )

        result.duration_seconds = (datetime.now(timezone.utc) - start_time).total_seconds()
        return result

    async def _sync_body(
        self,
        client: OParlClient,
        body_data: dict[str, Any],
        source_id: UUID,
        full: bool,
    ) -> dict[str, Any]:
        """
        Sync a single body and all its entities.

        Returns statistics about synced entities.
        """
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
        }

        body_name = body_data.get("name", "Unknown")
        body_external_id = body_data.get("id", "")

        console.print(f"\n[bold cyan]{'=' * 50}[/bold cyan]")
        console.print(f"[bold cyan]Syncing: {body_name}[/bold cyan]")
        console.print(f"[bold cyan]{'=' * 50}[/bold cyan]")

        # Process and store body
        processed_body = self.processor.process_body(body_data, body_external_id)
        body_id = await self.storage.upsert_body(processed_body, source_id)

        # Determine modified_since for incremental sync
        modified_since: datetime | None = None
        if not full:
            body_db = await self.storage.get_body_by_external_id(body_external_id)
            if body_db and body_db.last_sync:
                modified_since = body_db.last_sync
                console.print(f"[dim]Incremental sync since {modified_since}[/dim]")
        else:
            console.print(f"[dim]Full sync: fetching ALL pages[/dim]")

        # Detect server-side filter support for incremental sync
        use_server_filter = False
        if modified_since and not full:
            use_server_filter = await self._detect_filter_support(
                client, processed_body, modified_since,
            )

        # Helper to build common kwargs for _sync_entity_type
        def sync_kwargs(list_url: str | None, entity_type: str) -> dict:
            return dict(
                client=client,
                list_url=list_url,
                entity_type=entity_type,
                body_id=body_id,
                body_external_id=body_external_id,
                body_name=body_name,
                modified_since=modified_since,
                full=full,
                use_server_filter=use_server_filter,
            )

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
                self._sync_entity_type(**sync_kwargs(processed_body.organization_list_url, "organization")),
                self._sync_entity_type(**sync_kwargs(processed_body.person_list_url, "person")),
                return_exceptions=True,
            )

            if isinstance(org_result, Exception):
                stats["errors"].append(f"Organizations: {org_result}")
            else:
                stats["organizations"] = org_result
                progress.update(task1, completed=org_result, total=org_result)

            if isinstance(person_result, Exception):
                stats["errors"].append(f"Persons: {person_result}")
            else:
                stats["persons"] = person_result
                progress.update(task2, completed=person_result, total=person_result)

            # Second: Memberships (depends on persons + organizations)
            task3 = progress.add_task("[cyan]Memberships...", total=None)
            membership_result = await self._sync_entity_type(
                **sync_kwargs(processed_body.membership_list_url, "membership")
            )
            stats["memberships"] = membership_result
            progress.update(task3, completed=membership_result, total=membership_result)

            # Third: Meetings and Papers (parallel, include nested entities)
            task4 = progress.add_task("[cyan]Meetings...", total=None)
            task5 = progress.add_task("[cyan]Papers...", total=None)

            meeting_result, paper_result = await asyncio.gather(
                self._sync_entity_type(**sync_kwargs(processed_body.meeting_list_url, "meeting")),
                self._sync_entity_type(**sync_kwargs(processed_body.paper_list_url, "paper")),
                return_exceptions=True,
            )

            if isinstance(meeting_result, Exception):
                stats["errors"].append(f"Meetings: {meeting_result}")
            else:
                stats["meetings"] = meeting_result
                progress.update(task4, completed=meeting_result, total=meeting_result)

            if isinstance(paper_result, Exception):
                stats["errors"].append(f"Papers: {paper_result}")
            else:
                stats["papers"] = paper_result
                progress.update(task5, completed=paper_result, total=paper_result)

            # Fourth: Locations, AgendaItems, Files, Consultations (parallel)
            task6 = progress.add_task("[cyan]Locations...", total=None)
            task7 = progress.add_task("[cyan]AgendaItems...", total=None)
            task8 = progress.add_task("[cyan]Files...", total=None)
            task9 = progress.add_task("[cyan]Consultations...", total=None)

            location_result, agenda_item_result, file_result, consultation_result = await asyncio.gather(
                self._sync_entity_type(**sync_kwargs(processed_body.location_list_url, "location")),
                self._sync_entity_type(**sync_kwargs(processed_body.agenda_item_list_url, "agendaitem")),
                self._sync_entity_type(**sync_kwargs(processed_body.file_list_url, "file")),
                self._sync_entity_type(**sync_kwargs(processed_body.consultation_list_url, "consultation")),
                return_exceptions=True,
            )

            if isinstance(location_result, Exception):
                stats["errors"].append(f"Locations: {location_result}")
            else:
                stats["locations"] = location_result
                progress.update(task6, completed=location_result, total=location_result)

            if isinstance(agenda_item_result, Exception):
                stats["errors"].append(f"AgendaItems: {agenda_item_result}")
            else:
                stats["agenda_items"] = agenda_item_result
                progress.update(task7, completed=agenda_item_result, total=agenda_item_result)

            if isinstance(file_result, Exception):
                stats["errors"].append(f"Files: {file_result}")
            else:
                stats["files"] = file_result
                progress.update(task8, completed=file_result, total=file_result)

            if isinstance(consultation_result, Exception):
                stats["errors"].append(f"Consultations: {consultation_result}")
            else:
                stats["consultations"] = consultation_result
                progress.update(task9, completed=consultation_result, total=consultation_result)

        # Update body sync time
        await self.storage.update_body_sync_time(body_id)

        # Print summary
        console.print(f"[green]Body sync complete:[/green]")
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

    async def _detect_filter_support(
        self,
        client: OParlClient,
        processed_body: ProcessedBody,
        modified_since: datetime,
    ) -> bool:
        """
        Test if the OParl server supports ?modified_since= query parameter.

        Tests by fetching one page with the filter and validating that all
        returned items have modified dates >= modified_since.

        Live test results (2026-02-09):
        - Münster: ✅ all entity types
        - Bonn, Aachen, Köln, Neuss: ✅ papers
        - Düsseldorf: ❌ papers broken (returns old items), meetings OK

        Args:
            client: OParl HTTP client
            processed_body: The body being synced
            modified_since: The timestamp to test with

        Returns:
            True if server correctly supports modified_since
        """
        # Pick a test endpoint (prefer papers, fall back to meetings)
        test_url = processed_body.paper_list_url or processed_body.meeting_list_url
        if not test_url:
            console.print("[dim]  No endpoint to test filter support[/dim]")
            return False

        try:
            test_items: list[dict[str, Any]] = []
            async for page in client.fetch_list(test_url, modified_since=modified_since, max_pages=1):
                test_items.extend(page)
                break

            if not test_items:
                # Empty result is valid - no items modified since last sync
                console.print("[green]  Filter test: ✅ Server supports modified_since (0 items returned)[/green]")
                return True

            # Validate: ALL items should have modified >= modified_since
            all_valid = True
            for item in test_items:
                item_modified = self.processor.parse_datetime(item.get("modified"))
                if item_modified and item_modified < modified_since:
                    all_valid = False
                    break

            if all_valid:
                console.print(
                    f"[green]  Filter test: ✅ Server supports modified_since "
                    f"({len(test_items)} items, all valid)[/green]"
                )
            else:
                console.print(
                    f"[yellow]  Filter test: ❌ Server returns items older than filter date. "
                    f"Falling back to client-side filtering.[/yellow]"
                )

            return all_valid

        except Exception as e:
            console.print(f"[yellow]  Filter test failed: {e}. Using client-side filtering.[/yellow]")
            return False

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
        use_server_filter: bool = False,
    ) -> int:
        """
        Sync all entities of a specific type.

        For full sync:
        - Fetches all pages and upserts everything

        For incremental sync with server filter (use_server_filter=True):
        - Appends ?modified_since= to URL → server returns only changed items
        - Upserts all returned items (they are all new/modified)
        - Handles deleted items (OParl deleted=true flag)
        - No stop condition needed (server filters for us)

        For incremental sync without server filter (fallback):
        - Fetches pages and compares modified dates against DB
        - New items → Save, Modified items → Update, Deleted → Delete
        - Stops after 5 consecutive pages with no changes

        Returns the number of entities synced (new + updated).
        """
        if not list_url:
            return 0

        count = 0
        updated_count = 0
        deleted_count = 0
        skipped_count = 0
        pages_checked = 0
        consecutive_stale_pages = 0
        min_pages_to_check = 10
        stale_pages_to_stop = 5

        # Pass modified_since to client only if server supports it
        client_modified_since = modified_since if use_server_filter else None

        async for page in client.fetch_list(list_url, modified_since=client_modified_since):
            pages_checked += 1
            new_on_page = 0
            updated_on_page = 0
            deleted_on_page = 0
            unchanged_on_page = 0
            page_size = len(page)

            if full:
                # Full sync: upsert everything
                for item in page:
                    try:
                        processed = self.processor.process(item, body_external_id)
                        if processed:
                            await self._store_entity(processed, body_id, entity_type, body_name)
                            count += 1
                    except Exception as e:
                        console.print(f"[red]Error processing {entity_type}: {e}[/red]")

            elif use_server_filter:
                # Server-filtered incremental: all items are new/modified/deleted
                for item in page:
                    try:
                        external_id = item.get("id", "")
                        if not external_id:
                            continue

                        # Handle deleted items
                        if item.get("deleted") is True:
                            if await self.storage.delete_entity(entity_type, external_id):
                                deleted_on_page += 1
                                deleted_count += 1
                            continue

                        # Upsert (the server already filtered to modified items)
                        processed = self.processor.process(item, body_external_id)
                        if processed:
                            await self._store_entity(processed, body_id, entity_type, body_name)
                            count += 1
                            new_on_page += 1

                    except Exception as e:
                        console.print(f"[red]Error processing {entity_type}: {e}[/red]")

            else:
                # Client-side incremental: compare modified dates against DB
                external_ids = [item.get("id", "") for item in page if item.get("id")]
                existing_ids = await self.storage.batch_check_entities_exist(
                    entity_type, external_ids
                )

                for item in page:
                    try:
                        external_id = item.get("id", "")
                        if not external_id:
                            continue

                        # Handle deleted items (Bonn/Aachen/Köln/ITK send deleted flag)
                        if item.get("deleted") is True:
                            if await self.storage.delete_entity(entity_type, external_id):
                                deleted_on_page += 1
                                deleted_count += 1
                            continue

                        db_modified = existing_ids.get(external_id)
                        item_modified = self.processor.parse_datetime(item.get("modified"))

                        if db_modified is None:
                            # New item → save
                            new_on_page += 1
                            processed = self.processor.process(item, body_external_id)
                            if processed:
                                await self._store_entity(processed, body_id, entity_type, body_name)
                                count += 1
                        elif item_modified and db_modified and item_modified > db_modified:
                            # Modified item → update (upsert handles ON CONFLICT)
                            updated_on_page += 1
                            processed = self.processor.process(item, body_external_id)
                            if processed:
                                await self._store_entity(processed, body_id, entity_type, body_name)
                                updated_count += 1
                        else:
                            # Unchanged → skip
                            unchanged_on_page += 1
                            skipped_count += 1

                    except Exception as e:
                        console.print(f"[red]Error processing {entity_type}: {e}[/red]")

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

                # Stop condition: only for client-side filtering (server-filtered has no stale pages)
                if not use_server_filter:
                    if (pages_checked >= min_pages_to_check
                            and consecutive_stale_pages >= stale_pages_to_stop):
                        console.print(
                            f"[yellow]  Stopping {entity_type}: "
                            f"{consecutive_stale_pages} consecutive pages without changes[/yellow]"
                        )
                        break

        total = count + updated_count
        if not full and (updated_count > 0 or deleted_count > 0 or skipped_count > 0):
            console.print(
                f"[dim]  {entity_type} summary: {count} new, {updated_count} updated, "
                f"{deleted_count} deleted, {skipped_count} unchanged[/dim]"
            )

        return total

    async def _store_entity(
        self,
        entity: ProcessedEntity,
        body_id: UUID,
        entity_type: str,
        body_name: str | None = None,
    ) -> None:
        """Store a processed entity to the database and emit events."""
        entity_id: str | None = None

        if isinstance(entity, ProcessedMeeting):
            entity_id = str(await self.storage.upsert_meeting(entity, body_id))
            meeting_uuid = UUID(entity_id)
            # Emit high-priority event for new meetings
            if self._event_emitter and entity_id:
                await self._event_emitter.emit_new_meeting(
                    meeting_id=entity_id,
                    external_id=entity.external_id,
                    name=entity.name or "Unbekannte Sitzung",
                    body_name=body_name,
                    start_time=entity.start,
                )
            # Process nested entities (files, agenda items) with meeting_id
            for nested in entity.nested_entities:
                if isinstance(nested, ProcessedFile):
                    await self.storage.upsert_file(nested, body_id, meeting_id=meeting_uuid)
                    metrics.record_entity_synced("file", body_name or "unknown")
                elif isinstance(nested, ProcessedAgendaItem):
                    await self.storage.upsert_agenda_item(nested, meeting_uuid)
                    metrics.record_entity_synced("agendaitem", body_name or "unknown")
        elif isinstance(entity, ProcessedPaper):
            entity_id = str(await self.storage.upsert_paper(entity, body_id))
            paper_uuid = UUID(entity_id)
            # Emit high-priority event for new papers
            if self._event_emitter and entity_id:
                await self._event_emitter.emit_new_paper(
                    paper_id=entity_id,
                    external_id=entity.external_id,
                    name=entity.name or "Unbekannte Vorlage",
                    body_name=body_name,
                    paper_type=entity.paper_type,
                )
            # Process nested entities (files, consultations) with paper_id
            for nested in entity.nested_entities:
                if isinstance(nested, ProcessedFile):
                    await self.storage.upsert_file(nested, body_id, paper_id=paper_uuid)
                    metrics.record_entity_synced("file", body_name or "unknown")
                elif isinstance(nested, ProcessedConsultation):
                    await self.storage.upsert_consultation(nested, body_id, paper_uuid)
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
            await self.storage.upsert_membership(entity, body_id)
            metrics.record_entity_synced("membership", body_name or "unknown")
        elif isinstance(entity, ProcessedLocation):
            await self.storage.upsert_location(entity, body_id)
            metrics.record_entity_synced("location", body_name or "unknown")
        elif isinstance(entity, ProcessedAgendaItem):
            # AgendaItems need meeting_id - try to look it up
            meeting_id = None
            if entity.meeting_external_id:
                meeting_id = await self.storage.get_meeting_uuid(entity.meeting_external_id)
            if meeting_id:
                await self.storage.upsert_agenda_item(entity, meeting_id)
                metrics.record_entity_synced("agendaitem", body_name or "unknown")
            # If no meeting_id found, skip (item likely from nested sync already)
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

        console.print(f"[bold]Syncing {len(sources)} sources...[/bold]")

        if parallel:
            # Sync all sources in PARALLEL for massive speedup
            # Disable Rich Progress bars to avoid "Only one live display" error
            self._parallel_mode = True

            async def sync_source_wrapper(source):
                """Wrapper to catch exceptions per source."""
                try:
                    return await self.sync_source(source.url, full=full)
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
                    *[sync_source_wrapper(source) for source in sources],
                    return_exceptions=False
                )
                return list(results)
            finally:
                self._parallel_mode = False
        else:
            # Sequential sync (fallback)
            results = []
            for source in sources:
                result = await self.sync_source(source.url, full=full)
                results.append(result)
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
