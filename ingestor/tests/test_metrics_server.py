# SPDX-License-Identifier: AGPL-3.0-or-later
"""Der Metrik-Server liefert /metrics (vorher HTTP 500: aiohttp lehnt „charset“ im Argument content_type ab)."""

import socket

import httpx
import pytest

from src.metrics import MetricsCollector


def _freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_metrics_liefert_prometheus_format() -> None:
    sammler = MetricsCollector()
    if not sammler._prometheus_enabled:
        pytest.skip("prometheus_client nicht installiert")
    port = _freier_port()
    await sammler.start_server(port=port)

    async with httpx.AsyncClient() as client:
        antwort = await client.get(f"http://127.0.0.1:{port}/metrics")

    assert antwort.status_code == 200
    assert antwort.headers["content-type"].startswith("text/plain")
    assert "# HELP" in antwort.text
