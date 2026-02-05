"""
Prometheus Metrics Module

Exposes metrics for monitoring the OParl sync service.

Metrics:
- Counters: requests, entities synced, errors
- Histograms: request duration, sync duration
- Gauges: active syncs, circuit breaker status

Usage:
    from src.metrics import metrics

    # Record HTTP request
    metrics.record_http_request(source="muenster", status=200, duration=0.5)

    # Record entity sync
    metrics.record_entity_synced(entity_type="meeting", source="muenster")

    # Start metrics server
    await metrics.start_server(port=9090)
"""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from rich.console import Console

console = Console()

# Try to import prometheus_client, gracefully degrade if not available
try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )
    from prometheus_client.exposition import basic_auth_handler
    import aiohttp.web as web

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    console.print("[yellow]prometheus_client not installed, metrics disabled[/yellow]")


@dataclass
class SimpleMetrics:
    """Simple in-memory metrics when Prometheus is not available."""

    http_requests: int = 0
    http_errors: int = 0
    http_total_duration: float = 0.0
    entities_synced: dict[str, int] = field(default_factory=dict)
    sync_runs: int = 0
    sync_errors: int = 0
    active_syncs: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON output."""
        return {
            "http_requests_total": self.http_requests,
            "http_errors_total": self.http_errors,
            "http_avg_duration_seconds": (
                self.http_total_duration / max(self.http_requests, 1)
            ),
            "entities_synced_total": sum(self.entities_synced.values()),
            "entities_by_type": self.entities_synced,
            "sync_runs_total": self.sync_runs,
            "sync_errors_total": self.sync_errors,
            "active_syncs": self.active_syncs,
        }


class MetricsCollector:
    """
    Prometheus metrics collector for the OParl ingestor.

    Provides both Prometheus metrics (if available) and simple in-memory
    metrics as fallback.
    """

    def __init__(self, enabled: bool = True) -> None:
        """
        Initialize metrics collector.

        Args:
            enabled: Whether to collect metrics
        """
        self.enabled = enabled
        self.simple = SimpleMetrics()
        self._server_task: asyncio.Task | None = None

        if PROMETHEUS_AVAILABLE and enabled:
            self._init_prometheus()
        else:
            self._prometheus_enabled = False

    def _init_prometheus(self) -> None:
        """Initialize Prometheus metrics."""
        self._prometheus_enabled = True
        self.registry = CollectorRegistry()

        # HTTP Request metrics
        self.http_requests_total = Counter(
            "mandari_ingestor_http_requests_total",
            "Total HTTP requests made",
            ["source", "status"],
            registry=self.registry,
        )

        self.http_request_duration = Histogram(
            "mandari_ingestor_http_request_duration_seconds",
            "HTTP request duration in seconds",
            ["source"],
            buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
            registry=self.registry,
        )

        self.http_errors_total = Counter(
            "mandari_ingestor_http_errors_total",
            "Total HTTP errors",
            ["source", "error_type"],
            registry=self.registry,
        )

        # Entity sync metrics
        self.entities_synced_total = Counter(
            "mandari_ingestor_entities_synced_total",
            "Total entities synced",
            ["entity_type", "source", "action"],
            registry=self.registry,
        )

        self.entities_per_sync = Histogram(
            "mandari_ingestor_entities_per_sync",
            "Number of entities synced per run",
            ["source"],
            buckets=(10, 50, 100, 500, 1000, 5000, 10000, 50000),
            registry=self.registry,
        )

        # Sync operation metrics
        self.sync_duration = Histogram(
            "mandari_ingestor_sync_duration_seconds",
            "Sync operation duration in seconds",
            ["source", "sync_type"],
            buckets=(10, 30, 60, 120, 300, 600, 1800, 3600),
            registry=self.registry,
        )

        self.sync_runs_total = Counter(
            "mandari_ingestor_sync_runs_total",
            "Total sync runs",
            ["source", "sync_type", "status"],
            registry=self.registry,
        )

        self.active_syncs = Gauge(
            "mandari_ingestor_active_syncs",
            "Number of currently active sync operations",
            registry=self.registry,
        )

        # Circuit breaker metrics
        self.circuit_breaker_state = Gauge(
            "mandari_ingestor_circuit_breaker_state",
            "Circuit breaker state (0=closed, 1=open, 2=half-open)",
            ["source"],
            registry=self.registry,
        )

        self.circuit_breaker_failures = Counter(
            "mandari_ingestor_circuit_breaker_failures_total",
            "Circuit breaker failure count",
            ["source"],
            registry=self.registry,
        )

        # Cache metrics
        self.cache_hits_total = Counter(
            "mandari_ingestor_cache_hits_total",
            "Total cache hits (ETag/304 responses)",
            ["source"],
            registry=self.registry,
        )

        console.print("[dim]Prometheus metrics initialized[/dim]")

    # ========== HTTP Metrics ==========

    def record_http_request(
        self,
        source: str,
        status: int,
        duration: float,
        from_cache: bool = False,
    ) -> None:
        """Record an HTTP request."""
        if not self.enabled:
            return

        # Simple metrics (always)
        self.simple.http_requests += 1
        self.simple.http_total_duration += duration

        # Prometheus metrics
        if self._prometheus_enabled:
            self.http_requests_total.labels(source=source, status=str(status)).inc()
            self.http_request_duration.labels(source=source).observe(duration)
            if from_cache:
                self.cache_hits_total.labels(source=source).inc()

    def record_http_error(
        self,
        source: str,
        error_type: str,
    ) -> None:
        """Record an HTTP error."""
        if not self.enabled:
            return

        self.simple.http_errors += 1

        if self._prometheus_enabled:
            self.http_errors_total.labels(source=source, error_type=error_type).inc()

    # ========== Entity Metrics ==========

    def record_entity_synced(
        self,
        entity_type: str,
        source: str,
        action: str = "created",
    ) -> None:
        """Record an entity sync."""
        if not self.enabled:
            return

        # Simple metrics
        self.simple.entities_synced[entity_type] = (
            self.simple.entities_synced.get(entity_type, 0) + 1
        )

        # Prometheus metrics
        if self._prometheus_enabled:
            self.entities_synced_total.labels(
                entity_type=entity_type,
                source=source,
                action=action,
            ).inc()

    def record_entities_batch(
        self,
        source: str,
        count: int,
    ) -> None:
        """Record a batch of entities synced."""
        if not self.enabled or not self._prometheus_enabled:
            return

        self.entities_per_sync.labels(source=source).observe(count)

    # ========== Sync Metrics ==========

    @asynccontextmanager
    async def track_sync(
        self,
        source: str,
        sync_type: str = "incremental",
    ) -> AsyncIterator[None]:
        """
        Context manager to track sync duration and status.

        Usage:
            async with metrics.track_sync("muenster", "full"):
                await do_sync()
        """
        start_time = time.time()
        self.simple.active_syncs += 1
        self.simple.sync_runs += 1

        if self._prometheus_enabled:
            self.active_syncs.inc()

        try:
            yield
            status = "success"
        except Exception:
            status = "error"
            self.simple.sync_errors += 1
            raise
        finally:
            duration = time.time() - start_time
            self.simple.active_syncs -= 1

            if self._prometheus_enabled:
                self.active_syncs.dec()
                self.sync_duration.labels(source=source, sync_type=sync_type).observe(
                    duration
                )
                self.sync_runs_total.labels(
                    source=source, sync_type=sync_type, status=status
                ).inc()

    # ========== Circuit Breaker Metrics ==========

    def record_circuit_breaker_state(
        self,
        source: str,
        state: str,
    ) -> None:
        """Record circuit breaker state change."""
        if not self.enabled or not self._prometheus_enabled:
            return

        state_value = {"closed": 0, "open": 1, "half_open": 2}.get(state, 0)
        self.circuit_breaker_state.labels(source=source).set(state_value)

    def record_circuit_breaker_failure(self, source: str) -> None:
        """Record circuit breaker failure."""
        if not self.enabled or not self._prometheus_enabled:
            return

        self.circuit_breaker_failures.labels(source=source).inc()

    # ========== Metrics Server ==========

    async def start_server(self, port: int = 9090) -> None:
        """
        Start HTTP server to expose metrics.

        Args:
            port: Port to listen on (default 9090)
        """
        if not self._prometheus_enabled:
            console.print("[yellow]Prometheus not available, metrics server disabled[/yellow]")
            return

        async def metrics_handler(request: Any) -> Any:
            """Handle /metrics endpoint."""
            output = generate_latest(self.registry)
            return web.Response(body=output, content_type=CONTENT_TYPE_LATEST)

        async def health_handler(request: Any) -> Any:
            """Handle /health endpoint."""
            return web.Response(text="OK")

        app = web.Application()
        app.router.add_get("/metrics", metrics_handler)
        app.router.add_get("/health", health_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

        console.print(f"[green]Metrics server started on port {port}[/green]")

    def get_simple_metrics(self) -> dict[str, Any]:
        """Get simple metrics as dictionary (for non-Prometheus use)."""
        return self.simple.to_dict()


# Global metrics instance
metrics = MetricsCollector()
