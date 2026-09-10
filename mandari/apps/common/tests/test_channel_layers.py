# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Channel-Layer für WebSockets (#216).

Der Lese-Timeout der Redis-Verbindung muss über der blockierenden Wartezeit von
channels_redis liegen – sonst endet jede WebSocket-Verbindung nach ~5 Sekunden.
"""

from __future__ import annotations

from channels_redis.core import RedisChannelLayer
from channels_redis.utils import create_pool, decode_hosts

from mandari.redis_config import build_channel_layers

URL = "redis://:geheim@redis.example:6379/0"


def test_ohne_redis_url_wird_in_memory_genutzt() -> None:
    for value in (None, ""):
        assert build_channel_layers(value)["default"]["BACKEND"] == "channels.layers.InMemoryChannelLayer"


def test_lese_timeout_liegt_ueber_der_blockierenden_wartezeit() -> None:
    config = build_channel_layers(URL)["default"]
    layer = RedisChannelLayer(**config["CONFIG"])

    assert layer.hosts[0]["socket_timeout"] > layer.brpop_timeout


def test_timeout_kommt_im_verbindungspool_an() -> None:
    hosts = decode_hosts(build_channel_layers(URL)["default"]["CONFIG"]["hosts"])
    pool = create_pool(hosts[0])

    assert pool.connection_kwargs["socket_timeout"] > RedisChannelLayer.brpop_timeout
