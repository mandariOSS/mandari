"""
Mandari Ingestor - CLI Entry Point

Command-line interface for OParl data synchronization.

Usage:
    # Add a new source
    uv run python -m src.main add-source --url https://example.oparl.org/oparl/v1

    # Run incremental sync
    uv run python -m src.main sync --source https://example.oparl.org/oparl/v1

    # Run full sync
    uv run python -m src.main sync --source https://example.oparl.org/oparl/v1 --full

    # Show status
    uv run python -m src.main status
"""

import asyncio
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.config import settings
from src.sync.orchestrator import SyncOrchestrator

app = typer.Typer(
    name="mandari-ingestor",
    help="OParl data synchronization service for Mandari",
    add_completion=False,
)
console = Console()


def print_banner() -> None:
    """Print the application banner."""
    console.print(Panel.fit(
        "[bold blue]Mandari OParl Ingestor[/bold blue]\n"
        "[dim]High-performance sync service for municipal data[/dim]",
        border_style="blue",
    ))
    console.print()


@app.command()
def sync(
    body_url: Optional[list[str]] = typer.Option(
        None, "--body", "-b",
        help="Direct OParl Body URL(s). Auto-detects Body, Body-List, or System URLs.",
    ),
    source_url: Optional[str] = typer.Option(
        None, "--source", "-s",
        help="Legacy: OParl System URL (prefer --body for direct Body URLs)",
    ),
    full: bool = typer.Option(
        False, "--full", "-f", help="Perform full sync (ignore last sync timestamp)"
    ),
    all_sources: bool = typer.Option(
        False, "--all", "-a", help="Sync all registered sources"
    ),
    body_filter: Optional[str] = typer.Option(
        None, "--filter", help="Filter by body name when using --source (partial match)"
    ),
    max_concurrent: int = typer.Option(
        10, "--concurrent", "-c", help="Maximum concurrent HTTP requests"
    ),
) -> None:
    """
    Synchronize OParl data.

    Supports three input modes:
    1. --body URL  (recommended): Direct Body URL, auto-detects type
    2. --source URL (legacy): System URL, discovers bodies
    3. --all: Sync all registered sources

    Examples:

        # Sync a direct Body URL (recommended)
        mandari-ingestor sync --body https://ris-oparl.itk-rheinland.de/Oparl/bodies/0015

        # Sync multiple bodies
        mandari-ingestor sync --body URL1 --body URL2

        # Sync a Body-List URL (auto-detected)
        mandari-ingestor sync --body https://oparl.stadt-muenster.de/bodies

        # Full sync
        mandari-ingestor sync --body URL --full

        # Legacy: Sync via system URL
        mandari-ingestor sync --source https://example.oparl.org/oparl/system

        # Sync all registered sources
        mandari-ingestor sync --all
    """
    print_banner()

    if not body_url and not source_url and not all_sources:
        console.print("[red]Error:[/red] Please specify --body URL, --source URL, or --all")
        console.print("\nExamples:")
        console.print("  mandari-ingestor sync --body https://oparl.stadt-muenster.de/bodies")
        console.print("  mandari-ingestor sync --body https://ris-oparl.itk-rheinland.de/Oparl/bodies/0015")
        console.print("  mandari-ingestor sync --source https://example.oparl.org/oparl/system")
        console.print("  mandari-ingestor sync --all")
        raise typer.Exit(1)

    mode = "[bold green]Full Sync[/bold green]" if full else "[bold cyan]Incremental Sync[/bold cyan]"
    console.print(f"Mode: {mode}")
    console.print(f"Concurrent requests: {max_concurrent}")
    console.print()

    async def run_sync() -> None:
        async with SyncOrchestrator(max_concurrent=max_concurrent) as orchestrator:
            if all_sources:
                console.print("[blue]Syncing all registered sources...[/blue]")
                results = await orchestrator.sync_all(full=full)
                for result in results:
                    orchestrator.print_result(result)
            elif body_url:
                # Body-First: sync each body URL via auto-detection
                for url in body_url:
                    console.print(f"[blue]Syncing: {url}[/blue]")
                    result = await orchestrator.sync_body_url(
                        url=url,
                        full=full,
                        max_concurrent=max_concurrent,
                    )
                    orchestrator.print_result(result)
            else:
                # Legacy: system URL
                assert source_url is not None
                result = await orchestrator.sync_source(
                    url=source_url,
                    full=full,
                    body_filter=body_filter,
                )
                orchestrator.print_result(result)

    try:
        asyncio.run(run_sync())
    except KeyboardInterrupt:
        console.print("\n[yellow]Sync interrupted by user[/yellow]")
        raise typer.Exit(130)
    except Exception as e:
        console.print(f"\n[red]Sync failed: {e}[/red]")
        raise typer.Exit(1)


@app.command("add-source")
def add_source(
    url: str = typer.Argument(..., help="OParl API URL (system endpoint)"),
    name: Optional[str] = typer.Option(
        None, "--name", "-n", help="Display name (auto-detected if not specified)"
    ),
) -> None:
    """
    Register a new OParl source.

    Fetches the system endpoint to auto-detect the source name.

    Examples:

        # Add source with auto-detected name
        mandari-ingestor add-source https://example.oparl.org/oparl/v1

        # Add source with custom name
        mandari-ingestor add-source https://example.oparl.org/oparl/v1 --name "Stadt Example"
    """
    print_banner()
    console.print(f"[bold]Adding OParl source:[/bold] {url}")
    console.print()

    async def run_add() -> None:
        async with SyncOrchestrator() as orchestrator:
            source_id = await orchestrator.add_source(url, name)
            console.print()
            console.print("[green]Source registered successfully![/green]")
            console.print(f"Source ID: {source_id}")
            console.print()
            console.print("[dim]Run sync to fetch data:[/dim]")
            console.print(f"  mandari-ingestor sync --source {url}")

    try:
        asyncio.run(run_add())
    except Exception as e:
        console.print(f"\n[red]Failed to add source: {e}[/red]")
        raise typer.Exit(1)


@app.command("list-sources")
def list_sources() -> None:
    """
    List all registered OParl sources.

    Shows source name, URL, last sync time, and entity counts.
    """
    print_banner()

    async def run_list() -> None:
        async with SyncOrchestrator() as orchestrator:
            bodies = await orchestrator.storage.get_all_bodies()

            if not bodies:
                console.print("[yellow]No sources registered yet.[/yellow]")
                console.print()
                console.print("[dim]Add a source:[/dim]")
                console.print("  mandari-ingestor add-source https://example.oparl.org/oparl/v1")
                return

            table = Table(title="Registered Bodies")
            table.add_column("Name", style="green")
            table.add_column("External ID", style="dim")
            table.add_column("Last Sync")
            table.add_column("Classification")

            for body in bodies:
                table.add_row(
                    body.name,
                    body.external_id[:50] + "..." if len(body.external_id) > 50 else body.external_id,
                    str(body.last_sync.strftime("%Y-%m-%d %H:%M")) if body.last_sync else "Never",
                    body.classification or "-",
                )

            console.print(table)

    asyncio.run(run_list())


@app.command()
def status() -> None:
    """
    Show sync status and database statistics.

    Displays counts for all entity types and database connection status.
    """
    print_banner()

    async def run_status() -> None:
        try:
            async with SyncOrchestrator() as orchestrator:
                status_data = await orchestrator.get_status()
                stats = status_data["database_stats"]

                console.print("[bold]Database Statistics:[/bold]")
                console.print()

                table = Table(show_header=True, header_style="bold")
                table.add_column("Entity Type", style="cyan")
                table.add_column("Count", justify="right", style="green")

                for entity_type, count in stats.items():
                    table.add_row(entity_type.replace("_", " ").title(), f"{count:,}")

                console.print(table)
                console.print()

                # Calculate totals
                total = sum(stats.values())
                console.print(f"[bold]Total entities:[/bold] {total:,}")

        except Exception as e:
            console.print(f"[red]Could not connect to database: {e}[/red]")
            console.print()
            console.print("[dim]Make sure PostgreSQL is running:[/dim]")
            console.print("  docker compose -f infrastructure/docker/docker-compose.dev.yml up -d")

    console.print("[bold]Configuration:[/bold]")
    console.print(f"  Database: {settings.database_url.split('@')[-1] if '@' in settings.database_url else settings.database_url}")
    console.print(f"  Redis: {settings.redis_url}")
    console.print(f"  Meilisearch: {settings.meilisearch_url}")
    console.print()

    asyncio.run(run_status())


@app.command()
def init_db() -> None:
    """
    Initialize the database schema.

    Creates all required tables if they don't exist.
    """
    print_banner()
    console.print("[blue]Initializing database schema...[/blue]")

    async def run_init() -> None:
        async with SyncOrchestrator() as orchestrator:
            console.print("[green]Database schema initialized successfully![/green]")

    try:
        asyncio.run(run_init())
    except Exception as e:
        console.print(f"[red]Failed to initialize database: {e}[/red]")
        console.print()
        console.print("[dim]Make sure PostgreSQL is running:[/dim]")
        console.print("  docker compose -f infrastructure/docker/docker-compose.dev.yml up -d")
        raise typer.Exit(1)


@app.command()
def daemon(
    interval: int = typer.Option(
        15, "--interval", "-i", help="Minutes between incremental syncs"
    ),
    full_sync_hour: int = typer.Option(
        3, "--full-sync-hour", help="Hour of day for full sync (24h format)"
    ),
    max_concurrent: int = typer.Option(
        10, "--concurrent", "-c", help="Maximum concurrent HTTP requests"
    ),
    metrics_port: int = typer.Option(
        9090, "--metrics-port", help="Port for Prometheus metrics server"
    ),
) -> None:
    """
    Start the sync scheduler daemon.

    Runs continuously, performing:
    - Incremental syncs at regular intervals (default: every 15 minutes)
    - Full syncs once daily (default: 3 AM)
    - Exposes Prometheus metrics on /metrics endpoint

    Use Ctrl+C to stop the daemon gracefully.

    Examples:

        # Start with default settings (15 min incremental, 3 AM full sync)
        mandari-ingestor daemon

        # Sync every 5 minutes
        mandari-ingestor daemon --interval 5

        # Full sync at midnight
        mandari-ingestor daemon --full-sync-hour 0

        # Custom metrics port
        mandari-ingestor daemon --metrics-port 8080
    """
    print_banner()

    console.print("[bold]Starting Sync Daemon[/bold]")
    console.print(f"  Incremental sync: every {interval} minutes")
    console.print(f"  Full sync: daily at {full_sync_hour:02d}:00")
    console.print(f"  Concurrent requests: {max_concurrent}")
    console.print(f"  Metrics server: http://0.0.0.0:{metrics_port}/metrics")
    console.print()

    from src.scheduler import run_scheduler

    try:
        asyncio.run(run_scheduler(
            sync_interval=interval,
            full_sync_hour=full_sync_hour,
            max_concurrent=max_concurrent,
            metrics_port=metrics_port,
        ))
    except KeyboardInterrupt:
        console.print("\n[yellow]Daemon stopped by user[/yellow]")
        raise typer.Exit(130)


@app.command()
def test_connection(
    url: str = typer.Argument(..., help="OParl API URL to test"),
) -> None:
    """
    Test connection to an OParl API.

    Fetches the system endpoint and displays information.
    Does not modify the database.
    """
    print_banner()
    console.print(f"[blue]Testing connection to:[/blue] {url}")
    console.print()

    async def run_test() -> None:
        from src.client.oparl_client import OParlClient

        async with OParlClient(max_concurrent=1) as client:
            system_data = await client.fetch_system(url)

            if not system_data:
                console.print("[red]Failed to fetch system data[/red]")
                raise typer.Exit(1)

            console.print("[green]Connection successful![/green]")
            console.print()

            console.print("[bold]System Information:[/bold]")
            console.print(f"  Name: {system_data.get('name', 'Unknown')}")
            console.print(f"  OParl Version: {system_data.get('oparlVersion', 'Unknown')}")
            console.print(f"  Contact Email: {system_data.get('contactEmail', 'N/A')}")
            console.print(f"  Contact Name: {system_data.get('contactName', 'N/A')}")
            console.print(f"  Website: {system_data.get('website', 'N/A')}")
            console.print()

            body_url = system_data.get("body")
            if body_url:
                console.print(f"[dim]Body list URL: {body_url}[/dim]")

                # Fetch bodies
                bodies = await client.fetch_list_all(body_url)
                console.print(f"[bold]Found {len(bodies)} bodies:[/bold]")
                for body in bodies[:5]:
                    console.print(f"  - {body.get('name', 'Unknown')}")
                if len(bodies) > 5:
                    console.print(f"  ... and {len(bodies) - 5} more")

    try:
        asyncio.run(run_test())
    except Exception as e:
        console.print(f"[red]Connection failed: {e}[/red]")
        raise typer.Exit(1)


@app.command("init-sources")
def init_sources(
    priority: int = typer.Option(
        1, "--priority", "-p", help="Add sources up to this priority level (1=major cities, 2=medium, 3=all)"
    ),
    default_only: bool = typer.Option(
        False, "--default", "-d", help="Only add default recommended sources"
    ),
) -> None:
    """
    Initialize all known German OParl sources.

    Adds predefined OParl sources from German municipalities to the database.
    Sources are prioritized by city size and API reliability.

    Priority levels:
    - 1: Major cities (Köln, Düsseldorf, Dresden, München, etc.)
    - 2: Medium cities and districts
    - 3: All municipalities (including smaller ones)

    Examples:

        # Add only major cities (recommended for getting started)
        mandari-ingestor init-sources --priority 1

        # Add default reliable sources
        mandari-ingestor init-sources --default

        # Add all known sources
        mandari-ingestor init-sources --priority 3
    """
    print_banner()

    from src.sources import get_all_sources, get_default_sources

    if default_only:
        sources = get_default_sources()
        console.print("[bold]Adding default recommended sources...[/bold]")
    else:
        all_sources = get_all_sources()
        sources = [s for s in all_sources if s.priority <= priority]
        console.print(f"[bold]Adding sources with priority <= {priority}...[/bold]")

    console.print(f"[dim]Found {len(sources)} sources to add[/dim]")
    console.print()

    async def run_init() -> None:
        async with SyncOrchestrator() as orchestrator:
            added = 0
            failed = 0

            for source in sources:
                try:
                    console.print(f"[blue]Adding:[/blue] {source.name}")
                    await orchestrator.add_source(source.url, source.name)
                    added += 1
                except Exception as e:
                    console.print(f"[red]  Failed: {e}[/red]")
                    failed += 1

            console.print()
            console.print(f"[bold green]Added {added} sources successfully[/bold green]")
            if failed > 0:
                console.print(f"[yellow]Failed to add {failed} sources[/yellow]")

            console.print()
            console.print("[dim]Start syncing with:[/dim]")
            console.print("  mandari-ingestor sync --all")
            console.print()
            console.print("[dim]Or start the daemon:[/dim]")
            console.print("  mandari-ingestor daemon")

    try:
        asyncio.run(run_init())
    except Exception as e:
        console.print(f"\n[red]Initialization failed: {e}[/red]")
        raise typer.Exit(1)


@app.command("metrics")
def show_metrics() -> None:
    """
    Show current metrics (for debugging without Prometheus).

    Displays in-memory metrics including HTTP request counts,
    entity sync counts, and error rates.
    """
    print_banner()

    from src.metrics import metrics

    console.print("[bold]Current Metrics:[/bold]")
    console.print()

    data = metrics.get_simple_metrics()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right", style="green")

    table.add_row("HTTP Requests", f"{data['http_requests_total']:,}")
    table.add_row("HTTP Errors", f"{data['http_errors_total']:,}")
    table.add_row("Avg Request Duration", f"{data['http_avg_duration_seconds']:.3f}s")
    table.add_row("Entities Synced (Total)", f"{data['entities_synced_total']:,}")
    table.add_row("Sync Runs", f"{data['sync_runs_total']:,}")
    table.add_row("Sync Errors", f"{data['sync_errors_total']:,}")
    table.add_row("Active Syncs", f"{data['active_syncs']}")

    console.print(table)

    if data["entities_by_type"]:
        console.print()
        console.print("[bold]Entities by Type:[/bold]")
        for entity_type, count in sorted(data["entities_by_type"].items()):
            console.print(f"  {entity_type}: {count:,}")


@app.command("circuit-breakers")
def show_circuit_breakers() -> None:
    """
    Show circuit breaker status for all sources.

    Displays the current state of circuit breakers protecting
    against failing OParl API endpoints.
    """
    print_banner()

    from src.circuit_breaker import circuit_breakers

    async def get_status():
        return await circuit_breakers.get_all_status()

    statuses = asyncio.run(get_status())

    if not statuses:
        console.print("[yellow]No circuit breakers active (no requests made yet)[/yellow]")
        return

    console.print("[bold]Circuit Breaker Status:[/bold]")
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Source", style="cyan")
    table.add_column("State", style="green")
    table.add_column("Failures")
    table.add_column("Successes")
    table.add_column("Timeout")

    for status in statuses:
        state_style = {
            "closed": "green",
            "open": "red",
            "half_open": "yellow",
        }.get(status["state"], "white")

        table.add_row(
            status["name"],
            f"[{state_style}]{status['state']}[/{state_style}]",
            str(status["failure_count"]),
            str(status["success_count"]),
            f"{status['remaining_timeout']:.1f}s" if status["remaining_timeout"] else "-",
        )

    console.print(table)


@app.callback()
def main() -> None:
    """
    Mandari OParl Ingestor - High-performance sync service for municipal data.

    This tool synchronizes OParl data from municipal information systems
    into a local PostgreSQL database for fast access.

    Features:
    - Event emission via Redis for real-time updates
    - Prometheus metrics for monitoring
    - Circuit breakers for resilience

    Use 'mandari-ingestor COMMAND --help' for more information on a command.
    """
    pass


if __name__ == "__main__":
    app()
