"""
Tests for the OParl HTTP Client.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime

from src.client.oparl_client import OParlClient, FetchResult, SyncStats


class TestOParlClientInit:
    """Tests for OParlClient initialization."""

    def test_default_settings(self) -> None:
        """Test client initializes with default settings."""
        client = OParlClient()

        assert client.max_concurrent == 10
        assert client.max_retries > 0
        assert client.retry_backoff > 0

    def test_custom_concurrent(self) -> None:
        """Test client accepts custom concurrent limit."""
        client = OParlClient(max_concurrent=5)

        assert client.max_concurrent == 5

    def test_custom_timeout(self) -> None:
        """Test client accepts custom timeout."""
        client = OParlClient(timeout=60)

        assert client.timeout == 60

    def test_custom_wait_time(self) -> None:
        """Test client accepts custom wait time."""
        client = OParlClient(wait_time=0.5)

        assert client.wait_time == 0.5


class TestFetchResult:
    """Tests for FetchResult dataclass."""

    def test_fetch_result_creation(self) -> None:
        """Test creating a FetchResult."""
        result = FetchResult(
            url="https://example.org/test",
            data={"key": "value"},
            status_code=200,
        )

        assert result.url == "https://example.org/test"
        assert result.data == {"key": "value"}
        assert result.status_code == 200
        assert result.from_cache is False
        assert result.error is None

    def test_fetch_result_with_error(self) -> None:
        """Test creating a FetchResult with error."""
        result = FetchResult(
            url="https://example.org/test",
            data=None,
            status_code=500,
            error="Server error",
        )

        assert result.data is None
        assert result.status_code == 500
        assert result.error == "Server error"

    def test_fetch_result_cache_hit(self) -> None:
        """Test creating a FetchResult from cache."""
        result = FetchResult(
            url="https://example.org/test",
            data=None,
            status_code=304,
            from_cache=True,
        )

        assert result.from_cache is True
        assert result.status_code == 304


class TestSyncStats:
    """Tests for SyncStats dataclass."""

    def test_sync_stats_defaults(self) -> None:
        """Test SyncStats has correct defaults."""
        stats = SyncStats()

        assert stats.http_requests == 0
        assert stats.cache_hits == 0
        assert stats.objects_processed == 0
        assert stats.pages_fetched == 0
        assert stats.errors == 0
        assert stats.http_time == 0.0

    def test_sync_stats_str(self) -> None:
        """Test SyncStats string representation."""
        stats = SyncStats()
        stats.http_requests = 10
        stats.objects_processed = 100

        str_repr = str(stats)

        assert "10" in str_repr
        assert "100" in str_repr


class TestURLHashing:
    """Tests for URL hashing utility."""

    def test_get_url_hash(self) -> None:
        """Test URL hash generation."""
        client = OParlClient()
        hash1 = client.get_url_hash("https://example.org/test")
        hash2 = client.get_url_hash("https://example.org/test")

        assert hash1 == hash2
        assert len(hash1) == 8

    def test_different_urls_different_hashes(self) -> None:
        """Test different URLs get different hashes."""
        client = OParlClient()
        hash1 = client.get_url_hash("https://example.org/test1")
        hash2 = client.get_url_hash("https://example.org/test2")

        assert hash1 != hash2


class TestClientContextManager:
    """Tests for async context manager."""

    @pytest.mark.asyncio
    async def test_context_manager_initializes(self) -> None:
        """Test context manager initializes client."""
        async with OParlClient() as client:
            assert client._client is not None
            assert client._semaphore is not None

    @pytest.mark.asyncio
    async def test_context_manager_cleans_up(self) -> None:
        """Test context manager cleans up on exit."""
        client = OParlClient()

        async with client:
            pass

        assert client._client is None


class TestCacheHeaders:
    """Tests for ETag and If-Modified-Since caching."""

    def test_etag_cache_starts_empty(self) -> None:
        """Test ETag cache starts empty."""
        client = OParlClient()
        assert len(client.etag_cache) == 0

    def test_modified_cache_starts_empty(self) -> None:
        """Test modified cache starts empty."""
        client = OParlClient()
        assert len(client.modified_cache) == 0


# Integration tests would require mocking httpx or a test server
# These are left as examples of what could be tested

class TestFetchIntegration:
    """Integration tests for fetch operations (mocked)."""

    @pytest.mark.asyncio
    async def test_fetch_without_context_raises(self) -> None:
        """Test fetch raises if called without context manager."""
        client = OParlClient()

        with pytest.raises(RuntimeError):
            await client.fetch("https://example.org/test")

    @pytest.mark.asyncio
    async def test_fetch_success(self) -> None:
        """Test successful fetch operation (mocked)."""
        async with OParlClient() as client:
            # Mock the _do_fetch method
            mock_result = FetchResult(
                url="https://example.org/test",
                data={"key": "value"},
                status_code=200,
            )
            with patch.object(client, "_do_fetch", return_value=mock_result):
                result = await client.fetch("https://example.org/test")

                assert result.status_code == 200
                assert result.data == {"key": "value"}

    @pytest.mark.asyncio
    async def test_fetch_with_retry(self) -> None:
        """Test fetch retries on server error (mocked)."""
        async with OParlClient() as client:
            # Create a mock that fails then succeeds
            call_count = 0

            async def mock_do_fetch(url: str, use_cache: bool, skip_wait: bool) -> FetchResult:
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    import httpx
                    response = MagicMock()
                    response.status_code = 500
                    raise httpx.HTTPStatusError("Server error", request=MagicMock(), response=response)
                return FetchResult(
                    url=url,
                    data={"success": True},
                    status_code=200,
                )

            with patch.object(client, "_do_fetch", side_effect=mock_do_fetch):
                with patch("asyncio.sleep", return_value=None):
                    result = await client.fetch("https://example.org/test")

                    assert result.status_code == 200
                    assert call_count == 2
