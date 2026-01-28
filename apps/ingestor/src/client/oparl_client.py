"""
OParl HTTP Client - High-Performance Async Client

Features:
- Async HTTP with connection pooling
- Concurrent request handling with semaphore
- ETag and If-Modified-Since caching
- Exponential backoff retry
- Rate limiting
- Circuit breaker for resilience
- Prometheus metrics
"""

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
from rich.console import Console

from src.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitOpenError
from src.config import settings
from src.metrics import metrics

console = Console()


@dataclass
class FetchResult:
    """Result of a fetch operation."""

    url: str
    data: dict[str, Any] | None
    status_code: int
    from_cache: bool = False
    error: str | None = None
    fetch_time: float = 0.0


@dataclass
class SyncStats:
    """Statistics for sync operation."""

    http_requests: int = 0
    cache_hits: int = 0
    objects_processed: int = 0
    pages_fetched: int = 0
    errors: int = 0
    http_time: float = 0.0
    start_time: float = field(default_factory=time.time)

    def __str__(self) -> str:
        elapsed = time.time() - self.start_time
        return (
            f"HTTP Requests: {self.http_requests} | "
            f"Cache Hits: {self.cache_hits} | "
            f"Objects: {self.objects_processed} | "
            f"Pages: {self.pages_fetched} | "
            f"Errors: {self.errors} | "
            f"HTTP Time: {self.http_time:.1f}s | "
            f"Total: {elapsed:.1f}s | "
            f"Speed: {self.objects_processed / max(elapsed, 0.1):.1f} obj/s"
        )


class OParlClient:
    """
    High-performance async HTTP client for OParl APIs.

    Features:
    - Connection pooling via httpx.AsyncClient
    - Concurrent requests with semaphore control
    - ETag caching for bandwidth efficiency
    - If-Modified-Since header support
    - Exponential backoff retry logic
    - Rate limiting between requests
    - Circuit breaker for resilience
    - Prometheus metrics collection
    """

    def __init__(
        self,
        max_concurrent: int = 10,
        timeout: int | None = None,
        wait_time: float | None = None,
        source_name: str | None = None,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.timeout = timeout or settings.oparl_request_timeout
        self.wait_time = wait_time or settings.oparl_wait_time
        self.max_retries = settings.oparl_max_retries
        self.retry_backoff = settings.oparl_retry_backoff
        self.source_name = source_name or "unknown"

        # Caching
        self.etag_cache: dict[str, str] = {}
        self.modified_cache: dict[str, str] = {}

        # Concurrency control
        self._semaphore: asyncio.Semaphore | None = None
        self._client: httpx.AsyncClient | None = None

        # Circuit breaker per source host
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._circuit_breaker_config = CircuitBreakerConfig(
            failure_threshold=settings.circuit_breaker_failure_threshold,
            recovery_timeout=settings.circuit_breaker_recovery_timeout,
            success_threshold=settings.circuit_breaker_success_threshold,
        )

        # Statistics
        self.stats = SyncStats()

    async def __aenter__(self) -> "OParlClient":
        """Async context manager entry."""
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(
                max_connections=self.max_concurrent * 2,
                max_keepalive_connections=self.max_concurrent,
            ),
            headers={
                "Accept": "application/json",
                "User-Agent": "Mandari-Ingestor/2.0 (https://github.com/mandari)",
            },
            follow_redirects=True,  # Follow HTTP 301/302 redirects
        )
        self.stats = SyncStats()
        self._circuit_breakers = {}
        return self

    def _get_circuit_breaker(self, url: str) -> CircuitBreaker:
        """Get or create circuit breaker for URL's host."""
        if not settings.circuit_breaker_enabled:
            # Return a no-op breaker that never opens
            return CircuitBreaker(
                name="disabled",
                config=CircuitBreakerConfig(failure_threshold=999999),
            )

        host = urlparse(url).netloc
        if host not in self._circuit_breakers:
            self._circuit_breakers[host] = CircuitBreaker(
                name=host,
                config=self._circuit_breaker_config,
            )
        return self._circuit_breakers[host]

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def fetch(
        self,
        url: str,
        use_cache: bool = True,
        skip_wait: bool = False,
    ) -> FetchResult:
        """
        Fetch a single URL with caching and retry.

        Args:
            url: The URL to fetch
            use_cache: Whether to use ETag/If-Modified-Since
            skip_wait: Skip rate limiting wait

        Returns:
            FetchResult with data or error
        """
        if not self._client or not self._semaphore:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        async with self._semaphore:
            return await self._fetch_with_retry(url, use_cache, skip_wait)

    async def _fetch_with_retry(
        self,
        url: str,
        use_cache: bool,
        skip_wait: bool,
    ) -> FetchResult:
        """Fetch with exponential backoff retry and circuit breaker."""
        last_error: str | None = None
        circuit_breaker = self._get_circuit_breaker(url)

        # Check if circuit is open
        try:
            # Try to execute through circuit breaker
            for attempt in range(self.max_retries):
                try:
                    result = await circuit_breaker.call(
                        self._do_fetch, url, use_cache, skip_wait
                    )
                    return result

                except CircuitOpenError as e:
                    # Circuit is open - fail fast
                    self.stats.errors += 1
                    metrics.record_http_error(self.source_name, "circuit_open")
                    return FetchResult(
                        url=url,
                        data=None,
                        status_code=0,
                        error=f"Circuit breaker open: {e}",
                    )

                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        return FetchResult(url=url, data=None, status_code=404, error="Not found")
                    if e.response.status_code >= 500:
                        last_error = f"HTTP {e.response.status_code}"
                        metrics.record_http_error(self.source_name, f"http_{e.response.status_code}")
                    else:
                        return FetchResult(
                            url=url,
                            data=None,
                            status_code=e.response.status_code,
                            error=f"HTTP {e.response.status_code}",
                        )

                except (httpx.RequestError, httpx.TimeoutException) as e:
                    last_error = str(e)
                    error_type = "timeout" if isinstance(e, httpx.TimeoutException) else "request_error"
                    metrics.record_http_error(self.source_name, error_type)

                # Exponential backoff
                if attempt < self.max_retries - 1:
                    wait = self.retry_backoff ** attempt
                    await asyncio.sleep(wait)

        except Exception as e:
            last_error = str(e)
            metrics.record_http_error(self.source_name, "unknown")

        self.stats.errors += 1
        return FetchResult(
            url=url,
            data=None,
            status_code=0,
            error=f"Max retries exceeded: {last_error}",
        )

    async def _do_fetch(
        self,
        url: str,
        use_cache: bool,
        skip_wait: bool,
    ) -> FetchResult:
        """Perform the actual HTTP fetch with metrics."""
        assert self._client is not None

        headers: dict[str, str] = {}

        # Add caching headers
        if use_cache:
            if settings.oparl_etag_cache_enabled and url in self.etag_cache:
                headers["If-None-Match"] = self.etag_cache[url]
            if settings.oparl_modified_since_enabled and url in self.modified_cache:
                headers["If-Modified-Since"] = self.modified_cache[url]

        # Rate limiting
        if not skip_wait and self.wait_time > 0:
            await asyncio.sleep(self.wait_time)

        start = time.time()
        response = await self._client.get(url, headers=headers)
        fetch_time = time.time() - start

        self.stats.http_requests += 1
        self.stats.http_time += fetch_time

        # Record metrics
        from_cache = response.status_code == 304
        metrics.record_http_request(
            source=self.source_name,
            status=response.status_code,
            duration=fetch_time,
            from_cache=from_cache,
        )

        # Not modified - cache hit
        if response.status_code == 304:
            self.stats.cache_hits += 1
            return FetchResult(
                url=url,
                data=None,
                status_code=304,
                from_cache=True,
                fetch_time=fetch_time,
            )

        response.raise_for_status()

        # Update cache
        if "ETag" in response.headers:
            self.etag_cache[url] = response.headers["ETag"]
        if "Last-Modified" in response.headers:
            self.modified_cache[url] = response.headers["Last-Modified"]

        return FetchResult(
            url=url,
            data=response.json(),
            status_code=response.status_code,
            fetch_time=fetch_time,
        )

    async def fetch_list(
        self,
        url: str,
        modified_since: datetime | None = None,
        max_pages: int | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Fetch a paginated OParl list.

        Yields pages of items as they are fetched.

        Args:
            url: The list URL
            modified_since: Only fetch items modified after this date (used for filtering, not query param)
            max_pages: Maximum number of pages to fetch (for incremental sync)

        Yields:
            Lists of items from each page
        """
        current_url: str | None = url
        pages_fetched = 0

        # NOTE: We do NOT use modified_since as query parameter because:
        # 1. Not all OParl servers support it
        # 2. New items appear on page 1 and push old items to later pages
        # Instead, we fetch the first N pages and check each item's modified date

        while current_url:
            result = await self.fetch(current_url, use_cache=False)

            if result.error:
                console.print(f"[red]Error fetching {current_url}: {result.error}[/red]")
                break

            if result.data is None:
                break

            self.stats.pages_fetched += 1
            pages_fetched += 1

            # Extract data - OParl uses "data" for list items
            items = result.data.get("data", [])
            if items:
                self.stats.objects_processed += len(items)
                yield items

            # Check if we've reached max pages (for incremental sync)
            if max_pages and pages_fetched >= max_pages:
                break

            # Get next page URL
            links = result.data.get("links", {})
            current_url = links.get("next")

    async def fetch_list_all(
        self,
        url: str,
        modified_since: datetime | None = None,
        max_pages: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch all items from a paginated list.

        Args:
            url: The list URL
            modified_since: Only fetch items modified after this date (for filtering)
            max_pages: Maximum number of pages to fetch

        Returns:
            All items from all pages
        """
        all_items: list[dict[str, Any]] = []
        async for page in self.fetch_list(url, modified_since, max_pages):
            all_items.extend(page)
        return all_items

    async def fetch_many(
        self,
        urls: list[str],
        use_cache: bool = True,
    ) -> list[FetchResult]:
        """
        Fetch multiple URLs concurrently.

        Args:
            urls: List of URLs to fetch
            use_cache: Whether to use caching

        Returns:
            List of FetchResults in same order as input
        """
        tasks = [self.fetch(url, use_cache=use_cache) for url in urls]
        return await asyncio.gather(*tasks)

    async def fetch_system(self, url: str) -> dict[str, Any] | None:
        """
        Fetch the OParl system object (entry point).

        Args:
            url: The system URL

        Returns:
            System data or None
        """
        result = await self.fetch(url, use_cache=False, skip_wait=True)
        return result.data

    def get_url_hash(self, url: str) -> str:
        """Generate a short hash for a URL (for logging)."""
        # Use SHA256 instead of MD5 (MD5 is considered weak)
        return hashlib.sha256(url.encode()).hexdigest()[:8]
