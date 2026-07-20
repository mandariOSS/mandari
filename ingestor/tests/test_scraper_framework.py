"""
Tests für die Framework-Bausteine der Scraper-Integration:
external_id-Normalisierung, Content-Hash-Diffing, Crawl-Fenster,
Quellen-Konfiguration, Politeness (robots.txt, Rate-Limit).
"""

import time
import urllib.robotparser
from datetime import date

import httpx
import pytest

from src.scrapers import get_adapter
from src.scrapers.base import (
    CrawlWindow,
    ScraperConfig,
    content_hash,
    normalize_external_id,
    with_content_hash,
)
from src.scrapers.politeness import PoliteFetcher, RobotsDisallowedError, _RobotsEntry


class TestNormalizeExternalId:
    def test_canonical_form(self):
        url = "HTTP://Rat.Eschweiler.DE/bi/vo0050.php?__kvonr=12546&sid=abc123"
        assert (
            normalize_external_id(url, keep_params=("__kvonr",))
            == "https://rat.eschweiler.de/bi/vo0050.php?__kvonr=12546"
        )

    def test_params_sorted_and_filtered(self):
        url = "https://host.de/getfile.asp?type=do&id=42&session=xyz"
        assert (
            normalize_external_id(url, keep_params=("id", "type"))
            == "https://host.de/getfile.asp?id=42&type=do"
        )

    def test_fragment_dropped(self):
        url = "https://host.de/si0057.asp?__ksinr=7#tab"
        assert (
            normalize_external_id(url, keep_params=("__ksinr",))
            == "https://host.de/si0057.asp?__ksinr=7"
        )


class TestContentHash:
    def test_stable_regardless_of_key_order(self):
        a = {"name": "Rat", "type": "X", "nested": {"b": 1, "a": 2}}
        b = {"nested": {"a": 2, "b": 1}, "type": "X", "name": "Rat"}
        assert content_hash(a) == content_hash(b)

    def test_volatile_fields_excluded(self):
        base = {"name": "Rat", "modified": "2026-01-01", "created": "2020-01-01"}
        changed = {"name": "Rat", "modified": "2026-07-20", "created": "2021-05-05"}
        assert content_hash(base) == content_hash(changed)

    def test_substantive_change_detected(self):
        assert content_hash({"name": "Rat"}) != content_hash({"name": "Rat neu"})

    def test_with_content_hash_idempotent(self):
        entity = {"name": "Rat"}
        first = with_content_hash(dict(entity))["mandari:contentHash"]
        # Hash-Feld selbst ist volatil und ändert den Hash nicht
        second = with_content_hash(with_content_hash(dict(entity)))["mandari:contentHash"]
        assert first == second
        assert first.startswith("sha256:")

    def test_nested_volatile_excluded(self):
        a = {"file": {"name": "Anlage", "modified": "2026-01-01"}}
        b = {"file": {"name": "Anlage", "modified": "2026-02-02"}}
        assert content_hash(a) == content_hash(b)


class TestCrawlWindow:
    def test_months_within_year(self):
        window = CrawlWindow(start=date(2026, 3, 15), end=date(2026, 5, 2))
        assert window.months() == [(2026, 3), (2026, 4), (2026, 5)]

    def test_months_across_year_boundary(self):
        window = CrawlWindow(start=date(2025, 11, 20), end=date(2026, 2, 10))
        assert window.months() == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]

    def test_from_days(self):
        window = CrawlWindow.from_days(-60, 210, today=date(2026, 7, 20))
        assert window.start == date(2026, 5, 21)
        assert window.end == date(2027, 2, 15)


class TestScraperConfig:
    def test_missing_base_url_raises(self):
        with pytest.raises(ValueError):
            ScraperConfig.from_sync_config({"scraper": {}})

    def test_defaults(self):
        config = ScraperConfig.from_sync_config(
            {"scraper": {"base_url": "https://rat.example.de/bi"}}
        )
        assert config.base_url == "https://rat.example.de/bi/"
        assert config.rate_limit_seconds == 2.0
        assert config.max_concurrent == 1
        assert config.calendar_window_days == (-60, 210)
        assert config.members_on_full_only is True
        assert config.max_detail_pages is None

    def test_overrides(self):
        config = ScraperConfig.from_sync_config(
            {
                "scraper": {
                    "base_url": "https://rat.example.de/bi/",
                    "variant": "php",
                    "rate_limit_seconds": 5,
                    "max_detail_pages": 20,
                    "calendar_window_days": [-30, 90],
                }
            }
        )
        assert config.variant == "php"
        assert config.rate_limit_seconds == 5.0
        assert config.max_detail_pages == 20
        assert config.calendar_window_days == (-30, 90)


class TestAdapterRegistry:
    def test_sessionnet_resolves(self):
        config = ScraperConfig(base_url="https://rat.example.de/bi/")
        adapter = get_adapter("scraper:sessionnet", config, fetcher=None)
        assert adapter.vendor == "sessionnet"

    def test_unknown_vendor_raises(self):
        config = ScraperConfig(base_url="https://rat.example.de/bi/")
        with pytest.raises(ValueError):
            get_adapter("scraper:doesnotexist", config, fetcher=None)


def _make_robots_entry(rules: str) -> _RobotsEntry:
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(rules.splitlines())
    return _RobotsEntry(parser=parser, fetched_at=time.monotonic())


class TestRobots:
    async def test_disallow_respected(self):
        fetcher = PoliteFetcher()
        fetcher._robots_cache["rat.example.de"] = _make_robots_entry(
            "User-agent: *\nDisallow: /bi/"
        )
        assert await fetcher.is_allowed("https://rat.example.de/bi/si0040.asp") is False
        assert await fetcher.is_allowed("https://rat.example.de/andere.html") is True

    async def test_no_robots_means_allowed(self):
        fetcher = PoliteFetcher()
        fetcher._robots_cache["rat.example.de"] = _RobotsEntry(
            parser=None, fetched_at=time.monotonic()
        )
        assert await fetcher.is_allowed("https://rat.example.de/bi/si0040.asp") is True

    async def test_specific_agent_disallow(self):
        fetcher = PoliteFetcher()
        fetcher._robots_cache["rat.example.de"] = _make_robots_entry(
            "User-agent: mandari-ingestor\nDisallow: /"
        )
        assert await fetcher.is_allowed("https://rat.example.de/bi/si0040.asp") is False


class TestPoliteFetcher:
    async def test_rate_limit_and_fetch(self):
        calls: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            calls.append(time.monotonic())
            return httpx.Response(200, text="<html>ok</html>")

        fetcher = PoliteFetcher(rate_limit_seconds=0.3)
        fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            first = await fetcher.fetch_text("https://rat.example.de/bi/a.asp")
            second = await fetcher.fetch_text("https://rat.example.de/bi/b.asp")
            assert first == "<html>ok</html>"
            assert second == "<html>ok</html>"
            assert len(calls) == 2
            assert calls[1] - calls[0] >= 0.25  # Mindestabstand je Host
        finally:
            await fetcher._client.aclose()

    async def test_robots_disallow_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /")
            return httpx.Response(200, text="nie erreicht")

        fetcher = PoliteFetcher(rate_limit_seconds=0)
        fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(RobotsDisallowedError):
                await fetcher.fetch_text("https://rat.example.de/bi/a.asp")
        finally:
            await fetcher._client.aclose()

    async def test_transparent_user_agent(self):
        seen_agents: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_agents.append(request.headers.get("User-Agent", ""))
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, text="ok")

        fetcher = PoliteFetcher(rate_limit_seconds=0)
        fetcher._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"User-Agent": fetcher.user_agent},
        )
        try:
            await fetcher.fetch_text("https://rat.example.de/bi/a.asp")
        finally:
            await fetcher._client.aclose()
        assert seen_agents
        assert all("mandari" in agent for agent in seen_agents)
        assert any("mandari.de/crawler" in agent for agent in seen_agents)

    async def test_404_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(404)

        fetcher = PoliteFetcher(rate_limit_seconds=0)
        fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            assert await fetcher.fetch_text("https://rat.example.de/bi/weg.asp") is None
        finally:
            await fetcher._client.aclose()
