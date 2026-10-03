"""
Höflicher HTML-Fetcher für Scraper-Adapter.

- Rate-Limit je Host (konfigurierbar je Quelle, Default 1 Request / 2 s)
- max_concurrent=1 je Quelle (Serialisierung über Lock)
- robots.txt-Respekt nach RFC 9309 mit Platzhaltern (src/client/robots.py, 24-h-Cache je Host)
- Transparenter User-Agent (settings.user_agent, je Quelle überschreibbar)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from mandari_oparl.robots import KIND_API, RobotsOverride

from src.client.robots import robots_gate
from src.config import settings
from src.metrics import metrics
from src.redaction import MaskingConsole

console = MaskingConsole()


class RobotsDisallowedError(Exception):
    """robots.txt verbietet den Abruf des Pfads für unseren User-Agent."""


class PoliteFetcher:
    """
    Async-HTML-Fetcher mit Politeness-Garantien.

    Bewusst getrennt vom OParlClient (JSON-orientiert): liefert Roh-HTML,
    erzwingt strengere Defaults und prüft robots.txt vor jedem Request.
    """

    def __init__(
        self,
        rate_limit_seconds: float = 2.0,
        timeout: float = 30.0,
        max_retries: int = 3,
        user_agent: str | None = None,
        source_name: str = "scraper",
        respect_robots: bool = True,
        robots_override: RobotsOverride | None = None,
    ) -> None:
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent or settings.user_agent
        self.source_name = source_name
        self.respect_robots = respect_robots
        self.robots_override = robots_override

        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()  # max_concurrent=1: serialisiert alle Requests
        self._last_request_at: dict[str, float] = {}
        self.pages_fetched = 0

    async def __aenter__(self) -> PoliteFetcher:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # robots.txt
    # ------------------------------------------------------------------

    async def is_allowed(self, url: str, kind: str = KIND_API) -> bool:
        """Prüft, ob robots.txt den Abruf der URL für unseren User-Agent erlaubt (RFC 9309)."""
        if not self.respect_robots:
            return True
        decision = await robots_gate.decide(
            self._client, url, user_agent=self.user_agent, kind=kind, override=self.robots_override
        )
        return decision.allowed

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def fetch_text(self, url: str) -> str | None:
        """
        Holt eine Seite als Text (None bei nicht behebbarem Fehler).

        Wirft RobotsDisallowedError, wenn robots.txt den Pfad verbietet.
        """
        if not self._client:
            raise RuntimeError("PoliteFetcher nicht initialisiert — 'async with' verwenden.")

        if not await self.is_allowed(url):
            raise RobotsDisallowedError(url)

        host = urlparse(url).netloc.lower()
        last_error: str | None = None

        for attempt in range(self.max_retries):
            async with self._lock:
                # Rate-Limit je Host: Mindestabstand zwischen Requests
                wait = 0.0
                last = self._last_request_at.get(host)
                if last is not None:
                    wait = max(0.0, self.rate_limit_seconds - (time.monotonic() - last))
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request_at[host] = time.monotonic()

                try:
                    response = await self._client.get(url)
                except httpx.HTTPError as e:
                    last_error = str(e)
                    metrics.record_http_error(self.source_name, "request_error")
                    continue

            self.pages_fetched += 1
            metrics.record_scraper_page(self.source_name)

            if response.status_code == 200:
                return response.text
            if response.status_code in (404, 410):
                return None
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                metrics.record_http_error(self.source_name, f"http_{response.status_code}")
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            # 4xx (außer 404/410): nicht wiederholen
            console.print(f"[yellow]Scraper: HTTP {response.status_code} für {url}[/yellow]")
            return None

        console.print(f"[red]Scraper: Abruf fehlgeschlagen ({last_error}): {url}[/red]")
        return None
