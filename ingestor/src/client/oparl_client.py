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
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from rich.console import Console

from src.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitOpenError
from src.client.oparl_compat import is_oparl_error, modified_since_dropped, oparl_error_message
from src.config import settings
from src.metrics import metrics

console = Console()

# Fehlerklassen für Sync-Log und Quellenstatus (Issue #123). Die Django-Seite
# kennt dieselben Werte (insight_core.models.OParlSource.ERROR_KIND_*).
ERROR_KIND_UA_BLOCKED = "ua_blocked"
ERROR_KIND_SERVER_ERROR_SERIES = "server_error_series"

# Neutraler Client-Header für die einmalige Vergleichsanfrage nach einem 403.
# Absichtlich der nackte Bibliotheks-Default: Antwortet der Server darauf mit
# 200, filtert er gezielt auf unseren User-Agent — das ist der Befund, mehr nicht.
NEUTRAL_USER_AGENT = f"python-httpx/{httpx.__version__}"


@dataclass
class FetchResult:
    """Result of a fetch operation."""

    url: str
    data: dict[str, Any] | None
    status_code: int
    from_cache: bool = False
    error: str | None = None
    fetch_time: float = 0.0
    # Fehlerklasse (ERROR_KIND_*), wenn der Fehler einer Sperre oder Störung zuzuordnen ist
    error_kind: str | None = None


class ListFetchError(Exception):
    """
    Eine Objektliste (oder eine ihrer Seiten) war nicht abrufbar.

    Früher brach fetch_list still ab und der Lauf galt als erfolgreich — die
    Lücke fiel niemandem auf. Jetzt trägt der Fehler die Liste, die Seite und
    die Fehlerklasse, damit der Orchestrator sie im Sync-Log nennt.
    """

    def __init__(self, list_url: str, page_url: str, result: FetchResult) -> None:
        self.list_url = list_url
        self.page_url = page_url
        self.result = result
        self.error_kind = result.error_kind
        page = "" if page_url == list_url else f" (Seite {page_url})"
        super().__init__(f"Liste {list_url}{page} nicht abrufbar: {result.error}")


@dataclass
class HostHealth:
    """
    Befund je Host: User-Agent-Sperre und 5xx-Serie (Issue #123).

    Wird vom Client während eines Laufs gepflegt; der Orchestrator liest ihn am
    Ende aus und schreibt Fehlerklasse und Statistik in Sync-Log und Quellenstatus.
    """

    host: str
    forbidden_count: int = 0
    ua_probe_status: int | None = None  # Status der Vergleichsanfrage (0 = Verbindungsfehler)
    ua_blocked_at: datetime | None = None
    consecutive_server_errors: int = 0
    server_error_total: int = 0
    server_error_series_started_at: datetime | None = None
    last_server_error_at: datetime | None = None
    last_status_codes: deque[int] = field(default_factory=lambda: deque(maxlen=10))
    failed_lists: list[str] = field(default_factory=list)

    @property
    def server_error_series(self) -> bool:
        return self.consecutive_server_errors >= max(1, settings.oparl_server_error_series_threshold)

    @property
    def error_kind(self) -> str | None:
        if self.ua_blocked_at is not None:
            return ERROR_KIND_UA_BLOCKED
        if self.server_error_series:
            return ERROR_KIND_SERVER_ERROR_SERIES
        return None

    def note_failed_list(self, list_url: str) -> None:
        # Gedeckelt: Der Text landet in last_error und im Sync-Log
        if list_url not in self.failed_lists and len(self.failed_lists) < 20:
            self.failed_lists.append(list_url)

    def describe(self) -> str:
        """Lesbare Zusammenfassung für Sync-Log, Quellenstatus und Konsole."""
        kind = self.error_kind
        if kind == ERROR_KIND_UA_BLOCKED:
            assert self.ua_blocked_at is not None
            text = (
                f"User-Agent gesperrt: {self.host} antwortet auf unseren User-Agent mit HTTP 403, "
                f"ein neutraler Client erhält HTTP {self.ua_probe_status} "
                f"(erkannt {self.ua_blocked_at:%d.%m.%Y %H:%M} UTC)"
            )
        elif kind == ERROR_KIND_SERVER_ERROR_SERIES:
            codes = ", ".join(str(code) for code in self.last_status_codes)
            started = self.server_error_series_started_at
            last = self.last_server_error_at
            span = ""
            if started and last:
                minutes = max(0, int((last - started).total_seconds() // 60))
                span = f" zwischen {started:%H:%M} und {last:%H:%M} UTC ({minutes} min)"
            text = (
                f"5xx-Serie: {self.host} lieferte {self.consecutive_server_errors} Serverfehler in Folge"
                f"{span}, insgesamt {self.server_error_total} in diesem Lauf; letzte Statuscodes: {codes}"
            )
        else:
            return ""
        if self.failed_lists:
            text += "; betroffene Listen: " + ", ".join(self.failed_lists)
        return text

    def as_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "error_kind": self.error_kind,
            "forbidden_count": self.forbidden_count,
            "ua_probe_status": self.ua_probe_status,
            "ua_blocked_at": self.ua_blocked_at.isoformat() if self.ua_blocked_at else None,
            "consecutive_server_errors": self.consecutive_server_errors,
            "server_error_total": self.server_error_total,
            "server_error_series_started_at": (
                self.server_error_series_started_at.isoformat() if self.server_error_series_started_at else None
            ),
            "last_server_error_at": self.last_server_error_at.isoformat() if self.last_server_error_at else None,
            "last_status_codes": list(self.last_status_codes),
            "failed_lists": list(self.failed_lists),
        }


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

    # Prozessweiter Capability-Cache: Hosts, die modified_since ablehnen
    # (401/403/400) — überlebt Client-Instanzen innerhalb des Daemons.
    # NOTE: Class-level and therefore process-lifetime by design — keyed by
    # host, shared across all OParlClient instances, never expires until
    # the daemon restarts. Bounded by the number of distinct OParl hosts.
    # Der Orchestrator persistiert den Befund zusätzlich je Quelle in
    # OParlSource.sync_config, damit er Daemon-Neustarts überlebt (Issue #22).
    _modified_since_unsupported: set[str] = set()

    @classmethod
    def get_modified_since_unsupported(cls) -> set[str]:
        """Snapshot des Capability-Caches (Hosts ohne modified_since-Support)."""
        return set(cls._modified_since_unsupported)

    @classmethod
    def add_modified_since_unsupported(cls, hosts) -> None:
        """Capability-Cache seeden, z. B. aus persistierter sync_config."""
        cls._modified_since_unsupported.update(h for h in hosts if h)

    def __init__(
        self,
        max_concurrent: int = 10,
        timeout: int | None = None,
        wait_time: float | None = None,
        source_name: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.timeout = timeout or settings.oparl_request_timeout
        self.wait_time = wait_time or settings.oparl_wait_time
        self.max_retries = settings.oparl_max_retries
        self.retry_backoff = settings.oparl_retry_backoff
        self.source_name = source_name or "unknown"
        # Je Quelle überschreibbar (OParlSource.user_agent); leer = Standard
        self.user_agent = (user_agent or "").strip() or settings.user_agent

        # Sperr- und Störungsbefund je Host (Issue #123), lebt so lange wie der Client
        self.host_health: dict[str, HostHealth] = {}

        # Caching
        self.etag_cache: dict[str, str] = {}
        self.modified_cache: dict[str, str] = {}

        # Concurrency control
        self._semaphore: asyncio.Semaphore | None = None
        self._client: httpx.AsyncClient | None = None

        # Circuit breaker per source host
        # Only count HTTP errors as failures, not local errors (encoding, etc.)
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._circuit_breaker_config = CircuitBreakerConfig(
            failure_threshold=settings.circuit_breaker_failure_threshold,
            recovery_timeout=settings.circuit_breaker_recovery_timeout,
            success_threshold=settings.circuit_breaker_success_threshold,
            failure_exceptions=(
                httpx.HTTPStatusError,
                httpx.RequestError,
                httpx.TimeoutException,
                ConnectionError,
                TimeoutError,
            ),
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
                "User-Agent": self.user_agent,
            },
            follow_redirects=True,  # Follow HTTP 301/302 redirects
        )
        self.stats = SyncStats()
        self._circuit_breakers = {}
        self.host_health = {}
        return self

    # ------------------------------------------------------------------
    # Sperr- und Störungserkennung (Issue #123)
    # ------------------------------------------------------------------

    def _health(self, url: str) -> HostHealth:
        host = urlparse(url).netloc
        if host not in self.host_health:
            self.host_health[host] = HostHealth(host=host)
        return self.host_health[host]

    def host_findings(self) -> list[HostHealth]:
        """Hosts mit Sperr- oder Störungsbefund (User-Agent gesperrt zuerst)."""
        findings = [h for h in self.host_health.values() if h.error_kind]
        findings.sort(key=lambda h: (h.error_kind != ERROR_KIND_UA_BLOCKED, h.host))
        return findings

    @property
    def error_kind(self) -> str | None:
        """Schwerwiegendste Fehlerklasse dieses Laufs (None = kein Befund)."""
        findings = self.host_findings()
        return findings[0].error_kind if findings else None

    def _note_server_error(self, url: str, status_code: int) -> None:
        health = self._health(url)
        now = datetime.now(UTC)
        if health.consecutive_server_errors == 0:
            health.server_error_series_started_at = now
        health.consecutive_server_errors += 1
        health.server_error_total += 1
        health.last_server_error_at = now
        health.last_status_codes.append(status_code)
        if health.consecutive_server_errors == max(1, settings.oparl_server_error_series_threshold):
            metrics.record_http_error(self.source_name, ERROR_KIND_SERVER_ERROR_SERIES)
            console.print(
                f"[red]{health.host}: {health.consecutive_server_errors} Serverfehler in Folge — "
                f"Quelle gilt als gestört (5xx-Serie)[/red]"
            )

    def _note_success(self, url: str) -> None:
        # Eine erfolgreiche Antwort beendet die Serie, die Gesamtzahl bleibt für die Statistik
        health = self.host_health.get(urlparse(url).netloc)
        if health is not None:
            health.consecutive_server_errors = 0

    async def _diagnose_forbidden(self, url: str) -> tuple[str | None, str]:
        """
        Einmalige Vergleichsanfrage nach HTTP 403 (je Host und Client-Instanz).

        Nur Diagnose: Bekommt ein neutraler Client-Header eine 2xx-Antwort, filtert
        der Server gezielt auf unseren User-Agent (Fehlerklasse ua_blocked). Der
        Regelbetrieb läuft weiter mit unserem User-Agent — keine Umgehung.
        """
        assert self._client is not None
        health = self._health(url)
        health.forbidden_count += 1
        if health.ua_blocked_at is not None:
            return ERROR_KIND_UA_BLOCKED, "User-Agent gesperrt"
        if health.ua_probe_status is not None:
            return None, "Zugriff verweigert, auch für neutralen Client"
        if not settings.oparl_ua_probe_enabled:
            return None, "Zugriff verweigert"

        health.ua_probe_status = 0
        try:
            if self.wait_time > 0:
                await asyncio.sleep(self.wait_time)
            start = time.time()
            response = await self._client.get(
                url, headers={"User-Agent": NEUTRAL_USER_AGENT, "Accept": "application/json"}
            )
        except httpx.HTTPError as exc:
            return None, f"Zugriff verweigert; Vergleichsanfrage fehlgeschlagen: {exc}"
        self.stats.http_requests += 1
        metrics.record_http_request(self.source_name, response.status_code, time.time() - start)
        health.ua_probe_status = response.status_code

        if response.status_code < 400:
            health.ua_blocked_at = datetime.now(UTC)
            metrics.record_http_error(self.source_name, ERROR_KIND_UA_BLOCKED)
            console.print(
                f"[red]{health.host}: HTTP 403 für unseren User-Agent, neutraler Client erhält "
                f"HTTP {response.status_code} — User-Agent gesperrt[/red]"
            )
            return ERROR_KIND_UA_BLOCKED, f"User-Agent gesperrt: neutraler Client erhält HTTP {response.status_code}"
        return None, f"Zugriff verweigert, auch für neutralen Client (HTTP {response.status_code})"

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
                    return await circuit_breaker.call(self._do_fetch, url, use_cache, skip_wait)

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
                    status_code = e.response.status_code
                    if status_code == 404:
                        return FetchResult(url=url, data=None, status_code=404, error="Not found")
                    if status_code >= 500:
                        last_error = f"HTTP {status_code}"
                        metrics.record_http_error(self.source_name, f"http_{status_code}")
                        self._note_server_error(url, status_code)
                    elif status_code == 403:
                        kind, detail = await self._diagnose_forbidden(url)
                        self.stats.errors += 1
                        return FetchResult(
                            url=url, data=None, status_code=403, error=f"HTTP 403 ({detail})", error_kind=kind
                        )
                    else:
                        return FetchResult(
                            url=url,
                            data=None,
                            status_code=status_code,
                            error=f"HTTP {status_code}",
                        )

                except (httpx.RequestError, httpx.TimeoutException) as e:
                    last_error = str(e)
                    error_type = "timeout" if isinstance(e, httpx.TimeoutException) else "request_error"
                    metrics.record_http_error(self.source_name, error_type)

                # Exponential backoff
                if attempt < self.max_retries - 1:
                    wait = self.retry_backoff**attempt
                    await asyncio.sleep(wait)

        except Exception as e:
            last_error = str(e)
            metrics.record_http_error(self.source_name, "unknown")

        self.stats.errors += 1
        health = self.host_health.get(urlparse(url).netloc)
        series = health is not None and health.server_error_series
        return FetchResult(
            url=url,
            data=None,
            status_code=0,
            error=f"Max retries exceeded: {last_error}",
            error_kind=ERROR_KIND_SERVER_ERROR_SERIES if series else None,
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

        if response.status_code < 500:
            self._note_success(url)

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
            modified_since: Append as ?modified_since= query parameter (OParl 2.5.5).
                           Most servers (5/6 tested) support this correctly.
                           The orchestrator handles capability detection and fallback.
            max_pages: Maximum number of pages to fetch

        Yields:
            Lists of items from each page
        """
        current_url: str | None = url
        pages_fetched = 0
        tried_modified_since = False

        # Append modified_since as OParl query parameter if provided.
        # Capability-Cache: Hosts, die den Filter bereits abgelehnt haben
        # (z. B. Stadt Münster mit 401), gar nicht erst erneut damit anfragen —
        # spart pro Liste einen toten Request samt Timeout/Retry.
        host = urlparse(url).netloc
        if modified_since and current_url and host not in self._modified_since_unsupported:
            current_url = self._append_modified_since(current_url, modified_since)
            tried_modified_since = True

        while current_url:
            result = await self.fetch(current_url, use_cache=False)

            # Fallback: manche RIS (z. B. Stadt Münster) beantworten
            # modified_since mit 401/403/400. Dann einmalig ohne den Filter
            # neu ansetzen — die Client-seitige modified-Prüfung plus
            # Early-Stop hält den Mehraufwand klein.
            if (
                tried_modified_since
                and pages_fetched == 0
                and result.status_code in (400, 401, 403)
                and result.error_kind != ERROR_KIND_UA_BLOCKED  # Sperre, kein Capability-Problem
            ):
                console.print(
                    f"[yellow]{url}: modified_since nicht unterstützt "
                    f"(HTTP {result.status_code}) — Fallback auf vollständige Liste[/yellow]"
                )
                metrics.record_http_error(self.source_name, "modified_since_unsupported")
                self._modified_since_unsupported.add(host)
                tried_modified_since = False
                current_url = url
                continue

            if result.error:
                # Keine stille Lücke (Issue #123): der Aufrufer erfährt, welche
                # Liste ab welcher Seite fehlt, und nennt sie im Sync-Log.
                console.print(f"[red]Error fetching {current_url}: {result.error}[/red]")
                self._health(current_url).note_failed_list(url)
                raise ListFetchError(url, current_url, result)

            if result.data is None:
                break

            # OParl 1.0 (more! rubin) liefert Fehlerobjekte mit HTTP 200,
            # z. B. "Requested class doesn't exist." für unbekannte Pfade.
            # Ohne diese Prüfung würde das Fehlerobjekt als Einzelobjekt
            # durch _extract_items laufen (Issue #122).
            if is_oparl_error(result.data):
                console.print(f"[red]OParl-Fehlerobjekt von {current_url}: {oparl_error_message(result.data)}[/red]")
                break

            # Server, die modified_since stillschweigend ignorieren (OParl 1.0,
            # more! rubin): der Parameter fehlt in self/next. Die Seite ist dann
            # bereits die ungefilterte Liste — weiterlaufen, aber den Host
            # merken, damit Folgelisten den toten Parameter nicht mehr senden.
            if (
                tried_modified_since
                and pages_fetched == 0
                and isinstance(result.data, dict)
                and modified_since_dropped(result.data.get("links"))
            ):
                console.print(
                    f"[yellow]{url}: Server ignoriert modified_since (Filter fehlt in den Listen-Links) "
                    f"— Client-seitiger Abgleich[/yellow]"
                )
                self._modified_since_unsupported.add(host)
                tried_modified_since = False

            self.stats.pages_fetched += 1
            pages_fetched += 1

            # Extract items - handle multiple OParl response formats
            items = self._extract_items(result.data)

            if items:
                self.stats.objects_processed += len(items)
                yield items

            # Check if we've reached max pages
            if max_pages and pages_fetched >= max_pages:
                break

            # Get next page URL (server preserves filter params in links.next)
            if isinstance(result.data, dict):
                links = result.data.get("links", {})
                current_url = links.get("next")
            else:
                current_url = None

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

    @staticmethod
    def _append_modified_since(url: str, modified_since: datetime) -> str:
        """Append modified_since as OParl query parameter to URL."""
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        # OParl-Spec 2.5.5 verlangt das Format yyyy-mm-ddThh:mm:ss±hh:mm —
        # der Zeitzonen-Offset ist Pflicht. Naive datetimes als UTC auszeichnen,
        # sonst ist der Wert spec-widrig und Server dürfen ihn ablehnen.
        if modified_since.tzinfo is None:
            modified_since = modified_since.replace(tzinfo=UTC)
        params["modified_since"] = [modified_since.isoformat(timespec="seconds")]
        new_query = urlencode(params, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    @staticmethod
    def _extract_items(data: dict[str, Any] | list | Any) -> list[dict[str, Any]]:
        """
        Extract items from various OParl response formats.

        Handles:
        - Standard: {"data": [...], "links": {...}} (Köln, Münster, Bonn, Aachen)
        - Single object: {"type": "...Body", ...} (ITK Rheinland direct body)
        - Array without wrapper: [{...}, {...}] (some endpoints)
        """
        # Standard OParl list with data[] wrapper
        if isinstance(data, dict):
            items = data.get("data", [])
            if items:
                return items

            # Single object without data[] wrapper (ITK Rheinland pattern);
            # OParl-Fehlerobjekte (HTTP 200, type .../Error) sind keine Items.
            if data.get("type") and not is_oparl_error(data):
                return [data]

        # Array without data wrapper
        if isinstance(data, list):
            return data

        return []

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
